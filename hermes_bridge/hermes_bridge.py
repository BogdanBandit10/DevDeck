#!/usr/bin/env python3
"""Hermes-backed compatibility bridge for the MateEngineX widget.

The Unity widget currently calls a local bridge at /codex/chat. This
server keeps that localhost HTTP contract but routes messages into one
persistent Hermes Agent runtime for the lifetime of the widget session.
"""
from __future__ import annotations
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import atexit
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Path hack to support running as a script or module
import sys
BRIDGE_DIR = Path(__file__).resolve().parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))
# Also add parent to support 'from hermes_bridge import ...'
if str(BRIDGE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR.parent))

from state import BridgeState, ROOT, CONFIG_PATH, LOG_DIR
from bridge_utils import append_log, write_status, normalized_command, now_iso, strip_ansi, expand_path
from assistant_protocol import (
    classify_intent,
    enrich_response,
    normalize_task_payload,
    strip_async_prefix,
    AGENT_PROFILES,
)
from task_queue import (
    DurableTaskQueue,
    TaskQueueError,
    TaskQueueNotFoundError,
    TaskQueueValidationError,
)
from runners import (
    lmstudio_status,
    start_lmstudio_stack,
    git_status_lines,
    git_status_map,
    git_rollback_files,
    extract_opencode_text,
    command_path,
    run_command_capture,
)
from services import (
    ChatGPTConnectorSupervisor,
    cleanup_external_processes,
    terminate_pid_tree,
    read_supervisor_state,
)
from runtime import CodexRuntime, HermesPersistentRuntime
from handlers import (
    Handler,
    model_command_response,
    directory_command_response,
    open_big_window,
    task_queue_result_payload,
    task_packet_text,
    truncate
)

def cleanup() -> None:
    """Final cleanup of external processes on exit."""
    append_log("bridge.log", "Final cleanup on exit...")
    cleanup_external_processes()

atexit.register(cleanup)

def handle_exit_signal(sig: int, frame: Any) -> None:
    append_log("bridge.log", f"Caught exit signal {sig}, shutting down...")
    sys.exit(0)

def load_config() -> dict[str, Any]:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        append_log("bridge.log", f"Failed to load config.json: {exc}")
    # Default fallback
    return {
        "host": "127.0.0.1",
        "port": 44888,
        "mode": "persistent_direct",
        "working_directory": str(ROOT.parent),
        "chatgpt_connector_enabled": False,
        "chatgpt_connector_allow_no_auth": False
    }

def reexec_with_configured_python(cfg: dict[str, Any], config_path: str) -> bool:
    """Restart under the Hermes venv when the bridge was launched with a random Python."""
    if str(cfg.get("backend") or "codex").lower() != "hermes":
        return False
    if os.environ.get("DEV_DECK_BRIDGE_REEXEC") == "1":
        return False
    configured = expand_path(cfg.get("hermes_python"))
    if not configured or not configured.exists():
        return False
    current = Path(sys.executable).resolve()
    try:
        if current.parent.resolve() == configured.parent.resolve():
            return False
    except Exception:
        pass
    env = os.environ.copy()
    env["DEV_DECK_BRIDGE_REEXEC"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    command = [str(configured), str(Path(__file__).resolve()), "--config", config_path]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.Popen(
        command,
        cwd=str(ROOT.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    append_log("bridge.log", f"Re-executed bridge with configured Hermes Python: {configured}")
    return True

def ensure_dirs() -> None:
    from state import LOG_DIR, TURN_LOG_DIR, ASSETS_DIR
    for d in (LOG_DIR, TURN_LOG_DIR, ASSETS_DIR):
        d.mkdir(parents=True, exist_ok=True)

def parse_model_command(message: str) -> tuple[str, str | None, str | None]:
    text = (message or "").strip()
    lowered = text.lower()
    if lowered in {"/model", "model", "what model", "show model", "current model"}:
        return "status", None, None
    if lowered.startswith("/model set "):
        args = text[len("/model set "):].strip()
    elif lowered.startswith("/model "):
        args = text[len("/model "):].strip()
    else:
        return "none", None, None
    if not args:
        return "help", None, None
    parts = args.split()
    providers = {"openrouter", "anthropic", "openai", "nous", "google", "gemini", "deepseek", "xai", "groq", "custom"}
    if len(parts) >= 2 and parts[0] in providers:
        return "set", parts[0], " ".join(parts[1:])
    return "set", None, args

def record_activity(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    import uuid
    item = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "text": str(text or ""),
        "created_at": now_iso(),
        **extra,
    }
    with BridgeState.activity_lock:
        BridgeState.activity.append(item)
        BridgeState.activity = BridgeState.activity[-80:]
    return item

def schedule_bridge_shutdown(server: ThreadingHTTPServer, delay_seconds: float = 0.25) -> None:
    def _worker() -> None:
        time.sleep(delay_seconds)
        append_log("bridge.log", "Shutdown requested")
        BridgeState.shutting_down = True
        cleanup_external_processes()
        try:
            server.shutdown()
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_worker, daemon=True).start()

def schedule_integrated_exit(server: ThreadingHTTPServer, delay_seconds: float = 0.75) -> None:
    def _worker() -> None:
        time.sleep(delay_seconds)
        cleanup_external_processes()
        time.sleep(1.0)
        if not read_supervisor_state().get("supervisor_pid"):
            BridgeState.shutting_down = True
            server.shutdown()
    threading.Thread(target=_worker, daemon=True).start()

def should_start_async(message: str, metadata: dict[str, Any], cfg: dict[str, Any], path: str) -> bool:
    if not cfg.get("async_long_tasks_enabled", True):
        return False
    raw = (message or "").strip().lower()
    prefixes = ["/task", "/async", "/agent", "/ideas", "/goal", "/team"]
    if any(raw.startswith(p) for p in prefixes):
        return True
    if bool(metadata.get("async_recommended")):
        return True
    compat_keywords = [str(item).strip().lower() for item in (cfg.get("compat_async_keywords") or []) if str(item).strip()]
    if any(keyword and keyword in raw for keyword in compat_keywords):
        return True
    if path == "/assistant/task":
        return True
    return False

def start_assistant_task(message: str, metadata: dict[str, Any], request_id: str) -> dict[str, Any]:
    runtime = BridgeState.runtime
    q = require_task_queue()
    if not runtime or not q:
        raise RuntimeError("Runtime or Queue not initialized")
    task_message = strip_async_prefix(message)
    task_intent = str(metadata.get("intent") or "long_running_task")
    if task_intent == "normal_chat" and metadata.get("async_recommended"):
        task_intent = "long_running_task"
    
    # Brain/Hands mandate: Unified background task creation
    task = q.create({
        "packet": {
            "TASK": task_message,
            "FILES": ["Current shared channel context"],
            "ACTIONS": [
                "Handle the user request through the assigned assistant persona.",
                "Post the result back to the shared activity feed.",
                "Report any errors clearly."
            ],
            "RESTRICTIONS": [
                "Do not make unrelated changes.",
                "Stop if the request is ambiguous or unsafe."
            ],
            "OUTPUT FORMAT": [
                "1. You MUST actually perform the requested work using your tools (editing files, running commands, etc.).",
                "2. Do not just output a plan. You are the executor.",
                "3. Once the work is completely finished, structure your FINAL text response exactly like this:",
                "A brief, personality-driven conversational message. DO NOT say 'Understood, I am operating as...' Just speak naturally as your persona.",
                "---REPORT---",
                "Detailed technical summary, files changed, diffs, errors, and unverified items."
            ],
            "STOP CONDITIONS": [
                "Stop if required local context is unavailable.",
                "Stop if execution fails."
            ]
        },
        "message": task_message,
        "intent": task_intent,
        "agent_name": str(metadata.get("agent_name") or "Codex"),
    }, source="assistant-protocol", metadata=metadata)
    
    def runner(prompt: str, async_req_id: str, initial_agent: str) -> str:
        current_agent = initial_agent
        current_prompt = prompt
        final_res = ""
        import re
        
        for _ in range(5):  # Max 5 handoffs per task to prevent infinite loops
            # Build the Persona context
            profile_prompt = AGENT_PROFILES.get(current_agent, {}).get("system_prompt", "")
            
            # Brain mandate: Inject Swarm Handoff instructions and Roster
            roster = ", ".join([f"{name} ({info.get('owns', 'general')})" for name, info in AGENT_PROFILES.items()])
            handoff_instructions = (
                f"TEAM COLLABORATION: The following agents are available: {roster}. "
                "If another agent is better suited for the next step, "
                'hand off the task by outputting exactly: <handoff target="AgentName">Reason and instructions</handoff>.'
            )
            
            packet_str = ""
            if "packet" in task:
                packet_str = "\n\nTASK PACKET INSTRUCTIONS:\n" + "\n".join(
                    f"{k}:\n" + "\n".join(f"- {v}" for v in (val if isinstance(val, list) else [val]))
                    for k, val in task["packet"].items() if val
                )
            
            # Combine instructions into the prompt. 
            # CodexRuntime.chat will handle the primary persona wrapping.
            cfg = BridgeState.cfg
            backend = str(cfg.get("backend") or "").lower()
            task_backend = str(cfg.get("task_executor_backend") or "").lower()
            append_log("bridge.log", f"DEBUG: backend={backend}, task_backend={task_backend}")
            
            if "codex" in backend or "codex" in task_backend:
                # For Codex, we prioritize the raw user request to trigger tool use directly
                combined_prompt = f"TASK: {current_prompt}"
            else:
                combined_prompt = f"{handoff_instructions}{packet_str}\n\nUser request: {current_prompt}"
            
            res = runtime.chat(combined_prompt, async_req_id, current_agent)
            
            # Detect handoff
            match = re.search(r'<handoff target="([^"]+)">(.*?)</handoff>', res, re.IGNORECASE | re.DOTALL)
            if match:
                next_agent = match.group(1).strip()
                reason = match.group(2).strip()
                record_activity("handoff", f"Handed off to {next_agent}: {truncate(reason, 100)}", source="agent", status="running", request_id=request_id, task_id=task["task_id"], agent_name=current_agent)
                
                # Update task status for UI
                q.update_status(task["task_id"], "running", progress_message=f"Handed off to {next_agent}.")
                
                current_prompt = f"Handoff from {current_agent}:\nReason: {reason}\nPrevious output: {res}"
                current_agent = next_agent
                continue
                
            final_res = res
            break
            
        chat_res = final_res
        if "---REPORT---" in final_res:
            chat_res = final_res.split("---REPORT---")[0].strip()
            
        record_activity("task", chat_res or f"{task['task_id']} completed", source="agent", status="completed", request_id=request_id, task_id=task["task_id"], agent_name=current_agent)
        return final_res

    q.start_background(task["task_id"], runner)
    return enrich_response(f"Task {task['task_id']} is running.", metadata, request_id=request_id, task=task)

def assistant_status_response(request_id: str, task_id: str | None = None) -> dict[str, Any]:
    q = require_task_queue()
    if not q:
        return enrich_response("Queue not initialized", {}, ok=False, request_id=request_id)
    if task_id:
        task = q.get(task_id)
        if not task:
            return enrich_response("Not found", {}, ok=False, request_id=request_id)
        return enrich_response("Task status", {}, request_id=request_id, task=normalize_task_payload(task))
    
    items = [normalize_task_payload(item) for item in q.list(limit=20)]
    # Create summary for chat
    if not items:
        summary = "No background tasks yet."
    else:
        summary = "Recent background tasks:\n" + "\n".join([f"- {t['task_id']}: {t['status']} | {t['agent_name']}" for t in items[:5]])
    
    body = enrich_response(summary, {}, request_id=request_id)
    body["tasks"] = items
    return body

def cancel_assistant_task(task_id: str, request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    if q and q.get(task_id):
        # mark_failed acts as a stop for now in this unified model
        q.mark_failed(task_id, error="Cancelled by user.")
        return enrich_response(f"Task {task_id} cancelled.", {}, request_id=request_id)
    return enrich_response("Task not found", {}, ok=False, request_id=request_id)

def queue_response(task: dict[str, Any] | None = None, *, tasks: list[dict[str, Any]] | None = None, request_id: str = "") -> dict[str, Any]:
    body = {"success": True, "ok": True, "request_id": request_id}
    if task:
        body["task"] = task
        body["task_id"] = task.get("task_id")
    if tasks is not None:
        body["tasks"] = tasks
    return body

def queue_error_response(exc: Exception, request_id: str) -> tuple[dict[str, Any], int]:
    code = getattr(exc, "status_code", 500)
    return {"success": False, "ok": False, "error": str(exc), "request_id": request_id}, int(code)

def require_task_queue() -> DurableTaskQueue:
    if not BridgeState.task_queue:
        raise RuntimeError("Task queue not initialized")
    return BridgeState.task_queue

def brawn_response(task: dict[str, Any] | None = None, *, tasks: list[dict[str, Any]] | None = None, request_id: str = "") -> dict[str, Any]:
    body = {"success": True, "ok": True, "request_id": request_id}
    if task:
        body["command"] = task
        body["command_id"] = task.get("task_id")
    if tasks is not None:
        body["commands"] = tasks
    return body

def brawn_error_response(exc: Exception, request_id: str) -> tuple[dict[str, Any], int]:
    return queue_error_response(exc, request_id)

def brawn_list_response(query: dict[str, list[str]], request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    status = (query.get("status") or [""])[0] or None
    limit = int((query.get("limit") or ["50"])[0])
    return brawn_response(tasks=q.list(status=status, limit=limit), request_id=request_id)

def brawn_get_response(task_id: str, request_id: str) -> dict[str, Any]:
    task = require_task_queue().get(task_id)
    if not task:
        raise TaskQueueNotFoundError(f"No task {task_id}")
    return brawn_response(task=task, request_id=request_id)

def brawn_create_response(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    # Brain/Hands wrapping
    instr = str(payload.get("instruction") or "").strip()
    if not instr:
        raise TaskQueueValidationError("instruction required")
    packet = {
        "TASK": instr,
        "FILES": ["Current project context"],
        "ACTIONS": ["Perform the work.", "Verify changes.", "Report."],
        "RESTRICTIONS": ["Surgical changes only.", "Never ask to think."],
        "OUTPUT FORMAT": ["Files changed:", "Diff:", "Errors:"],
        "STOP CONDITIONS": ["Stop if ambiguous."]
    }
    task = require_task_queue().create({"packet": packet}, source="chatgpt-mcp")
    return brawn_response(task=task, request_id=request_id)

def brawn_status_response(task_id: str, action: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    if action == "ack":
        task = q.claim(task_id, executor="manual")
    elif action == "complete":
        task = q.mark_completed(task_id, result=task_queue_result_payload(payload))
    elif action == "fail":
        task = q.mark_failed(task_id, error=str(payload.get("error") or "Failed"))
    else:
        raise TaskQueueValidationError(f"Unknown action {action}")
    return brawn_response(task=task, request_id=request_id)

def task_queue_list_response(query: dict[str, list[str]], request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    status = (query.get("status") or [""])[0] or None
    limit = int((query.get("limit") or ["50"])[0])
    return queue_response(tasks=q.list(status=status, limit=limit), request_id=request_id)

def task_queue_get_response(task_id: str, request_id: str) -> dict[str, Any]:
    task = require_task_queue().get(task_id)
    if not task:
        raise TaskQueueNotFoundError(f"No task {task_id}")
    return queue_response(task=task, request_id=request_id)

def task_queue_packet_response(task_id: str, request_id: str) -> dict[str, Any]:
    task = require_task_queue().require(task_id)
    return {
        "success": True, "ok": True, "request_id": request_id,
        "task_id": task_id, "status": task.get("status"),
        "packet": task.get("packet") or {}
    }


def task_queue_log_response(task_id: str, request_id: str) -> dict[str, Any]:
    log_path = LOG_DIR / "tasks" / f"{task_id}.log"
    if not log_path.exists():
        step_log_path = LOG_DIR / "tasks" / f"{task_id}_0.log"
        if step_log_path.exists():
            log_path = step_log_path
    content = ""
    if log_path.exists():
        try:
            # Return last 100 lines for "live" feel
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                content = "".join(lines[-100:])
        except Exception as exc:
            content = f"Error reading log: {exc}"
    
    return {
        "success": True, "ok": True, "request_id": request_id,
        "task_id": task_id,
        "log_tail": content
    }

def task_queue_create_response(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    source = str(payload.get("source") or "bridge")
    task = q.create(payload, source=source)
    if source == "chatgpt-mcp" and BridgeState.cfg.get("opencode_auto_run_chatgpt_tasks", True):
        return schedule_approved_opencode_task(
            task["task_id"],
            {"executor": "chatgpt-mcp-auto"},
            request_id,
        )
    return queue_response(task=task, request_id=request_id)

def task_queue_status_response(task_id: str, action: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    if action == "running":
        task = q.mark_running(task_id)
    elif action == "claim":
        task = q.claim(task_id, executor=str(payload.get("executor") or "manual"))
    elif action == "result":
        task = q.submit_result(task_id, task_queue_result_payload(payload, required=True) or {})
    elif action == "complete":
        task = q.mark_completed(task_id, result=task_queue_result_payload(payload))
    elif action == "fail":
        task = q.mark_failed(task_id, error=str(payload.get("error") or "Failed"))
    elif action == "approve-run":
        return schedule_approved_opencode_task(task_id, payload, request_id)
    elif action == "rollback":
        return rollback_task_response(task_id, request_id)
    else:
        raise TaskQueueValidationError(f"Unknown action {action}")
    return queue_response(task=task, request_id=request_id)

def schedule_approved_opencode_task(task_id: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    executor = str(payload.get("executor") or "opencode-ui")
    task = q.require(task_id)
    status = str(task.get("status") or "")
    if status == "pending":
        task = q.claim(task_id, executor=executor, note="Approved for local OpenCode execution.")
    elif status != "running":
        raise TaskQueueValidationError(f"OpenCode can only run pending or running tasks; current status is {status}")

    def _worker() -> None:
        try:
            run_approved_opencode_task(task_id, payload, f"async_{request_id}")
        except Exception as exc:
            try:
                q.mark_failed(task_id, error=str(exc), executor=executor)
            except Exception:
                append_log("bridge.log", f"OpenCode task {task_id} failed and could not update queue: {exc}")

    threading.Thread(target=_worker, daemon=True).start()
    return queue_response(task=task, request_id=request_id)

def run_approved_opencode_task(task_id: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    task = q.require(task_id)
    packet_data = task.get("packet") or {}
    
    # Brain/Hands mandate: Support Chains (list of packets)
    packets = packet_data if isinstance(packet_data, list) else [packet_data]
    
    cwd = Path(str(
        BridgeState.cfg.get("opencode_working_directory")
        or BridgeState.cfg.get("working_directory")
        or "."
    )).expanduser()
    model = str(payload.get("model") or BridgeState.cfg.get("opencode_model") or "lmstudio/qwen3.5-9b-mtp")
    timeout = int(BridgeState.cfg.get("opencode_timeout_seconds") or 600)
    
    total_files_changed = set()
    chain_results = []
    
    for i, packet in enumerate(packets):
        # Brain/Hands mandate: git tracking
        before_status = git_status_map(cwd)
        started = time.time()
        
        # Execution
        log_path = LOG_DIR / "tasks" / f"{task_id}_{i}.log"
        executor_backend = str(
            payload.get("executor_backend")
            or BridgeState.cfg.get("task_executor_backend")
            or BridgeState.cfg.get("backend")
            or "opencode"
        ).lower()
        if executor_backend == "codex":
            proc, raw_output = run_codex_task_capture(packet, cwd=cwd, timeout=timeout, log_path=log_path, request_id=request_id, step=i)
        else:
            cmd = [command_path(str(BridgeState.cfg.get("opencode_command") or "opencode")), "run", task_packet_text(packet), "--model", model, "--format", "json", "--print-logs"]
            if BridgeState.cfg.get("opencode_skip_permissions_after_approval", True):
                cmd.append("--dangerously-skip-permissions")
            proc = run_command_capture(cmd, cwd=cwd, timeout=timeout, log_path=log_path)
            raw_output = ""
            try:
                raw_output = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        
        after_status = git_status_map(cwd)
        output = strip_ansi(raw_output)
        if executor_backend != "codex":
            output = extract_opencode_text(raw_output, "")
        
        # Mandatory Verification Loop
        verification_output = ""
        actions_str = str(packet.get("ACTIONS", ""))
        if "Verify" in actions_str or "verify" in actions_str.lower():
            verification_output = f"Verification Step Captured:\nExit Code: {proc.returncode}\n{truncate(output, 3000)}"

        files_changed = [p for p, s in after_status.items() if p not in before_status or before_status[p] != s]
        total_files_changed.update(files_changed)
        
        step_result = {
            "step": i + 1,
            "task": packet.get("TASK", "Step"),
            "exit_code": proc.returncode,
            "verification": verification_output,
            "files_changed": files_changed
        }
        chain_results.append(step_result)
        
        if proc.returncode != 0:
            # Chain failed: Stop and report
            
            # Brain/Hands mandate: Auto-rollback on failure
            restrictions_str = str(packet.get("RESTRICTIONS", ""))
            rb_msg = ""
            if "auto-rollback" in restrictions_str.lower() or "auto-rollback" in actions_str.lower():
                rb = git_rollback_files(cwd, files_changed)
                rb_msg = f" Auto-Rollback triggered: {', '.join(rb.get('results', []))}"
                total_files_changed.difference_update(files_changed)
            
            result = {
                "summary": f"Chain failed at step {i+1}: {step_result['task']}.{rb_msg}",
                "files_changed": list(total_files_changed),
                "diff": truncate(output, 10000),
                "verification": verification_output,
                "chain_history": chain_results,
                "exit_code": proc.returncode,
                "duration_seconds": round(time.time() - started, 1)
            }
            task = q.mark_failed(task_id, error=f"Chain failed at step {i+1}", result=result)
            return queue_response(task=task, request_id=request_id)

    # All steps passed
    result = {
        "summary": f"Chain of {len(packets)} steps completed successfully.",
        "files_changed": list(total_files_changed),
        "diff": "Multiple steps; see chain history.",
        "verification": "All steps verified.",
        "chain_history": chain_results,
        "exit_code": 0,
        "duration_seconds": 0 # TODO: track total
    }
    task = q.mark_completed(task_id, result=result)
    return queue_response(task=task, request_id=request_id)

def run_codex_task_capture(packet: dict[str, Any], cwd: Path, timeout: int, log_path: Path, request_id: str, step: int):
    configured = str(BridgeState.cfg.get("codex_command") or "").strip()
    command = configured if configured else "codex"
    resolved = command_path(command)
    if str(resolved).lower().endswith(".ps1"):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(resolved)]
    else:
        cmd = [str(resolved)]
    cmd.extend([
        "--sandbox", str(BridgeState.cfg.get("codex_sandbox_mode") or "danger-full-access"),
        "--ask-for-approval", "never",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-rules",
        "--ignore-user-config",
        "-c", "features.memory=false",
        "-c", "features.hooks=false",
        "--cd", str(cwd),
    ])
    model = str(BridgeState.cfg.get("codex_model") or "").strip()
    if model:
        cmd.extend(["--model", model])
    reasoning = str(BridgeState.cfg.get("codex_reasoning_effort") or "").strip()
    if reasoning:
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning}"'])
    cmd.append(task_packet_text(packet))

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"$ {' '.join(cmd[:-1])} <task-packet>\n\n")
        log_file.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=int(BridgeState.cfg.get("codex_task_timeout_seconds") or timeout),
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        
        # Write full JSONL to log for diagnostics
        log_file.write(proc.stdout or "")
        log_file.write(proc.stderr or "")

    messages = []
    tools_used = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip().startswith("{"): continue
        try:
            evt = json.loads(line)
            if evt.get("type") == "item.completed":
                item = evt.get("item", {})
                item_type = item.get("type")
                if item_type == "agent_message" and item.get("text"):
                    messages.append(item["text"])
                elif item_type in {"tool_call", "file_change", "command_run", "command_execution", "git_action"}:
                    details = ""
                    if item_type == "file_change":
                        changes = item.get("changes", [])
                        details = ", ".join([f"{c.get('kind', 'edit')} {Path(c.get('path', 'unknown')).name}" for c in changes])
                    elif item_type in {"command_run", "command_execution"}:
                        details = item.get("command", "unknown command")
                    
                    label = item_type.replace("_", " ").title()
                    if details:
                        tools_used.append(f"{label}: {details}")
                    else:
                        tools_used.append(f"{label} completed.")
        except Exception:
            continue
    
    final_res = "\n\n".join(messages).strip()
    if tools_used and final_res:
        final_res += "\n\n---\n" + "\n".join(tools_used)
    elif tools_used and not final_res:
        final_res = "\n".join(tools_used)
    
    if not final_res:
        final_res = strip_ansi(proc.stdout or "").strip()
        
    return proc, final_res



def rollback_task_response(task_id: str, request_id: str) -> dict[str, Any]:
    q = require_task_queue()
    task = q.require(task_id)
    result = task.get("result") or {}
    files = result.get("files_changed") or []

    cwd = Path(str(BridgeState.cfg.get("working_directory") or ".")).expanduser()
    rb = git_rollback_files(cwd, files)

    # Mark task as rolled back in history
    q.mark_failed(task_id, error=f"Rolled back: {', '.join(rb.get('results', []))}")

    return {
        "success": True,
        "ok": True,
        "request_id": request_id,
        "task_id": task_id,
        "rollback_results": rb.get("results"),
        "git_status": rb.get("git_status_after")
    }


def run_server(cfg: dict[str, Any]) -> int:
    ensure_dirs()
    BridgeState.cfg = cfg
    BridgeState.task_queue = DurableTaskQueue(ROOT / "task_queue")
    from runtime import HermesPersistentRuntime, CodexRuntime, OpenCodeRuntime
    backend = str(cfg.get("backend") or "codex").lower()
    if backend == "hermes":
        BridgeState.runtime = HermesPersistentRuntime(cfg)
    elif backend == "opencode":
        BridgeState.runtime = OpenCodeRuntime(cfg)
    else:
        BridgeState.runtime = CodexRuntime(cfg)

    
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 44888)
    server = ThreadingHTTPServer((host, port), Handler)
    
    if cfg.get("chatgpt_connector_enabled", True):
        BridgeState.connector_supervisor = ChatGPTConnectorSupervisor(cfg)
        BridgeState.connector_supervisor.start()
        
    print(f"Bridge listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0

def is_integrated_exit_request(message: str, cfg: dict[str, Any]) -> bool:
    return normalized_command(message) in {"/exit", "/quit", "exit"}

def is_big_window_request(message: str, cfg: dict[str, Any]) -> bool:
    return normalized_command(message) in {"/big", "big window"}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    
    # Register signal handlers in the main thread
    try:
        signal.signal(signal.SIGINT, handle_exit_signal)
        signal.signal(signal.SIGTERM, handle_exit_signal)
    except ValueError:
        pass # Not in main thread (e.g. during re-exec)

    cfg = load_config()
    if not args.self_test and reexec_with_configured_python(cfg, args.config):
        return 0
    if args.self_test:
        ensure_dirs()
        rt = HermesPersistentRuntime(cfg) if str(cfg.get("backend") or "codex").lower() == "hermes" else CodexRuntime(cfg)
        if not rt.ready:
            print(rt.init_error or "not ready")
            return 1
        print(f"{str(cfg.get('backend') or 'codex')} runtime initialized")
        return 0
    return run_server(cfg)

if __name__ == "__main__":
    main()
