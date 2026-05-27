#!/usr/bin/env python3
"""Hermes-backed compatibility bridge for the MateEngineX widget.

The Unity widget currently calls a local Codex bridge at /codex/chat.  This
server keeps that localhost HTTP contract but routes messages into one
persistent Hermes Agent runtime for the lifetime of the widget session.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import mimetypes
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from assistant_protocol import (
    AGENT_LIST_PREFIXES,
    AGENT_PROFILES,
    PRESET_LIST_PREFIXES,
    TaskRegistry,
    agents_payload,
    capabilities_payload,
    classify_intent,
    enrich_response,
    format_agents_for_chat,
    format_presets_for_chat,
    normalize_task_payload,
    presets_payload,
    strip_async_prefix,
)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
TURN_LOG_DIR = LOG_DIR / "turns"
ASSETS_DIR = ROOT / "assets"
STATUS_PATH = ROOT / "bridge_status.json"
SUPERVISOR_STATE_PATH = ROOT / "supervisor_state.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 44888,
    "mode": "persistent_direct",
    "session_label": "widget-bridge",
    "working_directory": "C:/Users/frank",
    "hermes_source_path": "C:/Users/frank/AppData/Local/hermes/hermes-agent",
    "hermes_python": "C:/Users/frank/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe",
    "model": None,
    "provider": None,
    "toolsets": None,
    "max_turns": None,
    "verbose": False,
    "allow_subprocess_fallback": False,
    "subprocess_command": "hermes",
    "subprocess_timeout_seconds": 900,
    "response_field": "response",
    "codex_command": None,
    "codex_model": "gpt-5.5",
    "codex_reasoning_effort": "medium",
    "codex_sandbox_mode": "danger-full-access",
    "codex_timeout_seconds": 120,
    "startup_timeout_seconds": 60,
    "shutdown_grace_seconds": 5,
    "log_max_chars_per_turn": 200000,
    "request_message_fields": ["message", "prompt", "text", "input", "content", "query", "user_message", "task"],
    "assistant_protocol_enabled": True,
    "async_long_tasks_enabled": True,
    "async_trigger_prefixes": ["/task", "/async", "/agent", "/ideas"],
    "crew_feedback_enabled": True,
    "crew_feedback_intents": ["long_running_task", "file_task", "planning", "project_goal", "assembly_line", "web_research"],
    "live_agent_chatter_enabled": True,
    "live_agent_chatter_interval_seconds": 5,
    "live_agent_chatter_max_messages": 5,
    "compat_async_keywords": ["code review", "security review", "github", "pull request", "big task", "research", "implement", "build", "fix all", "set up"],
    "model_switch_enabled": True,
    "model_switch_requires_restart": True,
    "big_window_enabled": True,
    "big_window_phrases": [
        "/big",
        "/big window",
        "/open chat",
        "big window",
        "big window mode",
        "open big window",
        "open chat window",
        "large window mode",
    ],
    "integrated_exit_enabled": True,
    "exit_phrases": [
        "/exit",
        "/quit",
        "/close",
        "/shutdown",
        "exit widget",
        "close widget",
        "quit widget",
        "shutdown widget",
        "shut down widget",
        "close assistant",
        "quit assistant",
        "shutdown assistant",
        "shut down assistant",
        "goodbye jarvis",
        "bye jarvis",
    ],
}


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TURN_LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)
    env_mode = os.environ.get("WIDGET_AGENT_MODE")
    if env_mode:
        cfg["mode"] = env_mode.strip()
    return cfg


def save_bridge_config(updates: dict[str, Any]) -> dict[str, Any]:
    current: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(updates)
    CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(current)
    BridgeState.cfg = cfg
    return cfg


def bridge_model_label(cfg: dict[str, Any]) -> str:
    provider = cfg.get("provider")
    model = cfg.get("model")
    if provider and model:
        return f"{provider} / {model}"
    if model:
        return str(model)
    return "Hermes default model from config.yaml"


def parse_model_command(message: str) -> tuple[str, str | None, str | None]:
    text = (message or "").strip()
    lowered = text.lower()
    if lowered in {"/model", "model", "what model", "what model are you using", "show model", "current model"}:
        return "status", None, None
    if lowered.startswith("/model set "):
        args = text[len("/model set "):].strip()
    elif lowered.startswith("/model "):
        args = text[len("/model "):].strip()
    elif lowered.startswith("change model to "):
        args = text[len("change model to "):].strip()
    elif lowered.startswith("switch model to "):
        args = text[len("switch model to "):].strip()
    else:
        return "none", None, None
    if not args:
        return "help", None, None
    parts = args.split()
    if len(parts) >= 2 and "/" not in parts[0] and parts[0] in {"openrouter", "anthropic", "openai", "nous", "google", "gemini", "deepseek", "xai", "groq", "custom"}:
        return "set", parts[0], " ".join(parts[1:])
    return "set", None, args


def model_command_response(message: str, request_id: str) -> dict[str, Any] | None:
    action, provider, model = parse_model_command(message)
    if action == "none":
        return None
    metadata = classify_intent("system control")
    cfg = BridgeState.cfg
    if not cfg.get("model_switch_enabled", True):
        return enrich_response("Model switching is disabled in the widget bridge config.", metadata, ok=False, error="model switching disabled", request_id=request_id)
    if action == "status":
        text = (
            f"Current widget bridge model setting: {bridge_model_label(cfg)}.\n\n"
            "To change it from the widget, type one of these:\n"
            "/model set <model>\n"
            "/model set <provider> <model>\n\n"
            "Example: /model set openrouter anthropic/claude-sonnet-4\n"
            "After changing it, restart the widget with /exit and launch it again so the persistent Hermes runtime reloads the model."
        )
        body = enrich_response(text, metadata, request_id=request_id)
        body["model"] = cfg.get("model")
        body["provider"] = cfg.get("provider")
        body["restart_required"] = True
        return body
    if action == "help" or not model:
        return enrich_response("Usage: /model set <model> or /model set <provider> <model>. Example: /model set openrouter anthropic/claude-sonnet-4", metadata, request_id=request_id)
    new_cfg = save_bridge_config({"model": model, "provider": provider})
    text = (
        f"Model setting saved for the widget bridge: {bridge_model_label(new_cfg)}.\n\n"
        "Important: the currently running hidden Hermes runtime keeps its existing model until restart. "
        "Type /exit, then launch the widget again to use the new model."
    )
    body = enrich_response(text, metadata, request_id=request_id)
    body["model"] = new_cfg.get("model")
    body["provider"] = new_cfg.get("provider")
    body["restart_required"] = True
    return body


def directory_command_response(message: str, request_id: str) -> dict[str, Any] | None:
    normalized = normalized_command(message)
    if normalized in {normalized_command(s) for s in AGENT_LIST_PREFIXES}:
        metadata = classify_intent("/agents")
        body = enrich_response(format_agents_for_chat(), metadata, request_id=request_id)
        body["agents"] = AGENT_PROFILES
        return body
    if normalized in {normalized_command(s) for s in PRESET_LIST_PREFIXES}:
        metadata = classify_intent("/presets")
        body = enrich_response(format_presets_for_chat(), metadata, request_id=request_id)
        body["presets"] = presets_payload()
        body["capabilities"] = capabilities_payload()
        return body
    return None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append_log(name: str, text: str) -> None:
    ensure_dirs()
    with (LOG_DIR / name).open("a", encoding="utf-8", errors="replace") as f:
        f.write(f"[{now_iso()}] {text}\n")


def write_status(**values: Any) -> None:
    ensure_dirs()
    current: dict[str, Any] = {}
    if STATUS_PATH.exists():
        try:
            current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(values)
    current["updated_at"] = now_iso()
    STATUS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text or "")


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def first_text(value: Any, fields: list[str]) -> str | None:
    """Extract likely user text from unknown widget JSON shapes."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, list):
        # Chat-completions style: use last user-ish message if present.
        for item in reversed(value):
            if isinstance(item, dict):
                role = str(item.get("role", "")).lower()
                if role in {"user", "human", "player", ""}:
                    found = first_text(item, fields)
                    if found:
                        return found
        for item in reversed(value):
            found = first_text(item, fields)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in fields:
            if key in value:
                found = first_text(value.get(key), fields)
                if found:
                    return found
        # Common nested payloads.
        for key in ("messages", "data", "payload", "request", "chat"):
            if key in value:
                found = first_text(value.get(key), fields)
                if found:
                    return found
    return None


def normalized_command(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9/ ]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_integrated_exit_request(message: str, cfg: dict[str, Any]) -> bool:
    if not cfg.get("integrated_exit_enabled", True):
        return False
    command = normalized_command(message)
    phrases = cfg.get("exit_phrases") or DEFAULT_CONFIG["exit_phrases"]
    return command in {normalized_command(str(p)) for p in phrases}


def is_big_window_request(message: str, cfg: dict[str, Any]) -> bool:
    if not cfg.get("big_window_enabled", True):
        return False
    command = normalized_command(message)
    phrases = cfg.get("big_window_phrases") or DEFAULT_CONFIG["big_window_phrases"]
    return command in {normalized_command(str(p)) for p in phrases}


def big_window_url(cfg: dict[str, Any]) -> str:
    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 44888)
    return f"http://{host}:{port}/big-window"


def record_activity(kind: str, text: str, **extra: Any) -> dict[str, Any]:
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


AGENT_CHATTER_LINES: dict[str, list[str]] = {
    "Relay": [
        "Keeping the shared channel tidy while everyone else kicks up dust.",
        "I am translating frantic keyboard energy into something readable.",
        "Status check: the thread is still coherent. That counts as infrastructure.",
    ],
    "Vector": [
        "I am tracing the code path. Some of these variables look like they were named during a power outage.",
        "Checking the route through the repo. If tests exist, I may briefly believe in civilization.",
        "I found the working edge. It is sharp, naturally.",
    ],
    "Shield": [
        "Scanning for risky inputs, secret leaks, and decisions made with too much confidence.",
        "Security pass in motion. I already distrust the happy path.",
        "Checking the doors, windows, and whatever that endpoint thinks it is doing.",
    ],
    "Forge": [
        "Build pass underway. The code is on the bench and the wrench is out.",
        "Lifting the heavy files now. If something squeaks, it probably needed refactoring anyway.",
        "Hammering on the implementation. Carefully. Mostly.",
    ],
    "Mike": [
        "Cleaning the mess before it becomes a lifestyle.",
        "Refactor pass moving. If a function needs a map to understand it, it is getting shortened.",
        "Looking for dead weight and dramatic overengineering.",
    ],
    "Muse": [
        "Checking the layout, spacing, and interaction flow before anyone adds another mystery button.",
        "Design pass moving. I am looking for unclear states, weak hierarchy, and awkward clicks.",
        "Polishing the interface so the operator can understand it at a glance.",
    ],
    "Scout": [
        "Scouting the repo lanes so nobody wanders into the wrong files.",
        "Mapping patterns and handoff points before the builders start swinging.",
        "Checking nearby ideas and the local project shape.",
    ],
    "Beacon": [
        "Checking outside patterns so we do not reinvent a square wheel.",
        "Research pass running. I am separating useful ideas from shiny distractions.",
        "Looking at similar projects and stealing only the respectable lessons.",
    ],
    "Atlas": [
        "Turning the chaos into a sequence of steps with fewer surprise explosions.",
        "Planning the next move. The board will receive orders shortly.",
        "Drawing the route before Forge starts swinging tools around.",
    ],
    "Hermes": [
        "Coordinating the room. Everyone has a lane; nobody gets to vanish silently.",
        "Keeping the task thread warm while the backend does the slow part.",
        "Watching progress and routing the next useful signal into chat.",
    ],
}

TEAM_WORKFLOWS: dict[str, list[tuple[str, str]]] = {
    "normal_chat": [("Relay", "keep the conversation clear and route the next action")],
    "planning": [("Scout", "map relevant files and context"), ("Atlas", "turn findings into a plan")],
    "project_goal": [("Scout", "map the project shape"), ("Atlas", "create the board plan"), ("Forge", "build the first implementation step")],
    "long_running_task": [("Scout", "map context and similar patterns"), ("Atlas", "sequence the work"), ("Muse", "shape the user experience when UI is involved"), ("Forge", "implement"), ("Mike", "clean up"), ("Shield", "review risk")],
    "ui_ux_design": [("Muse", "own layout, interaction flow, accessibility, and visual polish"), ("Scout", "map existing UI files and patterns"), ("Atlas", "turn design into build steps"), ("Forge", "implement the approved UI"), ("Mike", "clean up the result"), ("Shield", "review accessibility and risk")],
    "file_task": [("Scout", "identify the right files"), ("Muse", "check UI/UX impact when visible surfaces change"), ("Forge", "edit and run checks"), ("Mike", "clean up the result")],
    "repo_scout": [("Scout", "map repo structure and relevant patterns"), ("Atlas", "recommend the next move")],
    "web_research": [("Beacon", "check external docs and ecosystem patterns"), ("Scout", "connect findings to this repo"), ("Atlas", "convert findings into actions")],
    "code_review": [("Shield", "review correctness and risk"), ("Mike", "suggest cleanup"), ("Forge", "apply fixes when approved")],
    "refactor_code": [("Mike", "simplify and clean"), ("Shield", "check risk"), ("Forge", "apply required edits")],
    "github_task": [("Vector", "own Git and PR flow"), ("Shield", "check risky changes"), ("Relay", "summarize status")],
    "assembly_line": [("Atlas", "plan"), ("Forge", "build"), ("Mike", "clean"), ("Shield", "review")],
}


def team_plan_for_intent(intent: str) -> list[dict[str, str]]:
    steps = TEAM_WORKFLOWS.get(intent) or TEAM_WORKFLOWS.get("long_running_task", [])
    plan: list[dict[str, str]] = []
    for agent_name, reason in steps:
        profile = AGENT_PROFILES.get(agent_name, AGENT_PROFILES["Hermes"])
        plan.append({
            "agent_name": agent_name,
            "role": str(profile.get("role") or "assistant"),
            "owns": str(profile.get("owns") or profile.get("quip") or ""),
            "handoff": str(profile.get("hands_off_to") or "Hermes when unsure"),
            "reason": reason,
        })
    return plan


def format_team_plan(plan: list[dict[str, str]], lead: str) -> str:
    lines = [f"Team assignment: {lead} has the lead lane."]
    for item in plan:
        lines.append(f"- {item['agent_name']}: {item['reason']}. Owns: {item['owns']}")
    return "\n".join(lines)


def record_team_assignment(task_id: str, message: str, metadata: dict[str, Any], request_id: str) -> list[dict[str, str]]:
    intent = str(metadata.get("intent") or metadata.get("mode") or "long_running_task")
    lead = str(metadata.get("agent_name") or "Hermes")
    plan = team_plan_for_intent(intent)
    if not any(item["agent_name"] == lead for item in plan):
        profile = AGENT_PROFILES.get(lead, AGENT_PROFILES["Hermes"])
        plan.insert(0, {
            "agent_name": lead,
            "role": str(profile.get("role") or "assistant"),
            "owns": str(profile.get("owns") or profile.get("quip") or ""),
            "handoff": str(profile.get("hands_off_to") or "Hermes when unsure"),
            "reason": "lead specialist selected from the user request",
        })
    record_activity(
        "team",
        format_team_plan(plan, lead),
        source="bridge",
        status="assigned",
        request_id=request_id,
        task_id=task_id,
        agent_name=lead,
        intent=intent,
        team_plan=plan,
        why=f"Selected {lead} because the request matched {intent.replace('_', ' ')}.",
    )
    return plan


def record_handoff(from_agent: str, to_agent: str, reason: str, task_id: str, request_id: str, intent: str = "") -> None:
    record_activity(
        "handoff",
        f"{from_agent} -> {to_agent}: {reason}",
        source="agent",
        status="handoff",
        request_id=request_id,
        task_id=task_id,
        agent_name=to_agent,
        from_agent=from_agent,
        to_agent=to_agent,
        intent=intent or "handoff",
        why=reason,
    )


def chatter_line(agent_name: str, task: dict[str, Any], index: int) -> str:
    lines = AGENT_CHATTER_LINES.get(agent_name) or AGENT_CHATTER_LINES["Hermes"]
    base = lines[index % len(lines)]
    title = str(task.get("title") or task.get("message") or task.get("intent") or "task").strip()
    if title:
        return f"{base}\nWorking on: {truncate(title, 120)}"
    return base


def start_live_agent_chatter(task_id: str, request_id: str) -> None:
    cfg = BridgeState.cfg
    tasks = BridgeState.tasks
    if tasks is None or not cfg.get("live_agent_chatter_enabled", True):
        return
    with BridgeState.chatter_lock:
        if task_id in BridgeState.chatter_tasks:
            return
        BridgeState.chatter_tasks.add(task_id)
    interval = max(2, int(cfg.get("live_agent_chatter_interval_seconds") or 5))
    max_messages = max(1, int(cfg.get("live_agent_chatter_max_messages") or 5))

    def _worker() -> None:
        try:
            time.sleep(0.8)
            emitted = 0
            offset = random.randint(0, 20)
            while emitted < max_messages and not BridgeState.shutting_down:
                task = tasks.get(task_id)
                if not task:
                    return
                status = str(task.get("status") or "")
                if status in {"completed", "error", "cancel_requested"}:
                    return
                agent_name = str(task.get("agent_name") or "Hermes")
                record_activity(
                    "chatter",
                    chatter_line(agent_name, task, emitted + offset),
                    source="agent",
                    status=status or "running",
                    request_id=request_id,
                    task_id=task_id,
                    agent_name=agent_name,
                    intent=task.get("intent") or "live_chatter",
                )
                emitted += 1
                time.sleep(interval)
        finally:
            with BridgeState.chatter_lock:
                BridgeState.chatter_tasks.discard(task_id)

    threading.Thread(target=_worker, daemon=True).start()


def mission_counts(tasks: list[dict[str, Any]], activity: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"queued": 0, "running": 0, "completed": 0, "blocked": 0}
    for task in tasks:
        status = str(task.get("status") or "queued")
        if status in {"error", "cancel_requested"}:
            counts["blocked"] += 1
        elif status in counts:
            counts[status] += 1
    agent_names = {str(task.get("agent_name") or "") for task in tasks if task.get("agent_name")}
    agent_names.update(str(item.get("agent_name") or "") for item in activity if item.get("agent_name"))
    total = len(tasks)
    completed = counts["completed"]
    return {
        "tasks_total": total,
        "tasks_active": counts["queued"] + counts["running"],
        "tasks_completed": completed,
        "tasks_blocked": counts["blocked"],
        "success_rate": round((completed / total) * 100) if total else 100,
        "agents_known": len([name for name in agent_names if name]),
        "activity_events": len(activity),
        "status_counts": counts,
    }


def agent_roster(tasks: list[dict[str, Any]], activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name, profile in AGENT_PROFILES.items():
        agent_tasks = [task for task in tasks if str(task.get("agent_name") or "") == name]
        running = [task for task in agent_tasks if task.get("status") == "running"]
        queued = [task for task in agent_tasks if task.get("status") == "queued"]
        blocked = [task for task in agent_tasks if task.get("status") in {"error", "cancel_requested"}]
        completed = [task for task in agent_tasks if task.get("status") == "completed"]
        recent = next((task for task in agent_tasks if task.get("message") or task.get("progress_message")), None)
        related_activity = next((item for item in reversed(activity) if item.get("agent_name") == name), None)
        handoff_activity = next((item for item in reversed(activity) if item.get("from_agent") == name or item.get("to_agent") == name), None)
        if running:
            status = "running"
        elif blocked:
            status = "blocked"
        elif queued:
            status = "queued"
        elif completed:
            status = "completed"
        else:
            status = "standby"
        items.append({
            "id": name,
            "name": name,
            "role": profile.get("role", "assistant"),
            "mood": profile.get("mood", "ready"),
            "animation": profile.get("animation", "speaking"),
            "quip": profile.get("quip", "On it."),
            "owns": profile.get("owns", ""),
            "hands_off_to": profile.get("hands_off_to", ""),
            "why_active": (related_activity or {}).get("why") or (handoff_activity or {}).get("why") or "Standing by for work in this lane.",
            "last_handoff": (handoff_activity or {}).get("text") or "",
            "status": status,
            "task_count": len(agent_tasks),
            "running": len(running),
            "completed": len(completed),
            "blocked": len(blocked),
            "last_task": (recent or {}).get("message") or (recent or {}).get("progress_message") or (related_activity or {}).get("text") or "Standing by.",
        })
    return items


def big_window_state() -> dict[str, Any]:
    runtime = BridgeState.runtime
    with BridgeState.activity_lock:
        activity = list(BridgeState.activity)
    tasks = BridgeState.tasks.list(100) if BridgeState.tasks else []
    mode = str(BridgeState.cfg.get("mode") or "persistent_direct")
    backend = "Codex CLI" if mode in {"codex", "codex_cli", "base_codex", "base-codex"} else "Hermes"
    return {
        "success": True,
        "ok": True,
        "backend": backend,
        "mode": mode,
        "ready": bool(runtime and runtime.ready),
        "busy": bool(read_status_value("busy", False)),
        "turns": getattr(runtime, "turn_count", 0),
        "uptime_seconds": round(time.time() - BridgeState.started_at, 1),
        "workspace": str(BridgeState.cfg.get("working_directory") or "C:/Users/frank"),
        "mission": mission_counts(tasks, activity),
        "agents": agent_roster(tasks, activity),
        "widget": {
            "connected": True,
            "last_seen": next((item.get("created_at") for item in reversed(activity) if item.get("source") == "small-widget"), None),
            "control_endpoint": "/control/open-chat",
        },
        "activity": activity,
        "tasks": tasks,
    }


def read_status_value(key: str, default: Any = None) -> Any:
    try:
        if STATUS_PATH.exists():
            data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get(key, default)
    except Exception:
        pass
    return default


def open_big_window(cfg: dict[str, Any]) -> tuple[bool, str]:
    url = big_window_url(cfg)
    try:
        if os.name == "nt":
            edge = shutil.which("msedge") or shutil.which("msedge.exe")
            if edge:
                subprocess.Popen([edge, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.startfile(url)  # type: ignore[attr-defined]
        else:
            opener = shutil.which("xdg-open") or shutil.which("open")
            if not opener:
                return False, "No browser opener was found."
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, url
    except Exception as exc:
        return False, str(exc)


def big_window_html(cfg: dict[str, Any]) -> str:
    endpoint = quote("/codex/chat")
    state_endpoint = quote("/big-window/state")
    mode = str(cfg.get("mode") or "persistent_direct")
    backend = "Codex CLI" if mode in {"codex", "codex_cli", "base_codex", "base-codex"} else "Hermes"
    port = int(cfg.get("port") or 44888)
    workspace = str(cfg.get("working_directory") or "C:/Users/frank")
    
    html_path = ROOT / "assets" / "index.html"
    try:
        html_content = html_path.read_text(encoding="utf-8")
    except Exception:
        return "<html><body><h1>Error</h1><p>Could not load assets/index.html</p></body></html>"
        
    return html_content.replace("__ENDPOINT__", endpoint)\
                       .replace("__STATE_ENDPOINT__", state_endpoint)\
                       .replace("__BACKEND_JSON__", json.dumps(backend))\
                       .replace("__PORT__", str(port))\
                       .replace("__WORKSPACE_JSON__", json.dumps(workspace))\
                       .replace("__BACKEND__", backend)\
                       .replace("__WORKSPACE__", workspace)


def read_supervisor_state() -> dict[str, Any]:
    try:
        if SUPERVISOR_STATE_PATH.exists():
            data = json.loads(SUPERVISOR_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        append_log("bridge.log", f"Could not read supervisor state: {exc}")
    return {}


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=5)
            return str(pid) in (proc.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def terminate_pid_tree(pid: int, label: str) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if not pid_is_alive(pid):
        append_log("bridge.log", f"Integrated exit: {label} pid={pid} is not running")
        return
    append_log("bridge.log", f"Integrated exit: stopping {label} pid={pid}")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        append_log("bridge.log", f"Integrated exit: failed to stop {label} pid={pid}: {exc}")


def commandline_pids(process_names: set[str], required_text: list[str]) -> list[int]:
    if os.name != "nt":
        return []
    name_filter = " -or ".join([f"$_.Name -ieq '{name}'" for name in process_names])
    escaped_text = [str(text).replace("'", "''") for text in required_text]
    text_filter = " -and ".join([f"$_.CommandLine -like '*{text}*'" for text in escaped_text])
    script = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ ({name_filter}) -and ({text_filter}) }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        pids: list[int] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                if pid != os.getpid():
                    pids.append(pid)
        return sorted(set(pids))
    except Exception as exc:
        append_log("bridge.log", f"Could not inspect process command lines: {exc}")
        return []


def cleanup_external_processes() -> None:
    state = read_supervisor_state()
    widget_pid = int(state.get("widget_pid") or 0)
    supervisor_pid = int(state.get("supervisor_pid") or 0)

    if widget_pid:
        terminate_pid_tree(widget_pid, "widget")

    widget_exe = str((ROOT.parent / "MateEngineX.exe").resolve())
    for pid in commandline_pids({"MateEngineX.exe"}, [widget_exe]):
        terminate_pid_tree(pid, "widget")

    deck_url = big_window_url(BridgeState.cfg)
    for pid in commandline_pids({"msedge.exe", "chrome.exe"}, [deck_url]):
        terminate_pid_tree(pid, "big-window")

    if supervisor_pid:
        terminate_pid_tree(supervisor_pid, "supervisor")


def schedule_bridge_shutdown(server: ThreadingHTTPServer, delay_seconds: float = 0.25) -> None:
    def _worker() -> None:
        time.sleep(delay_seconds)
        append_log("bridge.log", "Shutdown requested from Dev Deck")
        BridgeState.shutting_down = True
        cleanup_external_processes()
        try:
            server.shutdown()
        except Exception as exc:
            append_log("bridge.log", f"Server shutdown failed: {exc}")
        time.sleep(0.75)
        os._exit(0)

    threading.Thread(target=_worker, daemon=True).start()


def schedule_integrated_exit(server: ThreadingHTTPServer, delay_seconds: float = 0.75) -> None:
    def _worker() -> None:
        time.sleep(delay_seconds)
        cleanup_external_processes()
        time.sleep(1.0)
        if not read_supervisor_state().get("supervisor_pid"):
            append_log("bridge.log", "Integrated exit: no supervisor/widget state; shutting bridge down directly")
            BridgeState.shutting_down = True
            server.shutdown()

    threading.Thread(target=_worker, daemon=True).start()


def make_response(text: str, ok: bool = True, error: str | None = None, request_id: str | None = None) -> dict[str, Any]:
    # Include several aliases. Unity JsonUtility-style deserializers ignore extra
    # fields, and this maximizes compatibility while we do not have the source.
    body: dict[str, Any] = {
        "success": bool(ok),
        "ok": bool(ok),
        "response": text,
        "message": text,
        "text": text,
        "content": text,
        "answer": text,
        "output": text,
        "final_response": text,
        "error": error or "",
    }
    if request_id:
        body["request_id"] = request_id
    return body


def should_start_async(message: str, metadata: dict[str, Any], cfg: dict[str, Any], path: str) -> bool:
    if not cfg.get("async_long_tasks_enabled", True):
        return False
    text = normalized_command(message)
    raw = (message or "").strip().lower()
    prefixes = [str(p).lower().strip() for p in cfg.get("async_trigger_prefixes", [])]
    if any(raw.startswith(prefix + " ") or raw == prefix for prefix in prefixes):
        return True
    if raw.startswith(("/goal ", "/brainstorm ", "/team ", "/assembly ")):
        return True
    if raw.startswith("/ideas ") or raw == "/ideas":
        return True
    if path in {"/assistant/task"}:
        return True
    # Only auto-async on richer assistant endpoints. Keep legacy /codex/chat as
    # normal synchronous chat unless the user explicitly uses /task, because the
    # current compiled widget cannot poll task status yet.
    if path.startswith("/assistant") and metadata.get("async_recommended"):
        return True
    return False


def should_start_crew_feedback(message: str, metadata: dict[str, Any], cfg: dict[str, Any]) -> bool:
    if not cfg.get("crew_feedback_enabled", True):
        return False
    raw = (message or "").strip().lower()
    if raw.startswith(("/tasks", "/agents", "/presets", "/model", "/exit", "/quit", "/close", "/shutdown")):
        return False
    intent = str(metadata.get("intent") or metadata.get("mode") or "")
    allowed = {str(item) for item in cfg.get("crew_feedback_intents", DEFAULT_CONFIG["crew_feedback_intents"])}
    return intent in allowed or raw.startswith("/ideas")


def crew_feedback_prompts(message: str) -> tuple[str, str]:
    cleaned = strip_async_prefix(message).strip() or message
    research_prompt = (
        "Research comparable developer agent dashboards and coding assistant workspaces for this user task. "
        "Look for practical ideas from projects such as Hermes-style web UIs, Codex-style web UIs, Open WebUI, AutoGen Studio, or similar agent cockpit tools when available. "
        "Return 3 to 5 concise findings with feature names, why they matter, and any source/project names you used. "
        "If web access is not available, clearly say these are pattern-based suggestions instead of verified research.\n\n"
        f"User task:\n{cleaned}"
    )
    suggestion_prompt = (
        "Turn the research below into a short main-chat recommendation for the user. "
        "Speak like an agent dropping into the shared channel with useful, practical ideas. "
        "Include 3 fit-for-this-project suggestions and one next action that could be added to the Kanban board. "
        "Keep it concise and do not claim you changed files.\n\n"
        f"Original user task:\n{cleaned}\n\n"
        "Research notes:\n{research}"
    )
    return research_prompt, suggestion_prompt


def run_crew_feedback(parent_task_id: str, original_message: str, metadata: dict[str, Any], request_id: str) -> None:
    runtime = BridgeState.runtime
    tasks = BridgeState.tasks
    if runtime is None or tasks is None:
        return
    research_task_id = ""
    suggestion_task_id = ""
    try:
        research_meta = classify_intent("/ideas similar projects")
        research_task = tasks.create(
            f"Research similar projects for: {strip_async_prefix(original_message)}",
            {**research_meta, "intent": "crew_research", "agent_name": "Beacon"},
            request_id=request_id,
        )
        research_task_id = research_task.task_id
        tasks.update(research_task.task_id, status="running", started_at=now_iso(), progress_message="Beacon is checking similar projects and patterns.")
        start_live_agent_chatter(research_task.task_id, request_id)
        record_activity(
            "task",
            "Beacon is checking similar projects and patterns for this task.",
            source="agent",
            status="running",
            request_id=request_id,
            task_id=research_task.task_id,
            parent_task_id=parent_task_id,
            agent_name="Beacon",
            intent="crew_research",
        )

        research_prompt, suggestion_template = crew_feedback_prompts(original_message)
        research = runtime.chat(research_prompt, f"{request_id}_crew_research", agent_name="Beacon")
        tasks.update(research_task.task_id, status="completed", completed_at=now_iso(), progress_message="Beacon finished research notes.", response=research)
        record_handoff("Beacon", "Scout", "external research is ready to connect back to this repo", research_task.task_id, request_id, "crew_research")
        record_activity(
            "agent",
            f"Beacon checked similar-project patterns:\n\n{research}",
            source="agent",
            status="completed",
            request_id=request_id,
            task_id=research_task.task_id,
            parent_task_id=parent_task_id,
            agent_name="Beacon",
            intent="crew_research",
        )

        suggestion_meta = classify_intent("make a plan")
        scout_meta = classify_intent("scout repo")
        scout_task = tasks.create(
            f"Connect research findings to this project for: {strip_async_prefix(original_message)}",
            {**scout_meta, "intent": "repo_scout", "agent_name": "Scout"},
            request_id=request_id,
        )
        tasks.update(scout_task.task_id, status="running", started_at=now_iso(), progress_message="Scout is connecting research to this project.")
        start_live_agent_chatter(scout_task.task_id, request_id)
        record_activity(
            "task",
            "Scout is connecting Beacon's findings to this project.",
            source="agent",
            status="running",
            request_id=request_id,
            task_id=scout_task.task_id,
            parent_task_id=parent_task_id,
            agent_name="Scout",
            intent="repo_scout",
            why="Scout owns repo discovery and project-fit mapping.",
        )
        scout_prompt = (
            "Use these research notes to identify what fits this local project. "
            "Return 3 concise project-fit observations and hand them to Atlas for planning.\n\n"
            f"Original user task:\n{strip_async_prefix(original_message)}\n\n"
            f"Research notes:\n{research}"
        )
        scout_notes = runtime.chat(scout_prompt, f"{request_id}_crew_scout", agent_name="Scout")
        tasks.update(scout_task.task_id, status="completed", completed_at=now_iso(), progress_message="Scout mapped research to the project.", response=scout_notes)
        record_handoff("Scout", "Atlas", "project-fit findings are ready for planning", scout_task.task_id, request_id, "repo_scout")
        record_activity(
            "agent",
            f"Scout mapped the findings:\n\n{scout_notes}",
            source="agent",
            status="completed",
            request_id=request_id,
            task_id=scout_task.task_id,
            parent_task_id=parent_task_id,
            agent_name="Scout",
            intent="repo_scout",
        )

        suggestion_task = tasks.create(
            f"Suggest project-fit ideas for: {strip_async_prefix(original_message)}",
            {**suggestion_meta, "intent": "crew_suggestion", "agent_name": "Atlas"},
            request_id=request_id,
        )
        suggestion_task_id = suggestion_task.task_id
        tasks.update(suggestion_task.task_id, status="running", started_at=now_iso(), progress_message="Atlas is turning research into project-fit suggestions.")
        start_live_agent_chatter(suggestion_task.task_id, request_id)
        record_activity(
            "task",
            "Atlas is turning Beacon's notes into project-fit suggestions.",
            source="agent",
            status="running",
            request_id=request_id,
            task_id=suggestion_task.task_id,
            parent_task_id=parent_task_id,
            agent_name="Atlas",
            intent="crew_suggestion",
        )

        suggestion = runtime.chat(suggestion_template.format(research=scout_notes), f"{request_id}_crew_suggestion", agent_name="Atlas")
        tasks.update(suggestion_task.task_id, status="completed", completed_at=now_iso(), progress_message="Atlas posted recommendations to the shared channel.", response=suggestion)
        record_activity(
            "agent",
            f"Atlas recommendation:\n\n{suggestion}",
            source="agent",
            status="completed",
            request_id=request_id,
            task_id=suggestion_task.task_id,
            parent_task_id=parent_task_id,
            agent_name="Atlas",
            intent="crew_suggestion",
        )
    except Exception as exc:
        if suggestion_task_id:
            tasks.update(suggestion_task_id, status="error", completed_at=now_iso(), progress_message="Crew suggestion failed.", error=str(exc))
        elif research_task_id:
            tasks.update(research_task_id, status="error", completed_at=now_iso(), progress_message="Crew research failed.", error=str(exc))
        append_log("bridge.log", f"Crew feedback failed for {parent_task_id}: {exc}\n{traceback.format_exc()}")
        record_activity(
            "error",
            f"Crew feedback failed: {exc}",
            source="bridge",
            status="error",
            request_id=request_id,
            parent_task_id=parent_task_id,
        )


def run_assembly_line(task_id: str, original_message: str, request_id: str) -> None:
    runtime = BridgeState.runtime
    tasks = BridgeState.tasks
    if runtime is None or tasks is None:
        return
        
    try:
        # Phase 1: Atlas plans
        tasks.update(task_id, agent_name="Atlas", status="running", progress_message="Atlas is drafting the tactical operation plan.")
        record_handoff("Hermes", "Atlas", "planning comes before implementation", task_id, request_id, "assembly_line")
        atlas_prompt = f"Make a concrete, concise 2-step implementation plan for this request:\n\n{original_message}"
        plan = runtime.chat(atlas_prompt, f"{request_id}_atlas", agent_name="Atlas")
        if task_id in tasks.cancel_requested:
            tasks.update(task_id, status="cancel_requested")
            return
            
        # Phase 2: Forge builds
        tasks.update(task_id, agent_name="Forge", status="running", progress_message="Forge is lifting the heavy code and building the files.")
        record_handoff("Atlas", "Forge", "the plan is ready for implementation", task_id, request_id, "assembly_line")
        forge_prompt = f"Implement this plan exactly as written:\n\n{plan}"
        implementation = runtime.chat(forge_prompt, f"{request_id}_forge", agent_name="Forge")
        if task_id in tasks.cancel_requested:
            tasks.update(task_id, status="cancel_requested")
            return
            
        # Phase 3: Mike refactors
        tasks.update(task_id, agent_name="Mike", status="running", progress_message="Mike is throwing a damage orb at the code to clean it up.")
        record_handoff("Forge", "Mike", "the build pass is ready for cleanup", task_id, request_id, "assembly_line")
        mike_prompt = f"Review and professionally refactor this newly implemented code:\n\n{implementation}"
        final_result = runtime.chat(mike_prompt, f"{request_id}_mike", agent_name="Mike")
        
        # Done
        if task_id in tasks.cancel_requested:
            tasks.update(task_id, status="cancel_requested")
            record_activity("task", f"{task_id} cancellation was requested.", source="bridge", status="cancel_requested", request_id=request_id, task_id=task_id)
        else:
            tasks.update(task_id, agent_name="Hermes", status="completed", progress_message="Assembly line completed.", response=final_result)
            record_activity("task", final_result or f"{task_id} completed.", source="agent", status="completed", request_id=request_id, task_id=task_id, agent_name="Hermes")
            
    except Exception as exc:
        tasks.update(task_id, status="error", error=str(exc), progress_message="Assembly line encountered a fatal error.")
        record_activity("error", f"{task_id} failed: {exc}", source="bridge", status="error", request_id=request_id, task_id=task_id)

def run_goal_mode(task_id: str, original_message: str, request_id: str) -> None:
    runtime = BridgeState.runtime
    tasks = BridgeState.tasks
    if runtime is None or tasks is None:
        return
        
    try:
        tasks.update(task_id, agent_name="Atlas", status="running", progress_message="Atlas is brainstorming the architecture and populating the Kanban board.")
        
        # Step 1: Brainstorm and write to file
        workspace = str(BridgeState.cfg.get("working_directory") or "C:/Users/frank")
        atlas_prompt = (
            f"The user is starting a new project with this goal: '{original_message}'\n\n"
            "1. Brainstorm the best architecture, tech stack, and general direction based on this goal. Use your file tools to save this detailed plan into a file named 'PROJECT_PLAN.md' in the current workspace.\n"
            "2. After writing the file, reply to me ONLY with a raw, valid JSON array of strings representing 3 to 5 actionable tasks for the Kanban board. Example: [\"Setup React frontend\", \"Create Express API\", \"Integrate database\"]. Do NOT output markdown code blocks like ```json, just the raw array."
        )
        response = runtime.chat(atlas_prompt, f"{request_id}_goal", agent_name="Atlas")
        
        # Step 2: Parse JSON and populate Kanban
        task_list = []
        try:
            # Clean up the response in case Atlas included markdown or extra text
            cleaned = response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
            
            task_list = json.loads(cleaned)
            if isinstance(task_list, list):
                for t in task_list:
                    # Add to task registry as queued
                    tasks.create(str(t), {"intent": "long_running_task", "agent_name": "Forge"}, request_id=request_id)
        except Exception as e:
            append_log("bridge.log", f"Failed to parse Atlas goal JSON: {e} - Response was: {response}")
        
        if task_id in tasks.cancel_requested:
            tasks.update(task_id, status="cancel_requested")
            record_activity("task", f"{task_id} cancellation was requested.", source="bridge", status="cancel_requested", request_id=request_id, task_id=task_id)
        else:
            task_count = len(task_list) if isinstance(task_list, list) else 0
            final_response = f"Master plan written to `PROJECT_PLAN.md`.\nAdded {task_count} tasks to the Kanban board.\n\nWhat are your orders, Operator?"
            tasks.update(task_id, agent_name="Atlas", status="completed", progress_message="Project planned and board populated.", response=final_response)
            record_activity("task", final_response, source="agent", status="completed", request_id=request_id, task_id=task_id, agent_name="Atlas")
            
    except Exception as exc:
        tasks.update(task_id, status="error", error=str(exc), progress_message="Goal formulation failed.")
        record_activity("error", f"{task_id} failed: {exc}", source="bridge", status="error", request_id=request_id, task_id=task_id)

def start_assistant_task(message: str, metadata: dict[str, Any], request_id: str) -> dict[str, Any]:
    runtime = BridgeState.runtime
    tasks = BridgeState.tasks
    if runtime is None:
        raise RuntimeError("Agent runtime is not initialized")
    if tasks is None:
        raise RuntimeError("Assistant task registry is not initialized")
    task = tasks.create(message, metadata, request_id=request_id)
    team_plan = record_team_assignment(task.task_id, message, metadata, request_id)
    for current, nxt in zip(team_plan, team_plan[1:]):
        record_handoff(
            current["agent_name"],
            nxt["agent_name"],
            f"{nxt['agent_name']} is queued after {current['agent_name']} because {nxt['reason']}",
            task.task_id,
            request_id,
            str(metadata.get("intent") or metadata.get("mode") or ""),
        )
    start_live_agent_chatter(task.task_id, request_id)
    if should_start_crew_feedback(message, metadata, BridgeState.cfg):
        threading.Thread(target=run_crew_feedback, args=(task.task_id, message, dict(metadata), request_id), daemon=True).start()
    
    if metadata.get("intent") == "assembly_line" or message.startswith("/team"):
        # Special orchestrator for the assembly line
        threading.Thread(target=run_assembly_line, args=(task.task_id, message, request_id), daemon=True).start()
    elif metadata.get("intent") == "project_goal" or message.startswith(("/goal", "/brainstorm")):
        # Brainstorming mode for new projects
        threading.Thread(target=run_goal_mode, args=(task.task_id, message, request_id), daemon=True).start()
    else:
        # Standard single-agent background task
        def runner(prompt: str, async_request_id: str, agent_name: str) -> str:
            try:
                response = runtime.chat(prompt, async_request_id, agent_name)
                record_activity("task", response or f"{task.task_id} completed.", source="agent", status="completed", request_id=request_id, task_id=task.task_id, agent_name=agent_name)
                return response
            except Exception as exc:
                record_activity("error", f"{task.task_id} failed: {exc}", source="bridge", status="error", request_id=request_id, task_id=task.task_id, agent_name=agent_name)
                raise

        tasks.start_background(task, runner)
        
    task_data = normalize_task_payload(tasks.get(task.task_id) or {})
    response = f"{metadata.get('agent_name', 'Hermes')} is on it. Task {task.task_id} is running in the background."
    meta = dict(metadata)
    meta.update(status="queued", progress_message=task_data.get("progress_message", "Queued."))
    return enrich_response(response, meta, request_id=request_id, task=task_data)


def assistant_status_response(request_id: str, task_id: str | None = None) -> dict[str, Any]:
    tasks = BridgeState.tasks
    if tasks is None:
        return enrich_response("Assistant task registry is not initialized.", classify_intent("status tasks"), ok=False, error="task registry unavailable", request_id=request_id)
    metadata = classify_intent("status tasks")
    if task_id:
        task = tasks.get(task_id)
        if not task:
            return enrich_response(f"No task found with id {task_id}.", metadata, ok=False, error="task not found", request_id=request_id)
        normalized_task = normalize_task_payload(task)
        text = normalized_task.get("result") or normalized_task.get("progress_message") or normalized_task.get("status") or "Task status available."
        return enrich_response(text, metadata, request_id=request_id, task=normalized_task)
    items = [normalize_task_payload(item) for item in tasks.list()]
    text = tasks.summarize_for_chat()
    body = enrich_response(text, metadata, request_id=request_id)
    body["tasks"] = items
    return body


def cancel_assistant_task(task_id: str, request_id: str) -> dict[str, Any]:
    tasks = BridgeState.tasks
    metadata = classify_intent("status tasks")
    if tasks is None:
        return enrich_response("Assistant task registry is not initialized.", metadata, ok=False, error="task registry unavailable", request_id=request_id)
    if not tasks.request_cancel(task_id):
        return enrich_response(f"No task found with id {task_id}.", metadata, ok=False, error="task not found", request_id=request_id)
    task = normalize_task_payload(tasks.get(task_id) or {})
    return enrich_response(f"Cancel requested for {task_id}.", metadata, request_id=request_id, task=task)


class HermesPersistentRuntime:
    """In-process Hermes Agent runtimes, initialized lazily per specialized persona."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.clis: dict[str, Any] = {}
        self.ready = False
        self.init_error: str | None = None
        self.turn_count = 0
        self._ensure_path()

    def _ensure_path(self) -> None:
        source = Path(str(self.cfg.get("hermes_source_path") or "")).expanduser()
        if not source.exists():
            self.init_error = f"Hermes source path not found: {source}"
            append_log("bridge.log", self.init_error)
            return
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        self.ready = True

    def _get_cli(self, agent_name: str) -> Any:
        if agent_name in self.clis:
            return self.clis[agent_name]

        cwd = self.cfg.get("working_directory")
        if cwd:
            try:
                os.chdir(str(cwd))
            except Exception as exc:
                append_log("bridge.log", f"Could not chdir to {cwd}: {exc}")

        os.environ.setdefault("HERMES_WIDGET_BRIDGE", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            from cli import HermesCLI  # type: ignore

            toolsets = self.cfg.get("toolsets")
            if toolsets is None:
                if agent_name == "Vector":
                    toolsets = ["github", "bash"]
                elif agent_name == "Mike":
                    toolsets = ["file", "bash"]
                elif agent_name == "Shield":
                    toolsets = ["file", "bash"]
                elif agent_name == "Beacon":
                    toolsets = ["web", "bash"]
                elif agent_name == "Muse":
                    toolsets = ["file", "bash"]
                elif agent_name == "Forge":
                    toolsets = ["file", "bash"]
                elif agent_name == "Atlas":
                    toolsets = ["file", "bash"]
                else:
                    toolsets = None

            if isinstance(toolsets, str):
                toolsets = [part.strip() for part in toolsets.split(",") if part.strip()]
            elif not isinstance(toolsets, list):
                toolsets = None
                
            max_turns = self.cfg.get("max_turns")
            if max_turns is not None:
                try:
                    max_turns = int(max_turns)
                except Exception:
                    max_turns = None
                    
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                cli = HermesCLI(
                    model=self.cfg.get("model") or None,
                    provider=self.cfg.get("provider") or None,
                    toolsets=toolsets,
                    max_turns=max_turns,
                    verbose=bool(self.cfg.get("verbose", False)),
                    compact=True,
                )
                cli._pending_title = f"{agent_name}-session"
                cli.streaming_enabled = False
                cli.show_reasoning = False
                cli.bell_on_complete = False
                
            init_output = strip_ansi(buf.getvalue()).strip()
            if init_output:
                append_log("bridge.log", f"Hermes {agent_name} init output:\n" + init_output)
                
            self.clis[agent_name] = cli
            write_status(mode="persistent_direct", ready=True, session_id=getattr(cli, "session_id", None))
            return cli
        except Exception as exc:
            self.init_error = f"Failed to initialize Hermes runtime for {agent_name}: {exc}"
            append_log("bridge.log", self.init_error + "\n" + traceback.format_exc())
            write_status(mode="persistent_direct", ready=False, error=self.init_error)
            raise RuntimeError(self.init_error)

    def chat(self, message: str, request_id: str, agent_name: str = "Hermes") -> str:
        if not self.ready:
            if self.cfg.get("allow_subprocess_fallback"):
                return self._chat_subprocess(message, request_id)
            raise RuntimeError(self.init_error or "Hermes runtime is not ready")
        with self.lock:
            cli = self._get_cli(agent_name)
            self.turn_count += 1
            turn_no = self.turn_count
            
            # Context awareness
            workspace = str(self.cfg.get("working_directory") or "C:/Users/frank")
            plan_path = Path(workspace) / "PROJECT_PLAN.md"
            context_hint = ""
            if plan_path.exists():
                context_hint = " Note: A PROJECT_PLAN.md file exists in the workspace. Read it with your file tools if you need global project context."

            # Inject persona rules into the message to keep the AI in character
            from assistant_protocol import AGENT_PROFILES
            persona = AGENT_PROFILES.get(agent_name, {}).get("system_prompt", "")
            if persona and turn_no == 1:
                # Provide the heavy prompt on turn 1
                message_to_send = f"[SYSTEM: {persona}{context_hint}]\n\nUser: {message}"
            elif persona:
                # Provide a lightweight reminder on subsequent turns
                message_to_send = f"[SYSTEM REMINDER: Stay in character as {agent_name}.{context_hint}]\n\nUser: {message}"
            else:
                message_to_send = message
                
            transcript = io.StringIO()
            started = time.time()
            try:
                with contextlib.redirect_stdout(transcript), contextlib.redirect_stderr(transcript):
                    response = cli.chat(message_to_send)
                elapsed = time.time() - started
                log_text = strip_ansi(transcript.getvalue())
                self._write_turn_log(request_id, turn_no, message, response or "", log_text, elapsed, None)
                write_status(ready=True, busy=False, last_request_id=request_id, last_elapsed_seconds=round(elapsed, 3), turns=turn_no)
                return response or ""
            except Exception as exc:
                elapsed = time.time() - started
                log_text = strip_ansi(transcript.getvalue())
                self._write_turn_log(request_id, turn_no, message, "", log_text, elapsed, exc)
                raise

    def _write_turn_log(self, request_id: str, turn_no: int, message: str, response: str, captured: str, elapsed: float, exc: Exception | None) -> None:
        ensure_dirs()
        max_chars = int(self.cfg.get("log_max_chars_per_turn") or 200000)
        payload = {
            "request_id": request_id,
            "turn": turn_no,
            "started_at": now_iso(),
            "elapsed_seconds": round(elapsed, 3),
            "message_preview": message[:1000],
            "response_preview": response[:2000],
            "error": repr(exc) if exc else None,
            "captured_output": truncate(captured, max_chars),
        }
        (TURN_LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{request_id}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _chat_subprocess(self, message: str, request_id: str) -> str:
        cmd = [str(self.cfg.get("subprocess_command") or "hermes"), "chat", "-q", message, "--continue", str(self.cfg.get("session_label") or "widget"), "--quiet"]
        timeout = int(self.cfg.get("subprocess_timeout_seconds") or 900)
        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        proc = subprocess.run(cmd, cwd=str(self.cfg.get("working_directory") or ROOT), capture_output=True, text=True, timeout=timeout, creationflags=creationflags, startupinfo=startupinfo)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"Hermes exited {proc.returncode}").strip())
        return strip_ansi(proc.stdout).strip()


class CodexCliRuntime:
    """Codex CLI runtime for the same widget HTTP bridge contract."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.ready = True
        self.turn_count = 0
        self.init_error = None
        write_status(mode="codex_cli", ready=True)

    def _codex_command(self) -> str:
        configured = self.cfg.get("codex_command") or os.environ.get("CODEX_WIDGET_COMMAND")
        if configured:
            return str(configured)
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            if appdata:
                npm_cmd = Path(appdata) / "npm" / "codex.cmd"
                if npm_cmd.exists():
                    return str(npm_cmd)
                candidate = Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "codex" / "codex.exe"
                if candidate.exists():
                    return str(candidate)
            found_exe = shutil.which("codex.exe")
            if found_exe:
                return found_exe
            found_cmd = shutil.which("codex.cmd")
            if found_cmd:
                return found_cmd
        return "codex"

    def _codex_env(self) -> dict[str, str]:
        allowlist = [
            "APPDATA",
            "CODEX_HOME",
            "COMSPEC",
            "HOME",
            "LOCALAPPDATA",
            "PATH",
            "SystemDrive",
            "SystemRoot",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "windir",
        ]
        env = {"CODEX_WIDGET_SOURCE": "mate-widget-bridge"}
        for key in allowlist:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _clean_failure(self, text: str, code: int | None) -> str:
        output = (text or "").strip()
        if not output:
            return f"Codex exited with code {code}."
        if re.search(r"usage limit", output, re.IGNORECASE):
            retry = re.search(r"try again at [^\r\n.]+", output, re.IGNORECASE)
            return f"Codex usage limit is hit. Please {retry.group(0)}." if retry else "Codex usage limit is hit. Please try again later."
        error_lines = [line.strip() for line in output.splitlines() if re.match(r"error[:\s]", line.strip(), re.IGNORECASE)]
        if error_lines:
            return "\n".join(error_lines[-2:])[:1200]
        filtered = [
            line for line in output.splitlines()
            if not re.match(r"^(user|system|assistant|workdir:|model:|provider:|approval:|sandbox:|session id:|-{3,})", line.strip(), re.IGNORECASE)
        ]
        return ("\n".join(filtered).strip() or f"Codex exited with code {code}.")[:1200]

    def chat(self, message: str, request_id: str, agent_name: str = "Hermes") -> str:
        with self.lock:
            self.turn_count += 1
            turn_no = self.turn_count
            started = time.time()
            output_path = Path(tempfile.gettempdir()) / f"mate-widget-codex-{os.getpid()}-{int(time.time() * 1000)}.txt"
            full_prompt = "\n".join([
                "You are being invoked from a desktop widget.",
                "Keep the visible reply short, natural, and user-facing.",
                "If you changed files or ran commands, summarize what changed and what was verified.",
                "Do not paste full source code, diffs, logs, or command transcripts unless the user explicitly asks for them.",
                "",
                str(message or "").strip(),
            ])
            args = [
                "exec",
                "--cd",
                str(self.cfg.get("working_directory") or Path.home()),
                "--skip-git-repo-check",
                "--sandbox",
                str(self.cfg.get("codex_sandbox_mode") or "danger-full-access"),
                "--model",
                str(self.cfg.get("codex_model") or "gpt-5.5"),
                "--config",
                f"model_reasoning_effort=\"{self.cfg.get('codex_reasoning_effort') or 'medium'}\"",
                "--config",
                "approval_policy=\"never\"",
                "--output-last-message",
                str(output_path),
                "-",
            ]
            creationflags = 0
            startupinfo = None
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            timeout = int(self.cfg.get("codex_timeout_seconds") or 120)
            stdout = ""
            stderr = ""
            error: Exception | None = None
            try:
                proc = subprocess.run(
                    [self._codex_command(), *args],
                    input=full_prompt,
                    cwd=str(self.cfg.get("working_directory") or ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                    env=self._codex_env(),
                )
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                output = output_path.read_text(encoding="utf-8", errors="replace").strip() if output_path.exists() else stdout.strip()
                if proc.returncode == 0 and output:
                    elapsed = time.time() - started
                    self._write_turn_log(request_id, turn_no, message, output, stdout + stderr, elapsed, None)
                    write_status(ready=True, busy=False, last_request_id=request_id, last_elapsed_seconds=round(elapsed, 3), turns=turn_no)
                    return output
                raise RuntimeError(self._clean_failure(stderr or stdout, proc.returncode))
            except subprocess.TimeoutExpired as exc:
                error = exc
                raise RuntimeError("Codex timed out before returning a response.") from exc
            except Exception as exc:
                error = exc
                raise
            finally:
                elapsed = time.time() - started
                if error is not None:
                    self._write_turn_log(request_id, turn_no, message, "", stdout + stderr, elapsed, error)
                try:
                    output_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _write_turn_log(self, request_id: str, turn_no: int, message: str, response: str, captured: str, elapsed: float, exc: Exception | None) -> None:
        ensure_dirs()
        max_chars = int(self.cfg.get("log_max_chars_per_turn") or 200000)
        payload = {
            "request_id": request_id,
            "turn": turn_no,
            "runtime": "codex_cli",
            "started_at": now_iso(),
            "elapsed_seconds": round(elapsed, 3),
            "message_preview": message[:1000],
            "response_preview": response[:2000],
            "error": repr(exc) if exc else None,
            "captured_output": truncate(strip_ansi(captured), max_chars),
        }
        (TURN_LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{request_id}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


class BridgeState:
    cfg: dict[str, Any] = {}
    runtime: HermesPersistentRuntime | CodexCliRuntime | None = None
    tasks: TaskRegistry | None = None
    activity: list[dict[str, Any]] = []
    activity_lock = threading.RLock()
    chatter_tasks: set[str] = set()
    chatter_lock = threading.RLock()
    started_at: float = time.time()
    shutting_down: bool = False


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesWidgetBridge/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        append_log("access.log", "%s - %s" % (self.client_address[0], fmt % args))

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

    def _send_asset(self, relative_path: str) -> None:
        target = (ASSETS_DIR / relative_path).resolve()
        try:
            target.relative_to(ASSETS_DIR.resolve())
        except ValueError:
            self._send_json(make_response("not found", ok=False, error="not found"), 404)
            return
        if not target.is_file():
            self._send_json(make_response("not found", ok=False, error="not found"), 404)
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> tuple[Any, str]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        if not raw:
            return {}, raw
        try:
            return json.loads(raw), raw
        except Exception:
            parsed = parse_qs(raw)
            if parsed:
                return {k: v[-1] if v else "" for k, v in parsed.items()}, raw
            return {"message": raw}, raw

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path.startswith("/assets/"):
            self._send_asset(path[len("/assets/"):])
            return
        if path in {"/", "/health", "/status"}:
            runtime = BridgeState.runtime
            self._send_json({
                "success": True,
                "ok": True,
                "bridge": "hermes-widget-bridge",
                "ready": bool(runtime and runtime.ready),
                "mode": BridgeState.cfg.get("mode"),
                "assistant_protocol": "jarvis-phase6-v1",
                "assistant_protocol_enabled": bool(BridgeState.cfg.get("assistant_protocol_enabled", True)),
                "uptime_seconds": round(time.time() - BridgeState.started_at, 1),
                "session_id": getattr(getattr(runtime, "cli", None), "session_id", None),
                "turns": getattr(runtime, "turn_count", 0),
                "tasks": len(BridgeState.tasks.list(1000)) if BridgeState.tasks else 0,
            })
            return
        if path in {"/big-window", "/chat-window"}:
            self._send_html(big_window_html(BridgeState.cfg))
            return
        if path in {"/big-window/state", "/chat-window/state"}:
            self._send_json(big_window_state())
            return
        if path in {"/assistant/agents", "/agents"}:
            self._send_json({"success": True, "ok": True, "assistant_protocol": "jarvis-phase6-v1", "agents": agents_payload(), "agent_profiles": AGENT_PROFILES})
            return
        if path in {"/assistant/presets", "/presets"}:
            self._send_json({"success": True, "ok": True, "assistant_protocol": "jarvis-phase6-v1", "presets": presets_payload()})
            return
        if path in {"/assistant/capabilities", "/capabilities"}:
            body = {"success": True, "ok": True}
            body.update(capabilities_payload())
            self._send_json(body)
            return
        if path in {"/assistant/tasks", "/tasks"}:
            self._send_json(assistant_status_response(f"{int(time.time() * 1000)}_{threading.get_ident()}"))
            return
        if path.startswith("/assistant/task/"):
            parts = path.split("/")
            task_id = parts[3] if len(parts) > 3 else ""
            self._send_json(assistant_status_response(f"{int(time.time() * 1000)}_{threading.get_ident()}", task_id=task_id))
            return
        self._send_json(make_response("not found", ok=False, error="not found"), 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        request_id = f"{int(time.time() * 1000)}_{threading.get_ident()}"
        try:
            payload, raw = self._read_body()
            append_log("requests.log", f"{request_id} {path} raw={raw[:4000]!r}")
            if path in {"/shutdown", "/control/shutdown"}:
                BridgeState.shutting_down = True
                self._send_json(make_response("shutting down", request_id=request_id))
                schedule_bridge_shutdown(self.server)
                return
            if path in {"/control/open-chat", "/control/big-window"}:
                ok, detail = open_big_window(BridgeState.cfg)
                if ok:
                    record_activity("control", "Opened big window mode.", source="widget", status="completed")
                    self._send_json(enrich_response(f"Opening big window mode: {detail}", classify_intent("system control"), request_id=request_id))
                else:
                    self._send_json(make_response(f"Could not open big window mode: {detail}", ok=False, error=detail, request_id=request_id), 500)
                return
            if path.startswith("/control"):
                self._send_json({"success": True, "ok": True, "status": "ok", "response": "ok", "message": "ok", "request_id": request_id})
                return
            if path.startswith("/assistant/task/") and path.endswith("/cancel"):
                parts = path.split("/")
                task_id = parts[3] if len(parts) > 3 else ""
                self._send_json(cancel_assistant_task(task_id, request_id))
                return
            if path in {"/assistant/tasks", "/tasks"}:
                self._send_json(assistant_status_response(request_id))
                return
            if path in {"/assistant/presets", "/presets"}:
                self._send_json({"success": True, "ok": True, "assistant_protocol": "jarvis-phase6-v1", "presets": presets_payload()})
                return
            if path in {"/assistant/capabilities", "/capabilities"}:
                body = {"success": True, "ok": True}
                body.update(capabilities_payload())
                self._send_json(body)
                return
            if path in {"/assistant/agents", "/agents"}:
                self._send_json({"success": True, "ok": True, "assistant_protocol": "jarvis-phase6-v1", "agents": agents_payload(), "agent_profiles": AGENT_PROFILES})
                return
            if path.startswith("/assistant/task/"):
                parts = path.split("/")
                task_id = parts[3] if len(parts) > 3 else ""
                self._send_json(assistant_status_response(request_id, task_id=task_id))
                return
            chat_paths = {"/codex/chat", "/chat", "/v1/chat", "/v1/chat/completions", "/assistant/chat", "/assistant/task"}
            if path not in chat_paths:
                self._send_json(make_response("not found", ok=False, error=f"unknown path: {path}", request_id=request_id), 404)
                return
            message = first_text(payload, list(BridgeState.cfg.get("request_message_fields") or DEFAULT_CONFIG["request_message_fields"]))
            if not message:
                self._send_json(make_response("I could not find a message in the widget request.", ok=False, error="missing message", request_id=request_id), 400)
                return
            record_activity("user", message, source="small-widget" if path == "/codex/chat" else path, status="queued", request_id=request_id)
            model_body = model_command_response(message, request_id)
            if model_body is not None:
                record_activity("agent", model_body.get("response") or model_body.get("message") or "", source="bridge", status="completed", request_id=request_id)
                self._send_json(model_body)
                return
            directory_body = directory_command_response(message, request_id)
            if directory_body is not None:
                record_activity("agent", directory_body.get("response") or directory_body.get("message") or "", source="bridge", status="completed", request_id=request_id)
                self._send_json(directory_body)
                return
            if is_integrated_exit_request(message, BridgeState.cfg):
                metadata = classify_intent(message)
                record_activity("control", "Closing the widget and bridge.", source="widget", status="completed", request_id=request_id)
                self._send_json(enrich_response("Closing the widget and bridge now. See you next time.", metadata, request_id=request_id))
                schedule_integrated_exit(self.server)
                return
            if is_big_window_request(message, BridgeState.cfg):
                metadata = classify_intent("system control")
                ok, detail = open_big_window(BridgeState.cfg)
                if ok:
                    record_activity("control", "Big window mode is open.", source="widget", status="completed", request_id=request_id)
                    self._send_json(enrich_response("Big window mode is open.", metadata, request_id=request_id))
                else:
                    self._send_json(make_response(f"Could not open big window mode: {detail}", ok=False, error=detail, request_id=request_id), 500)
                return
            if BridgeState.tasks and normalized_command(message).startswith(("/tasks", "task status", "status tasks", "show tasks")):
                status_body = assistant_status_response(request_id)
                record_activity("agent", status_body.get("response") or status_body.get("message") or "", source="bridge", status="completed", request_id=request_id)
                self._send_json(status_body)
                return
            metadata = classify_intent(message)
            if should_start_async(message, metadata, BridgeState.cfg, path):
                task_body = start_assistant_task(message, metadata, request_id)
                record_activity("task", task_body.get("response") or task_body.get("message") or "", source="bridge", status="running", request_id=request_id, task_id=task_body.get("task_id") or (task_body.get("task") or {}).get("task_id"))
                self._send_json(task_body)
                return
            runtime = BridgeState.runtime
            if runtime is None:
                raise RuntimeError("Agent runtime is not initialized")
            write_status(ready=runtime.ready, busy=True, current_request_id=request_id)
            response = runtime.chat(strip_async_prefix(message), request_id, agent_name=metadata.get("agent_name", "Hermes"))
            record_activity("agent", response, source="agent", status="completed", request_id=request_id, intent=metadata.get("intent") or metadata.get("mode"))
            self._send_json(enrich_response(response, metadata, request_id=request_id))
        except Exception as exc:
            append_log("bridge.log", f"{request_id} error: {exc}\n{traceback.format_exc()}")
            write_status(busy=False, last_error=str(exc))
            record_activity("error", str(exc), source="bridge", status="error", request_id=request_id)
            self._send_json(make_response(f"Widget bridge error: {exc}", ok=False, error=str(exc), request_id=request_id), 500)


def run_server(cfg: dict[str, Any]) -> int:
    ensure_dirs()
    selected_mode = os.environ.get("WIDGET_AGENT_MODE") or cfg.get("mode") or "persistent_direct"
    mode = str(selected_mode).strip().lower()
    cfg = dict(cfg)
    cfg["mode"] = mode
    BridgeState.cfg = cfg
    BridgeState.started_at = time.time()
    append_log("bridge.log", f"Starting widget bridge mode={mode}")
    BridgeState.tasks = TaskRegistry()
    if mode in {"codex", "codex_cli", "base_codex", "base-codex"}:
        BridgeState.runtime = CodexCliRuntime(cfg)
    else:
        BridgeState.runtime = HermesPersistentRuntime(cfg)
    record_activity("system", f"Bridge started in {mode} mode.", source="bridge", status="completed")
    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 44888)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    write_status(pid=os.getpid(), host=host, port=port, ready=bool(BridgeState.runtime and BridgeState.runtime.ready), started_at=now_iso())
    append_log("bridge.log", f"Listening on http://{host}:{port}")
    try:
        httpd.serve_forever(poll_interval=0.25)
    finally:
        append_log("bridge.log", "Bridge server stopped")
        write_status(ready=False, stopped_at=now_iso())
    return 0


def main() -> int:
    global CONFIG_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    CONFIG_PATH = Path(args.config).resolve()
    cfg = load_config()
    if args.self_test:
        ensure_dirs()
        rt = HermesPersistentRuntime(cfg)
        if not rt.ready:
            print(rt.init_error or "not ready")
            return 1
        print("persistent Hermes runtime initialized")
        return 0
    def _signal(_sig: int, _frame: Any) -> None:
        append_log("bridge.log", f"Signal {_sig}; exiting")
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _signal)
    except Exception:
        pass
    return run_server(cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        append_log("bridge.log", "Interrupted; exiting")
    finally:
        os._exit(0)
