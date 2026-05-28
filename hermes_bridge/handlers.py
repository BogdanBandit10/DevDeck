#!/usr/bin/env python3
from __future__ import annotations
import json
import mimetypes
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from typing import Any

from state import BridgeState, ROOT, ASSETS_DIR
from bridge_utils import append_log, write_status, normalized_command, first_text, now_iso, expand_path
from assistant_protocol import (
    AGENT_PROFILES,
    AGENT_LIST_PREFIXES,
    PRESET_LIST_PREFIXES,
    agents_payload,
    capabilities_payload,
    presets_payload,
    classify_intent,
    enrich_response,
    format_agents_for_chat,
    format_presets_for_chat,
    normalize_task_payload,
    strip_async_prefix,
)
from runners import (
    lmstudio_status,
    open_big_window,
    start_lmstudio_stack,
)
from services import connector_status_payload, terminate_pid_tree, read_connector_state, read_supervisor_state

def bridge_model_label(cfg: dict[str, Any]) -> str:
    provider = cfg.get("provider")
    model = cfg.get("model")
    if provider and model:
        return f"{provider} / {model}"
    if model:
        return str(model)
    return "Hermes default model from config.yaml"

def save_bridge_config(values: dict[str, Any]) -> dict[str, Any]:
    from hermes_bridge import load_config, CONFIG_PATH
    current = load_config()
    current.update(values)
    CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    BridgeState.cfg = current
    return current

def model_command_response(message: str, request_id: str) -> dict[str, Any] | None:
    from hermes_bridge import parse_model_command
    action, provider, model = parse_model_command(message)
    if action == "none":
        return None
    metadata = classify_intent("system control")
    cfg = BridgeState.cfg
    if not cfg.get("model_switch_enabled", True):
        return enrich_response("Model switching is disabled in the bridge config.", metadata, ok=False, error="model switching disabled", request_id=request_id)
    if action == "status":
        text = f"Current model: {bridge_model_label(cfg)}."
        body = enrich_response(text, metadata, request_id=request_id)
        return body
    if action == "help" or not model:
        return enrich_response("Usage: /model set <model>", metadata, request_id=request_id)
    new_cfg = save_bridge_config({"model": model, "provider": provider})
    body = enrich_response(f"Model saved: {bridge_model_label(new_cfg)}", metadata, request_id=request_id)
    body["restart_required"] = True
    return body

def directory_command_response(message: str, request_id: str) -> dict[str, Any] | None:
    norm = normalized_command(message)
    if norm in {normalized_command(s) for s in AGENT_LIST_PREFIXES}:
        metadata = classify_intent("/agents")
        body = enrich_response(format_agents_for_chat(), metadata, request_id=request_id)
        body["agents"] = agents_payload()
        return body
    if norm in {normalized_command(s) for s in PRESET_LIST_PREFIXES}:
        metadata = classify_intent("/presets")
        body = enrich_response(format_presets_for_chat(), metadata, request_id=request_id)
        body["presets"] = presets_payload()
        return body
    return None

def big_window_state() -> dict[str, Any]:
    runtime = BridgeState.runtime
    with BridgeState.activity_lock:
        activity = list(BridgeState.activity)
    tasks = [normalize_task_payload(t) for t in BridgeState.task_queue.list(limit=50)] if BridgeState.task_queue else []
    completed = len([task for task in tasks if task.get("status") == "completed"])
    active = len([task for task in tasks if task.get("status") in {"queued", "running"}])
    return {
        "success": True, "ok": True,
        "backend": backend_label(BridgeState.cfg),
        "ready": bool(runtime and runtime.ready),
        "turns": getattr(runtime, "turn_count", 0),
        "uptime_seconds": round(time.time() - BridgeState.started_at, 1),
        "agents": agents_payload(),
        "chatgpt_connector": connector_status_payload(),
        "opencode_runner": lmstudio_status(),
        "activity": activity,
        "tasks": tasks,
        "mission": {
            "tasks_active": active,
            "tasks_completed": completed,
            "success_rate": 100 if not tasks else round((completed / len(tasks)) * 100),
            "activity_events": len(activity),
        },
        "widget": {"connected": True},
    }

def big_window_url(cfg: dict[str, Any]) -> str:
    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 44888)
    return f"http://{host}:{port}/big-window"

def backend_label(cfg: dict[str, Any]) -> str:
    return "Hermes" if str(cfg.get("backend") or "codex").lower() == "hermes" else "Codex"

def truncate(text: str, limit: int) -> str:
    if not text: return ""
    if len(text) <= limit: return text
    return text[:limit] + "...[truncated]"

def chat_with_timeout(
    runtime: Any,
    message: str,
    request_id: str,
    agent_name: str,
    timeout_seconds: float,
    on_late: Any | None = None,
) -> tuple[str, str]:
    result_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=1)
    timed_out = threading.Event()

    def _worker() -> None:
        try:
            result = runtime.chat(message, request_id, agent_name=agent_name)
            if timed_out.is_set() and on_late:
                on_late("ok", result)
            else:
                result_queue.put(("ok", result))
        except Exception as exc:
            error = str(exc)
            if timed_out.is_set() and on_late:
                on_late("error", error)
            else:
                result_queue.put(("error", error))

    threading.Thread(target=_worker, daemon=True).start()
    try:
        return result_queue.get(timeout=max(1.0, timeout_seconds))
    except queue.Empty:
        timed_out.set()
        return "timeout", "Hermes is still working on that message. I will post the reply into the activity feed when it finishes."

def task_queue_result_payload(payload: dict[str, Any], required: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict): return {} if not required else {"summary": str(payload)}
    return {
        "summary": str(payload.get("summary") or ""),
        "files_changed": list(payload.get("files_changed") or []),
        "diff": str(payload.get("diff") or ""),
        "verification": str(payload.get("verification") or ""),
        "errors": str(payload.get("errors") or ""),
    }

def task_packet_text(packet: dict[str, Any]) -> str:
    parts = []
    for key in ["TASK", "FILES", "ACTIONS", "RESTRICTIONS", "OUTPUT FORMAT", "STOP CONDITIONS"]:
        if key in packet:
            val = packet[key]
            if isinstance(val, list): val = "\n".join(f"- {v}" if not str(v).startswith(tuple("123456789")) else v for v in val)
            parts.append(f"{key}\n{val}")
    return "\n\n".join(parts)

def big_window_html(cfg: dict[str, Any]) -> str:
    endpoint = quote("/codex/chat")
    state_endpoint = quote("/big-window/state")
    backend = backend_label(cfg)
    port = int(cfg.get("port") or 44888)
    workspace = str(expand_path(cfg.get("working_directory"), ROOT.parent) or ROOT.parent)
    html_path = ASSETS_DIR / "index.html"
    try:
        content = html_path.read_text(encoding="utf-8")
        return (
            content
            .replace("__ENDPOINT__", endpoint)
            .replace("__STATE_ENDPOINT__", state_endpoint)
            .replace("__BACKEND_JSON__", json.dumps(backend))
            .replace("__BACKEND__", backend)
            .replace("__PORT__", str(port))
            .replace("__WORKSPACE_JSON__", json.dumps(workspace))
            .replace("__WORKSPACE__", workspace)
        )
    except Exception:
        return "<html><body>Asset Error</body></html>"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        append_log("access.log", f"{self.client_address[0]} - {fmt % args}")

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str, status: int = 200) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_asset(self, path: str) -> None:
        target = (ASSETS_DIR / path).resolve()
        if not target.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> tuple[Any, str]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        try: return json.loads(raw), raw
        except Exception: return {"message": raw}, raw

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        from hermes_bridge import (
            brawn_list_response, brawn_get_response,
            task_queue_list_response, task_queue_get_response, task_queue_packet_response,
            assistant_status_response, brawn_error_response, queue_error_response,
            task_queue_log_response
        )
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        req_id = f"{int(time.time() * 1000)}"
        if path.startswith("/assets/"): self._send_asset(path[8:]); return
        if path in {"/", "/health", "/status"}:
            rt = BridgeState.runtime
            self._send_json({"ok": True, "ready": bool(rt and rt.ready), "uptime": round(time.time() - BridgeState.started_at, 1)})
            return
        if path == "/big-window": self._send_html(big_window_html(BridgeState.cfg)); return
        if path == "/big-window/state": self._send_json(big_window_state()); return
        if path == "/assistant/agents": self._send_json({"success": True, "ok": True, "agents": agents_payload(), "request_id": req_id}); return
        if path == "/assistant/presets": self._send_json({"success": True, "ok": True, "presets": presets_payload(), "request_id": req_id}); return
        if path == "/assistant/capabilities": self._send_json({"success": True, "ok": True, **capabilities_payload(), "request_id": req_id}); return
        if path == "/connector/status": self._send_json({"ok": True, "connector": connector_status_payload()}); return
        if path == "/opencode/status": self._send_json({"ok": True, "opencode": lmstudio_status()}); return
        if path == "/brawn/commands": self._send_json(brawn_list_response(parse_qs(parsed.query), req_id)); return
        if path.startswith("/brawn/commands/"): self._send_json(brawn_get_response(path.split("/")[3], req_id)); return
        if path == "/task-queue/tasks": self._send_json(task_queue_list_response(parse_qs(parsed.query), req_id)); return
        if path.startswith("/task-queue/tasks/"):
            parts = path.split("/")
            action = parts[4] if len(parts) > 4 else ""
            if action == "packet": self._send_json(task_queue_packet_response(parts[3], req_id))
            elif action == "log": self._send_json(task_queue_log_response(parts[3], req_id))
            else: self._send_json(task_queue_get_response(parts[3], req_id))
            return
        if path == "/tasks": self._send_json(assistant_status_response(req_id)); return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        from hermes_bridge import (
            brawn_create_response, brawn_status_response,
            task_queue_create_response, task_queue_status_response,
            start_assistant_task, cancel_assistant_task, assistant_status_response,
            brawn_error_response, queue_error_response,
            schedule_bridge_shutdown, schedule_integrated_exit,
            record_activity, is_integrated_exit_request, is_big_window_request, should_start_async,
            rollback_task_response
        )
        path = urlparse(self.path).path.rstrip("/") or "/"
        req_id = f"{int(time.time() * 1000)}"
        try:
            payload, raw = self._read_body()
            if path == "/brawn/commands": self._send_json(brawn_create_response(payload, req_id)); return
            if path.startswith("/brawn/commands/"):
                parts = path.split("/")
                self._send_json(brawn_status_response(parts[3], parts[4], payload, req_id))
                return
            if path == "/task-queue/clear":
                from hermes_bridge import require_task_queue
                count = require_task_queue().clear_terminal()
                self._send_json({"success": True, "ok": True, "cleared": count})
                return
            if path == "/task-queue/tasks": self._send_json(task_queue_create_response(payload, req_id)); return
            if path.startswith("/task-queue/tasks/"):
                parts = path.split("/")
                action = parts[4] if len(parts) > 4 else ""
                if action == "rollback":
                    self._send_json(rollback_task_response(parts[3], req_id))
                    return
                self._send_json(task_queue_status_response(parts[3], parts[4], payload, req_id))
                return
            if path == "/shutdown": schedule_bridge_shutdown(self.server); self._send_json({"ok": True}); return
            if path in {"/opencode/warmup", "/opencode/start"}:
                from runners import start_lmstudio_stack
                status = start_lmstudio_stack(load_model=True)
                self._send_json({"success": True, "ok": True, "opencode": status, "opencode_runner": status})
                return
            if path == "/opencode/auto-run":
                enabled = bool(payload.get("enabled"))
                cfg = save_bridge_config({"opencode_auto_run_chatgpt_tasks": enabled})
                status = lmstudio_status()
                status["auto_run_chatgpt_tasks"] = bool(cfg.get("opencode_auto_run_chatgpt_tasks"))
                self._send_json({"success": True, "ok": True, "opencode_runner": status})
                return
            if path == "/control/big-window":
                ok, d = open_big_window(BridgeState.cfg)
                self._send_json({"ok": ok, "detail": d})
                return
            
            # Chat routing
            msg = first_text(payload, ["message", "prompt", "text"])
            if not msg: self._send_json({"error": "no message"}, 400); return
            
            record_activity("user", msg)
            meta = classify_intent(msg)
            if backend_label(BridgeState.cfg) == "Codex" and meta.get("intent") == "normal_chat":
                meta = {
                    **meta,
                    "agent_name": "Codex",
                    "agent_role": "primary_assistant",
                    "agent_mood": "ready",
                    "quip": "Codex is ready.",
                }

            directory_response = directory_command_response(msg, req_id)
            if directory_response:
                self._send_json(directory_response)
                return

            model_response = model_command_response(msg, req_id)
            if model_response:
                self._send_json(model_response)
                return
            
            if should_start_async(msg, meta, BridgeState.cfg, path):
                self._send_json(start_assistant_task(msg, meta, req_id)); return
                
            rt = BridgeState.runtime
            if not rt: raise RuntimeError("No runtime")
            write_status(busy=True)
            def _late_chat(status: str, text: str) -> None:
                record_activity("agent" if status == "ok" else "error", text, agent_name=meta.get("agent_name", "Hermes"))

            chat_status, res = chat_with_timeout(
                rt,
                strip_async_prefix(msg),
                req_id,
                meta.get("agent_name", "Hermes"),
                float(BridgeState.cfg.get("shared_channel_timeout_seconds") or 12),
                on_late=_late_chat,
            )
            if chat_status != "timeout":
                record_activity("agent" if chat_status == "ok" else "error", res, agent_name=meta.get("agent_name", "Hermes"))
            if chat_status == "timeout":
                meta = {**meta, "status": "running"}
            self._send_json(enrich_response(res, meta, ok=chat_status != "error", error="" if chat_status != "error" else res, request_id=req_id))
            
        except Exception as exc:
            append_log("bridge.log", f"Error: {exc}")
            self._send_json({"error": str(exc)}, 500)
