#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any
from state import BridgeState, ROOT
from bridge_utils import append_log, strip_ansi, truncate, now_iso, expand_path

def command_path(command: str) -> str:
    command = str(command or "").strip()
    if not command:
        return command
    if os.name == "nt" and command.lower() == "opencode":
        appdata = os.environ.get("APPDATA")
        candidates = []
        if appdata:
            candidates.extend([
                Path(appdata) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
                Path(appdata) / "npm" / "opencode.cmd",
            ])
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    if Path(command).exists():
        return command
    found = shutil.which(command)
    if found:
        return found
    return command

def run_command_capture(command: list[str], *, cwd: Path | None = None, timeout: int = 30, log_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8", errors="replace") as f:
            proc = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            # Since we redirected to file, CompletedProcess will have empty stdout
            return subprocess.CompletedProcess(command, proc.returncode, stdout="", stderr="")

    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )

def lmstudio_exe_path() -> Path:
    configured = expand_path(BridgeState.cfg.get("lmstudio_exe"))
    if configured and configured.exists():
        return configured
    # Fallback to current user path
    home = Path.home()
    fallback = home / "AppData/Local/Programs/LM Studio/LM Studio.exe"
    return fallback

def git_status_lines(cwd: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return [f"git status unavailable: {exc}"]
    if proc.returncode != 0:
        return [f"git status failed: {(proc.stderr or proc.stdout).strip()}"]
    return [line for line in proc.stdout.splitlines() if line.strip()]

def git_status_map(cwd: Path) -> dict[str, str]:
    lines = git_status_lines(cwd)
    res: dict[str, str] = {}
    for line in lines:
        if len(line) < 4:
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        res[path.strip('"')] = status
    return res

def git_rollback_files(cwd: Path, files: list[str]) -> dict[str, Any]:
    if not files:
        return {"ok": True, "message": "No files to rollback"}
    
    # Brain/Hands mandate: Surgical rollback only for specified files
    results = []
    for f in files:
        try:
            # Revert to index state
            proc = subprocess.run(
                ["git", "checkout", "HEAD", "--", f],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                results.append(f"Restored {f}")
            else:
                results.append(f"Failed {f}: {proc.stderr.strip()}")
        except Exception as exc:
            results.append(f"Error {f}: {exc}")
            
    return {
        "ok": True,
        "results": results,
        "git_status_after": git_status_lines(cwd)
    }

def extract_opencode_text(stdout: str, stderr: str) -> str:
    texts: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        part = event.get("part") if isinstance(event, dict) else {}
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            texts.append(str(part.get("text")).strip())
    if texts:
        return "\n\n".join(text for text in texts if text)
    return "\n".join(part for part in [stdout.strip(), stderr.strip()] if part)

def lmstudio_status() -> dict[str, Any]:
    from services import commandline_pids, http_json_status
    cfg = BridgeState.cfg
    lms = command_path(str(cfg.get("lmstudio_lms_command") or "lms"))
    port = int(cfg.get("lmstudio_server_port") or 1234)
    model_key = str(cfg.get("lmstudio_model_key") or "qwen3.5-9b-mtp")
    model_identifier = str(cfg.get("lmstudio_model_identifier") or model_key)
    app_running = bool(commandline_pids({"LM Studio.exe"}, [str(lmstudio_exe_path())]))
    server_running = False
    try:
        proc = run_command_capture([lms, "server", "status", "--json"], timeout=8)
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            server_running = bool(data.get("running"))
            port = int(data.get("port") or port)
    except Exception:
        pass
    api_ok, _ = http_json_status(f"http://127.0.0.1:{port}/v1/models", timeout=0.8)
    opencode_cmd = command_path(str(cfg.get("opencode_command") or "opencode"))
    opencode_found = bool(opencode_cmd and (Path(opencode_cmd).exists() or shutil.which(opencode_cmd)))
    return {
        "app_running": app_running,
        "server_running": server_running,
        "api_ok": api_ok,
        "port": port,
        "model_key": model_key,
        "model_identifier": model_identifier,
        "model_loaded": api_ok,
        "opencode_found": opencode_found,
        "opencode_command": opencode_cmd,
        "ready": bool(server_running and api_ok and opencode_found),
    }

def start_lmstudio_stack(load_model: bool = True) -> dict[str, Any]:
    from services import commandline_pids
    cfg = BridgeState.cfg
    exe = lmstudio_exe_path()
    if exe.exists() and not commandline_pids({"LM Studio.exe"}, [str(exe)]):
        subprocess.Popen([str(exe)], cwd=str(exe.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.0)
    lms = command_path(str(cfg.get("lmstudio_lms_command") or "lms"))
    port = int(cfg.get("lmstudio_server_port") or 1234)
    try:
        run_command_capture([lms, "server", "start", "--port", str(port)], timeout=20)
    except Exception as exc:
        append_log("bridge.log", f"LM Studio server start failed: {exc}")
    
    if load_model:
        model_key = str(cfg.get("lmstudio_model_key") or "qwen3.5-9b-mtp")
        model_identifier = str(cfg.get("lmstudio_model_identifier") or model_key)
        try:
            run_command_capture([lms, "load", model_key, "--identifier", model_identifier, "-y"], timeout=90)
        except Exception as exc:
            append_log("bridge.log", f"LM Studio model load failed: {exc}")
    return lmstudio_status()

def open_big_window(cfg: dict[str, Any]) -> tuple[bool, str]:
    from handlers import big_window_url
    url = big_window_url(cfg)
    try:
        if os.name == "nt":
            edge = shutil.which("msedge") or shutil.which("msedge.exe")
            if edge:
                subprocess.Popen([edge, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.startfile(url)
        else:
            opener = shutil.which("xdg-open") or shutil.which("open")
            if not opener:
                return False, "No browser opener"
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, url
    except Exception as exc:
        return False, str(exc)
