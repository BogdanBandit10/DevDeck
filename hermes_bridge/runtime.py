#!/usr/bin/env python3
from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from state import BridgeState, ROOT, TURN_LOG_DIR
from bridge_utils import append_log, strip_ansi, truncate, now_iso, write_status, expand_path
from assistant_protocol import AGENT_PROFILES

class CodexRuntime:
    """Codex CLI runtime used as the default Dev Deck brain."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.ready = bool(self._resolve_codex_command())
        self.init_error = None if self.ready else "Codex command not found"
        self.turn_count = 0
        if self.ready:
            write_status(mode="codex_cli", ready=True)
        else:
            append_log("bridge.log", self.init_error or "Codex command not found")

    def _resolve_codex_command(self) -> str:
        configured = str(self.cfg.get("codex_command") or "").strip()
        if configured and Path(configured).exists():
            return configured
        if configured:
            found = shutil.which(configured)
            if found:
                return found
        return shutil.which("codex") or ""

    def _base_command(self) -> list[str]:
        command = self._resolve_codex_command()
        if command.lower().endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", command]
        return [command]

    def chat(self, message: str, request_id: str, agent_name: str = "Codex") -> str:
        if not self.ready:
            raise RuntimeError(self.init_error or "Codex runtime is not ready")
        with self.lock:
            self.turn_count += 1
            turn_no = self.turn_count
            started = time.time()
            persona = AGENT_PROFILES.get(agent_name, {}).get("system_prompt", "")
            prompt = message
            if persona and agent_name not in {"Codex", "Hermes"}:
                prompt = f"[Role context: {persona}]\n\nUser: {message}"

            output_path = Path(tempfile.gettempdir()) / f"dev_deck_codex_{request_id}.txt"
            cwd = (
                expand_path(self.cfg.get("codex_working_directory"), ROOT.parent)
                or expand_path(self.cfg.get("opencode_working_directory"), ROOT.parent)
                or expand_path(self.cfg.get("working_directory"), ROOT.parent)
                or ROOT.parent
            )
            timeout = int(self.cfg.get("codex_timeout_seconds") or 120)
            args = [
                *self._base_command(),
                "exec",
                "--cd", str(cwd),
                "--sandbox", str(self.cfg.get("codex_sandbox_mode") or "danger-full-access"),
                "--output-last-message", str(output_path),
            ]
            model = str(self.cfg.get("codex_model") or "").strip()
            if model:
                args.extend(["--model", model])
            reasoning = str(self.cfg.get("codex_reasoning_effort") or "").strip()
            if reasoning:
                args.extend(["-c", f'model_reasoning_effort="{reasoning}"'])
            args.append(prompt)

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            try:
                proc = subprocess.run(
                    args,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                )
                response = ""
                if output_path.exists():
                    response = output_path.read_text(encoding="utf-8", errors="replace").strip()
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                if not response:
                    response = strip_ansi((proc.stdout or "").strip())
                if proc.returncode != 0:
                    detail = strip_ansi((proc.stderr or proc.stdout or f"Codex exited {proc.returncode}").strip())
                    raise RuntimeError(detail)
                elapsed = time.time() - started
                self._write_turn_log(request_id, turn_no, message, response, proc.stdout or "", elapsed, None)
                write_status(mode="codex_cli", ready=True, busy=False, last_request_id=request_id, last_elapsed_seconds=round(elapsed, 3), turns=turn_no)
                return response
            except Exception as exc:
                elapsed = time.time() - started
                self._write_turn_log(request_id, turn_no, message, "", "", elapsed, exc)
                raise

    def _write_turn_log(self, request_id: str, turn_no: int, message: str, response: str, captured: str, elapsed: float, exc: Exception | None) -> None:
        TURN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "backend": "codex",
            "request_id": request_id,
            "turn": turn_no,
            "started_at": now_iso(),
            "elapsed_seconds": round(elapsed, 3),
            "message_preview": message[:1000],
            "response_preview": response[:2000],
            "error": repr(exc) if exc else None,
            "captured_output": truncate(strip_ansi(captured or ""), int(self.cfg.get("log_max_chars_per_turn") or 200000)),
        }
        (TURN_LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{request_id}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

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
        source = expand_path(self.cfg.get("hermes_source_path"))
        if not source or not source.exists():
            self.init_error = f"Hermes source path not found: {source}"
            append_log("bridge.log", self.init_error)
            return
        import sys
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        self.ready = True

    def _get_cli(self, agent_name: str) -> Any:
        if agent_name in self.clis:
            return self.clis[agent_name]

        cwd = expand_path(self.cfg.get("working_directory"), ROOT.parent)
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
                # Persona-specific toolset defaults
                defaults = {
                    "Vector": ["github", "bash"],
                    "Mike": ["file", "bash"],
                    "Shield": ["file", "bash"],
                    "Beacon": ["web", "bash"],
                    "Muse": ["file", "bash"],
                    "Forge": ["file", "bash"],
                    "Atlas": ["file", "bash"],
                }
                toolsets = defaults.get(agent_name)

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
            workspace_path = expand_path(self.cfg.get("working_directory"), ROOT.parent) or ROOT.parent
            workspace = str(workspace_path)
            plan_path = Path(workspace) / "PROJECT_PLAN.md"
            context_hint = ""
            if plan_path.exists():
                context_hint = " Note: A PROJECT_PLAN.md file exists in the workspace. Read it with your file tools if you need global project context."

            persona = AGENT_PROFILES.get(agent_name, {}).get("system_prompt", "")
            if persona and turn_no == 1:
                message_to_send = f"[SYSTEM: {persona}{context_hint}]\n\nUser: {message}"
            elif persona:
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
        TURN_LOG_DIR.mkdir(parents=True, exist_ok=True)
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
        cwd = expand_path(self.cfg.get("working_directory"), ROOT.parent) or ROOT
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, creationflags=creationflags, startupinfo=startupinfo)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"Hermes exited {proc.returncode}").strip())
        return strip_ansi(proc.stdout).strip()
