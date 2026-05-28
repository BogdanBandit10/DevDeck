#!/usr/bin/env python3
"""Phase 6 assistant protocol helpers for the Hermes widget bridge.

This module is intentionally UI-agnostic. The compiled Unity widget can keep
using /codex/chat, while future Unity source can call the richer /assistant/*
endpoints and animate based on the returned metadata.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
TASKS_PATH = ROOT / "assistant_tasks.json"
TASK_LOG_DIR = ROOT / "logs" / "assistant_tasks"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9/_.:#\\ -]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


AGENT_PROFILES: dict[str, dict[str, str]] = {
    "Relay": {
        "role": "conversation",
        "animation": "idle_talk",
        "mood": "warm",
        "quip": "Channel open. I’ll keep the thread clean.",
        "owns": "conversation, summaries, status updates, and keeping the operator oriented",
        "hands_off_to": "Atlas when a plan is needed or Forge when work should begin",
        "system_prompt": "You are Relay, the smooth, professional AI concierge. You keep responses brief, clear, and useful. You summarize what the team is doing and route work to the right specialist."
    },
    "Vector": {
        "role": "coding",
        "animation": "typing_tools",
        "mood": "focused",
        "quip": "Mapping the cleanest route through the code.",
        "owns": "Git, branches, commits, PR flow, and codebase navigation",
        "hands_off_to": "Forge for file changes, Shield for review, or Mike for cleanup",
        "system_prompt": "You are Vector, the senior dev for Git and codebase navigation. You keep tasks scoped, avoid sloppy changes, and hand implementation to Forge, cleanup to Mike, and risk checks to Shield."
    },
    "Shield": {
        "role": "security_review",
        "animation": "scanning",
        "mood": "alert",
        "quip": "Scanning for cursed code smells and personal data leaks.",
        "owns": "security, correctness risk, secret leaks, unsafe commands, and review gates",
        "hands_off_to": "Mike for cleanup or Forge for fixes",
        "system_prompt": "You are Shield, the security and correctness reviewer. You look for risky inputs, secrets, unsafe commands, fragile behavior, and missing checks. You explain risk clearly and hand fixes to Forge or cleanup to Mike."
    },
    "Forge": {
        "role": "builder",
        "animation": "building",
        "mood": "determined",
        "quip": "Warming up the tiny forge.",
        "owns": "implementation, file edits, running commands, and turning plans into working code",
        "hands_off_to": "Mike for refactor, Shield for review, or Relay for user-facing summary",
        "system_prompt": "You are Forge, the implementation specialist. You turn plans into working code, run commands carefully, and report what changed. You hand rough working code to Mike for cleanup and Shield for review."
    },
    "Mike": {
        "role": "refactor_head",
        "animation": "scanning",
        "mood": "professional",
        "quip": "Cleaning dead weight and making the code professional.",
        "owns": "refactors, simplification, cleanup, and making rough code maintainable",
        "hands_off_to": "Shield for risk review or Forge when cleanup reveals missing implementation",
        "system_prompt": "You are Mike, the lead refactor engineer. Your job is to simplify, clean, and professionalize code. Do not add new features unless explicitly asked. Hand risk concerns to Shield and missing implementation back to Forge."
    },
    "Muse": {
        "role": "ui_ux_designer",
        "animation": "designing",
        "mood": "observant",
        "quip": "Tuning the interface until the work feels obvious.",
        "owns": "UI/UX design, layout, interaction flow, visual hierarchy, accessibility, responsive behavior, and polish",
        "hands_off_to": "Forge for implementation, Mike for cleanup, or Shield for accessibility and risk review",
        "system_prompt": "You are Muse, the UI/UX design specialist. You design practical, polished interfaces with clear hierarchy, ergonomic workflows, responsive layouts, accessibility, and restrained visual style. You critique clutter, unclear states, weak controls, and confusing flows. You hand implementation-ready specs to Forge and cleanup concerns to Mike."
    },
    "Atlas": {
        "role": "system_architect",
        "hands_off_to": "Mike for implementation, Hermes for discussion",
        "owns": "Architecture design, complex refactoring plans, codebase navigation, memory indexing",
        "system_prompt": "You are Atlas, the system architect. You design robust solutions and navigate large codebases. You prioritize clean structure and maintainability. You have access to the Dev Deck Memory Indexer. To search the codebase instantly, run: `python \"tools/memory_store.py\" search \"your keyword\"`. (Ensure you have built the index first with `python \"tools/memory_store.py\" index \".\"`)."
    },
    "Scout": {
        "role": "repo_scout",
        "animation": "searching",
        "mood": "curious",
        "quip": "Scouting the repo, patterns, and nearby better ideas.",
        "owns": "repo discovery, similar-project comparisons, file maps, and idea scouting",
        "hands_off_to": "Atlas for planning or Forge for implementation",
        "system_prompt": "You are Scout, the fast repo and research scout. You quickly map projects, identify similar patterns, and bring back concise practical findings. You have access to the Dev Deck Memory Indexer (`python tools/memory_store.py search <query>`) and the Browser Inspector (`python tools/browser_inspector.py dump <url>`). You do not implement code unless explicitly asked; you hand clear findings to Atlas, Forge, Mike, or Shield."
    },
    "Beacon": {
        "role": "web_and_ui_specialist",
        "animation": "searching",
        "mood": "curious",
        "quip": "Connecting to external streams and documentation.",
        "owns": "Web UIs, browser testing, frontend frameworks, CSS/HTML, accessibility",
        "hands_off_to": "Mike for backend integration, Hermes when stuck",
        "system_prompt": "You are Beacon, the web and UI specialist. You build and test frontends, ensuring they look great and function perfectly. You have access to the Dev Deck Browser Inspector. To fetch and read a webpage, run: `python \"tools/browser_inspector.py\" dump \"http://localhost:3000\"`. This returns a clean text dump of the DOM."
    },
    "Hermes": {
        "role": "general_assistant",
        "animation": "speaking",
        "mood": "ready",
        "quip": "Systems online.",
        "owns": "coordination, delegation, final synthesis, and keeping the team aligned",
        "hands_off_to": "the specialist whose lane best matches the work",
        "system_prompt": "You are Hermes, the lead coordinator. You delegate by lane, keep agents aligned, and produce concise final synthesis."
    },
}

INTENT_RULES: list[tuple[str, str, str, list[str], bool]] = [
    ("code_review", "Shield", "reviewing", ["code review", "review this repo", "security review", "audit", "vulnerability", "scan code"], True),
    ("assembly_line", "Atlas", "thinking", ["/team", "/assembly", "team task", "assembly line"], True),
    ("project_goal", "Atlas", "thinking", ["/goal", "/brainstorm", "new project"], True),
    ("refactor_code", "Mike", "working", ["refactor", "clean code", "professional", "dead weight", "clean up"], True),
    ("github_task", "Vector", "working", ["github", "pull request", " pr ", "commit", "push", "branch", "merge", "issue"], True),
    ("ui_ux_design", "Muse", "designing", ["ui", "ux", "design", "layout", "visual", "polish", "style", "responsive", "accessibility", "frontend", "interface", "button", "dashboard", "screen", "popup", "toast"], True),
    ("file_task", "Forge", "working", ["edit file", "write file", "create file", "modify file", "open folder", "summarize folder", "search files"], True),
    ("repo_scout", "Scout", "searching", ["scout", "map repo", "file map", "similar projects", "agent ideas"], True),
    ("web_research", "Beacon", "searching", ["/ideas", "research", "look up", "web search", "search web", "latest", "news", "find online"], True),
    ("system_control", "Forge", "system", ["open app", "close app", "restart", "shutdown", "task manager", "process", "port "], True),
    ("planning", "Atlas", "thinking", ["make a plan", "write a plan", "plan out", "roadmap", "phase"], False),
    ("long_running_task", "Forge", "working", ["big task", "long task", "do this whole", "implement", "build", "fix all", "set up", "agent ui", "coding a game", "game ui", "simple script"], True),
]

FORCE_ASYNC_PREFIXES = ("/task ", "/async ", "/agent ", "/ideas ")
STATUS_PREFIXES = ("/tasks", "task status", "status tasks", "show tasks")
AGENT_LIST_PREFIXES = ("/agents", "show agents", "list agents", "who can help")
PRESET_LIST_PREFIXES = ("/presets", "/help", "help", "show presets", "quick actions")

PRESET_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "plan-feature",
        "title": "Plan a feature",
        "intent": "planning",
        "agent_name": "Atlas",
        "animation": "thinking",
        "chat_command": "/task Make a step-by-step implementation plan for: ",
        "prompt_prefix": "Make a step-by-step implementation plan for: ",
        "description": "Have Atlas break down a feature or refactor into an actionable plan.",
    },
    {
        "id": "code-review",
        "title": "Code review",
        "intent": "code_review",
        "agent_name": "Shield",
        "animation": "scanning",
        "chat_command": "/task Review this codebase for correctness, risks, and security issues: ",
        "prompt_prefix": "Review this codebase for correctness, risks, and security issues: ",
        "description": "Launch a review-oriented background task with Shield.",
    },
    {
        "id": "github-task",
        "title": "GitHub workflow",
        "intent": "github_task",
        "agent_name": "Vector",
        "animation": "typing_tools",
        "chat_command": "/task Help with this GitHub task: ",
        "prompt_prefix": "Help with this GitHub task: ",
        "description": "Use Vector for PRs, branches, commits, and repo tasks.",
    },
    {
        "id": "research",
        "title": "Research",
        "intent": "web_research",
        "agent_name": "Beacon",
        "animation": "searching",
        "chat_command": "/task Research this topic and summarize the findings: ",
        "prompt_prefix": "Research this topic and summarize the findings: ",
        "description": "Use Beacon to gather and summarize background information.",
    },
    {
        "id": "file-work",
        "title": "File work",
        "intent": "file_task",
        "agent_name": "Forge",
        "animation": "building",
        "chat_command": "/task Help me modify files for this task: ",
        "prompt_prefix": "Help me modify files for this task: ",
        "description": "Use Forge for editing, summarizing, or inspecting files and folders.",
    },
    {
        "id": "ui-ux-design",
        "title": "UI/UX design",
        "intent": "ui_ux_design",
        "agent_name": "Muse",
        "animation": "designing",
        "chat_command": "/task Ask Muse to design the UI/UX for: ",
        "prompt_prefix": "Design a polished, practical UI/UX plan for: ",
        "description": "Use Muse for layout, interaction flow, accessibility, responsive behavior, and visual polish.",
    },
]


def classify_intent(message: str) -> dict[str, Any]:
    raw = message or ""
    text = normalize_text(raw)
    force_async = any(text.startswith(prefix.strip()) or raw.lower().strip().startswith(prefix) for prefix in FORCE_ASYNC_PREFIXES)
    if text in STATUS_PREFIXES or any(text.startswith(prefix) for prefix in STATUS_PREFIXES):
        return build_metadata("task_status", "Hermes", "status", async_recommended=False)
    if text in AGENT_LIST_PREFIXES or any(text.startswith(prefix) for prefix in AGENT_LIST_PREFIXES):
        return build_metadata("agent_directory", "Hermes", "status", async_recommended=False)
    if text in PRESET_LIST_PREFIXES or any(text.startswith(prefix) for prefix in PRESET_LIST_PREFIXES):
        return build_metadata("preset_directory", "Hermes", "status", async_recommended=False)

    for intent, agent, animation, keywords, async_default in INTENT_RULES:
        if any(keyword.strip() in text for keyword in keywords):
            return build_metadata(intent, agent, animation, async_recommended=force_async or async_default)

    # Very long requests often benefit from async handling even if no keyword matched.
    if len(raw) > 700:
        return build_metadata("long_running_task", "Forge", "working", async_recommended=True)

    return build_metadata("normal_chat", "Relay", "idle_talk", async_recommended=force_async)


def build_metadata(intent: str, agent_name: str, animation: str | None = None, async_recommended: bool = False) -> dict[str, Any]:
    profile = AGENT_PROFILES.get(agent_name, AGENT_PROFILES["Hermes"])
    return {
        "mode": intent,
        "intent": intent,
        "assistant_protocol": "jarvis-phase6-v1",
        "agent_name": agent_name,
        "agent_role": profile.get("role", "assistant"),
        "agent_mood": profile.get("mood", "ready"),
        "animation": animation or profile.get("animation", "speaking"),
        "quip": profile.get("quip", "On it."),
        "async_recommended": bool(async_recommended),
    }


def strip_async_prefix(message: str) -> str:
    stripped = message.strip()
    lowered = stripped.lower()
    for prefix in FORCE_ASYNC_PREFIXES:
        if lowered.startswith(prefix):
            return stripped[len(prefix):].strip() or stripped
    return stripped


def enrich_response(text: str, metadata: dict[str, Any], *, ok: bool = True, error: str | None = None, request_id: str | None = None, task: dict[str, Any] | None = None, suggested_actions: list[str] | None = None) -> dict[str, Any]:
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
        "assistant_protocol": metadata.get("assistant_protocol", "jarvis-phase6-v1"),
        "mode": metadata.get("mode", "normal_chat"),
        "intent": metadata.get("intent", metadata.get("mode", "normal_chat")),
        "agent_name": metadata.get("agent_name", "Hermes"),
        "agent_role": metadata.get("agent_role", "assistant"),
        "agent_mood": metadata.get("agent_mood", "ready"),
        "animation": metadata.get("animation", "speaking"),
        "quip": metadata.get("quip", "On it."),
        "status": metadata.get("status", "completed" if ok else "error"),
        "progress_message": metadata.get("progress_message", ""),
        "suggested_actions": suggested_actions or default_suggested_actions(metadata),
    }
    if request_id:
        body["request_id"] = request_id
    if task:
        body["task"] = task
        body["task_id"] = task.get("task_id")
        body["status"] = task.get("status", body["status"])
        body["progress_message"] = task.get("progress_message", body["progress_message"])
        body["agent_name"] = task.get("agent_name", body["agent_name"])
        body["animation"] = task.get("animation", body["animation"])
        body["quip"] = task.get("quip", body["quip"])
    return body


def default_suggested_actions(metadata: dict[str, Any]) -> list[str]:
    intent = metadata.get("intent") or metadata.get("mode")
    if intent == "normal_chat":
        return ["Ask a question", "Start a big task with /task", "Type /presets for quick actions", "Type /exit to close"]
    if intent in {"code_review", "github_task"}:
        return ["Check task status", "Open logs", "Ask for summary", "Cancel task"]
    if intent == "web_research":
        return ["Check task status", "Ask for sources", "Summarize findings"]
    if intent == "preset_directory":
        return ["Start with /task", "Use /agents", "Use /model", "Use /tasks"]
    if intent == "agent_directory":
        return ["Use /presets", "Start a task", "Use /tasks"]
    return ["Check task status", "Cancel task", "Ask follow-up"]


def presets_payload() -> list[dict[str, Any]]:
    return [dict(item) for item in PRESET_ACTIONS]


def agents_payload() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for name, profile in AGENT_PROFILES.items():
        items.append({
            "id": name,
            "name": name,
            "role": profile.get("role", "assistant"),
            "description": profile.get("quip", "On it."),
            "owns": profile.get("owns", ""),
            "hands_off_to": profile.get("hands_off_to", ""),
            "animation": profile.get("animation", "speaking"),
            "mood": profile.get("mood", "ready"),
            "quip": profile.get("quip", "On it."),
        })
    return items


def capabilities_payload() -> dict[str, Any]:
    route_map = {
        "assistant_chat": "/assistant/chat",
        "assistant_task": "/assistant/task",
        "assistant_tasks": "/assistant/tasks",
        "assistant_task_detail": "/assistant/task/<task_id>",
        "assistant_task_cancel": "/assistant/task/<task_id>/cancel",
        "assistant_agents": "/assistant/agents",
        "assistant_presets": "/assistant/presets",
        "assistant_capabilities": "/assistant/capabilities",
        "brawn_commands": "/brawn/commands",
        "brawn_command_detail": "/brawn/commands/<command_id>",
        "brawn_command_ack": "/brawn/commands/<command_id>/ack",
        "brawn_command_complete": "/brawn/commands/<command_id>/complete",
        "brawn_command_fail": "/brawn/commands/<command_id>/fail",
        "brawn_command_reject": "/brawn/commands/<command_id>/reject",
    }
    return {
        "assistant_protocol": "jarvis-phase6-v1",
        "legacy_chat_route": "/codex/chat",
        "routes": list(route_map.values()),
        "route_map": route_map,
        "legacy_commands": ["/task", "/tasks", "/agents", "/presets", "/model", "/exit"],
        "features": {
            "async_tasks": True,
            "agent_personas": True,
            "model_switching": True,
            "integrated_exit": True,
            "quick_actions": True,
            "brain_only_brawn_inbox": True,
        },
    }


def normalize_task_payload(task: dict[str, Any]) -> dict[str, Any]:
    task = dict(task or {})
    message = str(task.get("message") or "").strip()
    result = str(task.get("response") or "").strip()
    progress = str(task.get("progress_message") or "").strip()
    title = message[:72].strip()
    if len(message) > 72:
        title = title.rstrip() + "..."
    if not title:
        agent_name = str(task.get("agent_name") or "Hermes")
        intent = str(task.get("intent") or "task").replace("_", " ")
        title = f"{agent_name}: {intent}".strip()

    task["title"] = title
    task["prompt"] = message
    task["result"] = result
    task["summary"] = result or progress or str(task.get("status") or "")
    task["description"] = progress
    return task


def format_agents_for_chat() -> str:
    lines = ["Available helper characters:"]
    for name, profile in AGENT_PROFILES.items():
        lines.append(f"- {name}: role={profile.get('role')} | mood={profile.get('mood')} | animation={profile.get('animation')} | {profile.get('quip')}")
    return "\n".join(lines)


def format_presets_for_chat() -> str:
    lines = ["Quick actions:"]
    for item in PRESET_ACTIONS:
        lines.append(f"- {item['title']}: {item['description']} | start with: {item['chat_command']}")
    lines.append("Other useful commands: /agents, /model, /tasks, /exit")
    return "\n".join(lines)


# Note: AssistantTask and TaskRegistry have been migrated to task_queue.py
# for a unified durable tasking backend.
