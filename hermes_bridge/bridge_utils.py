#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import re
import uuid
from datetime import datetime
from typing import Any
from pathlib import Path
from state import LOG_DIR, STATUS_PATH

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def append_log(name: str, text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / name).open("a", encoding="utf-8", errors="replace") as f:
        f.write(f"[{now_iso()}] {text}\n")

def expand_path(value: Any, default: Path | None = None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return default
    expanded = os.path.expandvars(os.path.expanduser(text))
    path = Path(expanded)
    if not path.is_absolute() and default is not None:
        return (default / path).resolve()
    return path

def write_status(**values: Any) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
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

def normalized_command(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9/ ]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def first_text(payload: Any, fields: list[str]) -> str:
    if not isinstance(payload, dict): return ""
    for f in fields:
        val = payload.get(f)
        if val and isinstance(val, str): return val
    return ""
