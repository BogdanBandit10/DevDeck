#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from state import BridgeState, ROOT, CONNECTOR_STATE_PATH, CONNECTOR_LOG_PATH, SUPERVISOR_STATE_PATH
from bridge_utils import append_log, now_iso

def write_connector_state(**values: Any) -> None:
    current: dict[str, Any] = {}
    if CONNECTOR_STATE_PATH.exists():
        try: current = json.loads(CONNECTOR_STATE_PATH.read_text(encoding="utf-8"))
        except Exception: current = {}
    current.update(values)
    current["updated_at"] = now_iso()
    CONNECTOR_STATE_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")

def read_connector_state() -> dict[str, Any]:
    try:
        if CONNECTOR_STATE_PATH.exists():
            data = json.loads(CONNECTOR_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict): return data
    except Exception: pass
    return {}

def read_supervisor_state() -> dict[str, Any]:
    try:
        if SUPERVISOR_STATE_PATH.exists():
            data = json.loads(SUPERVISOR_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict): return data
    except Exception: pass
    return {}

def connector_status_payload() -> dict[str, Any]:
    supervisor = BridgeState.connector_supervisor
    if supervisor: return supervisor.status()
    return read_connector_state()

def http_json_status(url: str, timeout: float = 1.0, headers: dict[str, str] | None = None) -> tuple[bool, dict[str, Any] | str]:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(65536).decode("utf-8", errors="replace")
            try: return 200 <= response.status < 300, json.loads(raw)
            except Exception: return 200 <= response.status < 300, raw
    except Exception as exc: return False, str(exc)

def pid_is_alive(pid: int) -> bool:
    if pid <= 0: return False
    try:
        if os.name == "nt":
            proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=5)
            return str(pid) in (proc.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception: return False

def terminate_pid_tree(pid: int, label: str) -> None:
    if pid <= 0 or pid == os.getpid(): return
    if not pid_is_alive(pid): return
    try:
        if os.name == "nt": subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=10)
        else: os.kill(pid, signal.SIGTERM)
    except Exception: pass

def commandline_pids(process_names: set[str], required_text: list[str]) -> list[int]:
    if os.name != "nt": return []
    name_filter = " -or ".join([f"$_.Name -ieq '{name}'" for name in process_names])
    text_filter = " -and ".join([f"$_.CommandLine -like '*{t}*'" for t in required_text])
    script = f"Get-CimInstance Win32_Process | Where-Object {{ ({name_filter}) -and ({text_filter}) }} | Select-Object -ExpandProperty ProcessId"
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=8)
        return [int(line.strip()) for line in (proc.stdout or "").splitlines() if line.strip().isdigit()]
    except Exception: return []

def find_executable(command: str) -> str:
    found = shutil.which(command)
    if found: return found
    return command

def cleanup_external_processes() -> None:
    supervisor = BridgeState.connector_supervisor
    if supervisor: supervisor.stop()
    for pid in commandline_pids({"node.exe"}, ["dev-deck-mcp-server.mjs"]): terminate_pid_tree(pid, "chatgpt-mcp")
    for pid in commandline_pids({"cloudflared.exe"}, ["tunnel --url"]): terminate_pid_tree(pid, "chatgpt-tunnel")

class ChatGPTConnectorSupervisor:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.cloudflared_proc = None
        self.connector_proc = None
        self.public_url = ""
        self.hostname = ""
        self.last_error = ""
        self.last_log = ""
        self.enabled = bool(cfg.get("chatgpt_connector_enabled", True))
        self.host = str(cfg.get("chatgpt_connector_host") or "127.0.0.1")
        self.port = int(cfg.get("chatgpt_connector_port") or 3000)
        self.local_url = f"http://{self.host}:{self.port}"

    def start(self):
        if not self.enabled: return
        write_connector_state(enabled=True, status="starting", last_error="", public_url="", mcp_url="")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            for pid in commandline_pids({"node.exe"}, ["dev-deck-mcp-server.mjs"]):
                terminate_pid_tree(pid, "stale-chatgpt-mcp")
            for pid in commandline_pids({"cloudflared.exe"}, ["tunnel --url", self.local_url]):
                terminate_pid_tree(pid, "stale-chatgpt-tunnel")
            self._start_tunnel()
            self._wait_for_public_url()
            self._start_connector()
            write_connector_state(**self.status())
        except Exception as exc:
            self.last_error = str(exc)
            append_log("bridge.log", f"Connector error: {exc}")
            write_connector_state(**self.status())

    def _start_tunnel(self):
        cmd = find_executable("cloudflared")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.cloudflared_proc = subprocess.Popen(
            [cmd, "tunnel", "--url", self.local_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            creationflags=flags,
        )
        write_connector_state(**self.status())
        threading.Thread(target=self._read_tunnel, daemon=True).start()

    def _read_tunnel(self):
        pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
        for line in self.cloudflared_proc.stdout:
            self.last_log = line.strip()
            match = pattern.search(line)
            if match:
                self.public_url = match.group(0).rstrip("/")
                self.hostname = urlparse(self.public_url).hostname or ""
            write_connector_state(**self.status())

    def _wait_for_public_url(self):
        deadline = time.time() + 25
        while time.time() < deadline and not self.public_url: time.sleep(0.2)
        if not self.public_url: raise RuntimeError("Tunnel timeout")

    def _start_connector(self):
        script = ROOT.parent / "connector" / "dev-deck-mcp-server.mjs"
        if not script.exists():
            raise RuntimeError(f"MCP connector script not found: {script}")
        env = os.environ.copy()
        env["DEV_DECK_MCP_PORT"] = str(self.port)
        env["DEV_DECK_MCP_HOST"] = self.host
        env["DEV_DECK_HERMES_URL"] = f"http://{BridgeState.cfg.get('host', '127.0.0.1')}:{BridgeState.cfg.get('port', 44888)}"
        allowed = ["127.0.0.1", "localhost"]
        if self.hostname:
            allowed.append(self.hostname)
        env["DEV_DECK_MCP_ALLOWED_HOSTS"] = ",".join(allowed)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.connector_proc = subprocess.Popen(
            ["node", str(script)],
            cwd=str(ROOT.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        write_connector_state(**self.status())

    def stop(self):
        if self.connector_proc: terminate_pid_tree(self.connector_proc.pid, "mcp")
        if self.cloudflared_proc: terminate_pid_tree(self.cloudflared_proc.pid, "tunnel")
        write_connector_state(enabled=self.enabled, status="stopped", connector_running=False, tunnel_running=False)

    def status(self) -> dict[str, Any]:
        connector_pid = self.connector_proc.pid if self.connector_proc else 0
        tunnel_pid = self.cloudflared_proc.pid if self.cloudflared_proc else 0
        connector_running = pid_is_alive(connector_pid)
        tunnel_running = pid_is_alive(tunnel_pid)
        mcp_url = f"{self.public_url}/mcp" if self.public_url else ""
        local_health_ok, local_health = http_json_status(f"{self.local_url}/health", timeout=0.6) if connector_running else (False, "")
        public_health_ok, public_health = http_json_status(f"{self.public_url}/health", timeout=1.5) if self.public_url and connector_running else (False, "")
        if not public_health_ok and self.public_url and tunnel_running and local_health_ok:
            public_health_ok = True
            public_health = "Tunnel process is active and the local connector is healthy."
        ready = bool(connector_running and tunnel_running and mcp_url and (public_health_ok or local_health_ok))
        status = "ready" if ready else "error" if self.last_error else "starting" if (connector_running or tunnel_running) else "offline"
        return {
            "enabled": self.enabled,
            "status": status,
            "ready": ready,
            "running": bool(connector_running or tunnel_running),
            "connector_running": connector_running,
            "tunnel_running": tunnel_running,
            "connector_pid": connector_pid,
            "tunnel_pid": tunnel_pid,
            "host": self.host,
            "port": self.port,
            "local_url": self.local_url,
            "local_mcp_url": f"{self.local_url}/mcp",
            "public_url": self.public_url,
            "mcp_url": mcp_url,
            "hostname": self.hostname,
            "public_health_ok": public_health_ok,
            "local_health_ok": local_health_ok,
            "public_health": public_health,
            "local_health": local_health,
            "last_error": self.last_error,
            "last_log": self.last_log,
        }
