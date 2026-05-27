#!/usr/bin/env python3
"""Durable local task packet queue for manual brawn execution."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


REQUIRED_PACKET_FIELDS = ("TASK", "FILES", "ACTIONS", "RESTRICTIONS", "OUTPUT FORMAT", "STOP CONDITIONS")
VALID_STATUSES = {"pending", "running", "completed", "failed"}
TERMINAL_STATUSES = {"completed", "failed"}


class TaskQueueError(Exception):
    status_code = 500


class TaskQueueValidationError(TaskQueueError):
    status_code = 400


class TaskQueueNotFoundError(TaskQueueError):
    status_code = 404


class TaskQueueTransitionError(TaskQueueError):
    status_code = 409


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_task_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"task_{stamp}_{uuid.uuid4().hex[:6]}"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TaskQueueValidationError(f"Task file is not a JSON object: {path.name}")
    return data


def normalize_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise TaskQueueValidationError(f"{field} must be a list of strings")
    items = [str(item).strip() for item in value if str(item).strip()]
    if not items:
        raise TaskQueueValidationError(f"{field} must contain at least one item")
    return items


def normalize_packet(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TaskQueueValidationError("Task packet must be a JSON object")
    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else payload
    missing = [field for field in REQUIRED_PACKET_FIELDS if field not in packet]
    if missing:
        raise TaskQueueValidationError("Missing required packet fields: " + ", ".join(missing))
    task = str(packet.get("TASK") or "").strip()
    if not task:
        raise TaskQueueValidationError("TASK must be a non-empty string")
    return {
        "TASK": task,
        "FILES": normalize_string_list(packet.get("FILES"), "FILES"),
        "ACTIONS": normalize_string_list(packet.get("ACTIONS"), "ACTIONS"),
        "RESTRICTIONS": normalize_string_list(packet.get("RESTRICTIONS"), "RESTRICTIONS"),
        "OUTPUT FORMAT": normalize_string_list(packet.get("OUTPUT FORMAT"), "OUTPUT FORMAT"),
        "STOP CONDITIONS": normalize_string_list(packet.get("STOP CONDITIONS"), "STOP CONDITIONS"),
    }


def default_result() -> dict[str, Any]:
    return {
        "summary": "",
        "files_read": [],
        "commands_run": [],
        "files_changed": [],
        "diff": "",
        "errors": "",
        "unverified": "",
    }


def append_event(task: dict[str, Any], event_type: str, message: str, **extra: Any) -> None:
    events = task.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        task["events"] = events
    events.append({
        "at": now_iso(),
        "type": event_type,
        "message": message,
        **extra,
    })


class DurableTaskQueue:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.results_dir = self.root / "results"
        self.logs_dir = self.root / "logs"
        self.lock = threading.RLock()
        self.queue_errors: list[str] = []
        self.ensure_dirs()
        self.recover_running_tasks()

    def ensure_dirs(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def task_path(self, task_id: str) -> Path:
        if not task_id.startswith("task_") or any(ch in task_id for ch in "\\/"):
            raise TaskQueueValidationError("Invalid task_id")
        return self.tasks_dir / f"{task_id}.json"

    def create(self, payload: dict[str, Any], source: str = "manual") -> dict[str, Any]:
        packet = normalize_packet(payload)
        task_id = new_task_id()
        created_at = now_iso()
        task = {
            "schema_version": 1,
            "task_id": task_id,
            "status": "pending",
            "source": str(source or "manual"),
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": None,
            "completed_at": None,
            "packet": packet,
            "result": default_result(),
            "error": "",
            "events": [],
        }
        append_event(task, "created", "Task created.")
        with self.lock:
            atomic_write_json(self.task_path(task_id), task)
        return task

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status and status not in VALID_STATUSES:
            raise TaskQueueValidationError(f"Invalid status: {status}")
        limit = max(1, min(int(limit or 50), 500))
        tasks: list[dict[str, Any]] = []
        errors: list[str] = []
        with self.lock:
            for path in self.tasks_dir.glob("task_*.json"):
                try:
                    task = read_json(path)
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
                    continue
                if status and task.get("status") != status:
                    continue
                tasks.append(task)
        tasks.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        self.queue_errors = errors[-20:]
        return tasks[:limit]

    def get(self, task_id: str) -> dict[str, Any] | None:
        path = self.task_path(task_id)
        if not path.exists():
            return None
        with self.lock:
            return read_json(path)

    def require(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if not task:
            raise TaskQueueNotFoundError(f"No task found with id {task_id}")
        return task

    def update_status(self, task_id: str, status: str, result: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise TaskQueueValidationError(f"Invalid status: {status}")
        with self.lock:
            task = self.require(task_id)
            current = str(task.get("status") or "")
            self.validate_transition(current, status)
            task["status"] = status
            task["updated_at"] = now_iso()
            if status == "running":
                task["started_at"] = task.get("started_at") or now_iso()
                append_event(task, "running", "Task marked running.")
            elif status == "completed":
                task["completed_at"] = now_iso()
                task["result"] = self.normalize_result(result)
                task["error"] = ""
                append_event(task, "completed", "Task completed.")
                self.write_result_file(task)
            elif status == "failed":
                task["completed_at"] = now_iso()
                task["error"] = str(error or "")
                if result is not None:
                    task["result"] = self.normalize_result(result)
                append_event(task, "failed", task["error"] or "Task failed.")
                self.write_result_file(task)
            atomic_write_json(self.task_path(task_id), task)
        return task

    def mark_running(self, task_id: str) -> dict[str, Any]:
        return self.update_status(task_id, "running")

    def mark_completed(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self.update_status(task_id, "completed", result=result)

    def mark_failed(self, task_id: str, error: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.update_status(task_id, "failed", result=result, error=error)

    def validate_transition(self, current: str, new: str) -> None:
        if current not in VALID_STATUSES:
            raise TaskQueueTransitionError(f"Task has invalid current status: {current}")
        if current in TERMINAL_STATUSES:
            raise TaskQueueTransitionError(f"Cannot change terminal task status: {current}")
        allowed = {
            "pending": {"running", "failed"},
            "running": {"completed", "failed"},
        }
        if new == current:
            return
        if new not in allowed.get(current, set()):
            raise TaskQueueTransitionError(f"Invalid status transition: {current} -> {new}")

    def normalize_result(self, result: dict[str, Any] | None) -> dict[str, Any]:
        if result is None:
            return default_result()
        if not isinstance(result, dict):
            raise TaskQueueValidationError("result must be a JSON object")
        normalized = default_result()
        normalized.update(result)
        return normalized

    def write_result_file(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            return
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        lines = [
            f"# {task_id}",
            "",
            f"Status: {task.get('status')}",
            "",
            "## Summary",
            "",
            str(result.get("summary") or ""),
            "",
            "## Errors",
            "",
            str(task.get("error") or result.get("errors") or ""),
            "",
            "## Unverified",
            "",
            str(result.get("unverified") or ""),
        ]
        path = self.results_dir / f"{task_id}.md"
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines).rstrip() + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def recover_running_tasks(self) -> None:
        with self.lock:
            for path in self.tasks_dir.glob("task_*.json"):
                try:
                    task = read_json(path)
                    if task.get("status") != "running":
                        continue
                    task["status"] = "failed"
                    task["updated_at"] = now_iso()
                    task["completed_at"] = now_iso()
                    task["error"] = "Bridge restarted while task was running; manual review required."
                    append_event(task, "recovered_failed", task["error"])
                    atomic_write_json(path, task)
                except Exception as exc:
                    self.queue_errors.append(f"{path.name}: {exc}")
