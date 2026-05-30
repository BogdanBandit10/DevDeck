#!/usr/bin/env python3
"""Single-file launcher for Dev Deck big-window mode."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from winreg import HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, OpenKey, QueryValueEx


APP_NAME = "Dev Deck"
HOST = "127.0.0.1"
PORT = 44888
MODE = "codex_cli"
HEALTH_URL = f"http://{HOST}:{PORT}/health"
DECK_URL = f"http://{HOST}:{PORT}/big-window"


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_root()
BRIDGE_DIR = ROOT / "hermes_bridge"
LOG_DIR = BRIDGE_DIR / "logs"
LOG_PATH = LOG_DIR / "dev_deck_launcher.log"
CONFIG_PATH = BRIDGE_DIR / "config.json"
BRIDGE_SCRIPT = BRIDGE_DIR / "hermes_bridge.py"
EDGE_PROFILE = ROOT / ".hermes" / "edge-devdeck-profile"


def log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        LOG_PATH.open("a", encoding="utf-8", errors="replace").write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def load_port() -> int:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return int(data.get("port") or PORT)
    except Exception:
        return PORT


def health_url() -> str:
    return f"http://{HOST}:{load_port()}/health"


def deck_url() -> str:
    return f"http://{HOST}:{load_port()}/big-window"


def bridge_ready(timeout: float = 0.7) -> bool:
    try:
        with urllib.request.urlopen(health_url(), timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def find_pythonw() -> str:
    local_appdata = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    hermes_root = local_appdata / "hermes" / "hermes-agent" / "venv" / "Scripts"
    hermes_pythonw = hermes_root / "pythonw.exe"
    if hermes_pythonw.exists():
        return str(hermes_pythonw)
    hermes_python = hermes_root / "python.exe"
    if hermes_python.exists():
        return str(hermes_python)
    return "pythonw.exe"


def start_bridge() -> None:
    if not BRIDGE_SCRIPT.exists():
        raise FileNotFoundError(f"Bridge script not found: {BRIDGE_SCRIPT}")
    env = os.environ.copy()
    env["WIDGET_AGENT_MODE"] = MODE
    env.setdefault("PYTHONIOENCODING", "utf-8")
    command = [
        find_pythonw(),
        str(BRIDGE_SCRIPT),
        "--config",
        str(CONFIG_PATH),
    ]
    log("Starting bridge: " + " ".join(command))
    subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def wait_for_bridge(seconds: float = 7.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if bridge_ready(timeout=0.4):
            return True
        time.sleep(0.2)
    return False


def edge_from_registry() -> str:
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
    for hive in (HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER):
        try:
            with OpenKey(hive, subkey) as key:
                value, _kind = QueryValueEx(key, "")
                if value and Path(value).exists():
                    return str(value)
        except OSError:
            continue
    fallback = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    return str(fallback) if fallback.exists() else ""


def open_dev_deck() -> None:
    EDGE_PROFILE.mkdir(parents=True, exist_ok=True)
    edge = edge_from_registry()
    url = deck_url()
    if edge:
        args = [
            "--new-window",
            f"--app={url}",
            f"--user-data-dir={EDGE_PROFILE}",
            "--window-size=1440,920",
            "--window-position=80,60",
        ]
        log(f"Opening Edge app window: {url}")
        subprocess.Popen([edge, *args], cwd=str(ROOT))
    else:
        log(f"Opening default browser: {url}")
        os.startfile(url)  # type: ignore[attr-defined]


def show_error(message: str) -> None:
    log("ERROR: " + message)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    except Exception:
        pass


def cleanup_state() -> None:
    log("Cleaning up old state and logs")
    # Clean turn logs
    if LOG_DIR.exists():
        for item in LOG_DIR.glob("*"):
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
    # Clean connector state
    if (BRIDGE_DIR / "connector_state.json").exists():
        (BRIDGE_DIR / "connector_state.json").unlink()


def main() -> int:
    log("Launcher requested")
    try:
        cleanup_state()
        if not bridge_ready(timeout=0.25):
            start_bridge()
            if not wait_for_bridge():
                show_error(
                    "Dev Deck bridge did not start.\n\n"
                    f"Check logs:\n{LOG_PATH}\n{BRIDGE_DIR / 'logs' / 'bridge.log'}"
                )
                return 1
        else:
            log("Bridge already running; reusing it")
        open_dev_deck()
        return 0
    except Exception as exc:
        show_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
