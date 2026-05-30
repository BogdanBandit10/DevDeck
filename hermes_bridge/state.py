#!/usr/bin/env python3
from __future__ import annotations
import threading
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from assistant_protocol import TaskRegistry
    from task_queue import DurableTaskQueue

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
TURN_LOG_DIR = LOG_DIR / "turns"
ASSETS_DIR = ROOT / "assets"
STATUS_PATH = ROOT / "bridge_status.json"
SUPERVISOR_STATE_PATH = ROOT / "supervisor_state.json"
CONNECTOR_STATE_PATH = ROOT / "connector_state.json"
CONNECTOR_LOG_PATH = LOG_DIR / "chatgpt_connector.log"

class BridgeState:
    cfg: dict[str, Any] = {}
    runtime: Any = None  # HermesPersistentRuntime
    connector_supervisor: Any = None  # ChatGPTConnectorSupervisor
    task_queue: DurableTaskQueue | None = None
    activity: list[dict[str, Any]] = []
    activity_lock = threading.RLock()
    chatter_tasks: set[str] = set()
    chatter_lock = threading.RLock()
    started_at: float = time.time()
    shutting_down: bool = False
