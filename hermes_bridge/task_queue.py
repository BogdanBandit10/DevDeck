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
LIST_RESULT_FIELDS = {"files_read", "commands_run", "files_changed"}
TEXT_RESULT_FIELDS = {"summary", "diff", "errors", "unverified"}


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
    event = {
        "at": now_iso(),
        "type": event_type,
        "message": message,
        **extra,
    }
    events = task.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        task["events"] = events
    events.append(event)
    history = task.setdefault("execution_history", [])
    if not isinstance(history, list):
        history = []
        task["execution_history"] = history
    history.append(event.copy())


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
        if not (task_id.startswith("task_") or task_id.startswith("tsk_")) or any(ch in task_id for ch in "\\/"):
            raise TaskQueueValidationError("Invalid task_id")
        return self.tasks_dir / f"{task_id}.json"

    def create(self, payload: dict[str, Any], source: str = "manual", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        packet = normalize_packet(payload)
        task_id = "tsk_" + uuid.uuid4().hex[:6]
        created_at = now_iso()
        task = {
            "schema_version": 2,
            "task_id": task_id,
            "status": "pending",
            "source": str(source or "manual"),
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": None,
            "claimed_at": None,
            "claimed_by": "",
            "result_submitted_at": None,
            "completed_at": None,
            "packet": packet,
            "message": str(payload.get("message") or ""),
            "intent": str(payload.get("intent") or "task"),
            "agent_name": str(payload.get("agent_name") or "Hermes"),
            "progress_message": "Queued.",
            "result": default_result(),
            "error": "",
            "events": [],
            "execution_history": [],
            "metadata": metadata or {},
        }
        append_event(task, "created", "Task created.")
        with self.lock:
            atomic_write_json(self.task_path(task_id), task)
        return task

    def start_background(self, task_id: str, runner: Callable[[str, str, str], str]) -> None:
        """Start a background assistant task using a provided runner."""
        thread = threading.Thread(target=self._run_assistant_task, args=(task_id, runner), daemon=True)
        thread.start()

    def _run_assistant_task(self, task_id: str, runner: Callable[[str, str, str], str]) -> None:
        task = self.get(task_id)
        if not task: return
        
        agent_name = task.get("agent_name", "Hermes")
        self.update_status(task_id, "running", progress_message=f"{agent_name} is working.")
        
        try:
            # Runner is usually rt.chat(message, req_id, agent_name)
            message = task.get("message", "")
            response = runner(message, f"async_{task_id}", agent_name)
            self.mark_completed(task_id, result={"summary": response})
        except Exception as exc:
            self.mark_failed(task_id, error=str(exc))

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status and status not in VALID_STATUSES:
            raise TaskQueueValidationError(f"Invalid status: {status}")
        limit = max(1, min(int(limit or 50), 500))
        tasks: list[dict[str, Any]] = []
        errors: list[str] = []
        with self.lock:
            for path in list(self.tasks_dir.glob("task_*.json")) + list(self.tasks_dir.glob("tsk_*.json")):
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

    def update_status(
        self,
        task_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
        executor: str = "",
        progress_message: str = "",
    ) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise TaskQueueValidationError(f"Invalid status: {status}")
        with self.lock:
            task = self.require(task_id)
            current = str(task.get("status") or "")
            self.validate_transition(current, status)
            self.validate_executor(task, executor)
            task["status"] = status
            task["updated_at"] = now_iso()
            if progress_message:
                task["progress_message"] = progress_message
            if status == "running":
                task["started_at"] = task.get("started_at") or now_iso()
                append_event(task, "running", "Task marked running.", executor=str(executor or ""))
            elif status == "completed":
                task["completed_at"] = now_iso()
                if result is not None:
                    task["result"] = self.normalize_result(result)
                    task["result_submitted_at"] = task.get("result_submitted_at") or now_iso()
                task["error"] = ""
                append_event(task, "completed", "Task completed.", executor=str(executor or ""))
                self.write_result_file(task)
            elif status == "failed":
                task["completed_at"] = now_iso()
                task["error"] = str(error or "")
                if result is not None:
                    task["result"] = self.normalize_result(result)
                    task["result_submitted_at"] = task.get("result_submitted_at") or now_iso()
                append_event(task, "failed", task["error"] or "Task failed.", executor=str(executor or ""))
                self.write_result_file(task)
            atomic_write_json(self.task_path(task_id), task)
        return task

    def mark_running(self, task_id: str) -> dict[str, Any]:
        return self.update_status(task_id, "running")

    def claim(self, task_id: str, executor: str = "manual", note: str = "") -> dict[str, Any]:
        executor = str(executor or "manual").strip() or "manual"
        with self.lock:
            task = self.require(task_id)
            current = str(task.get("status") or "")
            if current != "pending":
                raise TaskQueueTransitionError(f"Only pending tasks can be claimed; current status is {current}")
            claimed_by = str(task.get("claimed_by") or "").strip()
            if claimed_by and claimed_by != executor:
                raise TaskQueueTransitionError(f"Task is already claimed by {claimed_by}")
            task["status"] = "running"
            task["started_at"] = task.get("started_at") or now_iso()
            task["claimed_at"] = task.get("claimed_at") or now_iso()
            task["claimed_by"] = executor
            task["updated_at"] = now_iso()
            append_event(task, "claimed", f"Task claimed by {executor}.", executor=executor, note=str(note or ""))
            atomic_write_json(self.task_path(task_id), task)
        return task

    def submit_result(self, task_id: str, result: dict[str, Any], executor: str = "") -> dict[str, Any]:
        with self.lock:
            task = self.require(task_id)
            current = str(task.get("status") or "")
            if current != "running":
                raise TaskQueueTransitionError(f"Results can only be submitted for running tasks; current status is {current}")
            self.validate_executor(task, executor)
            task["result"] = self.normalize_result(result)
            task["result_submitted_at"] = now_iso()
            task["updated_at"] = now_iso()
            append_event(task, "result_submitted", "Task result submitted.", executor=str(executor or ""))
            atomic_write_json(self.task_path(task_id), task)
        return task

    def mark_completed(self, task_id: str, result: dict[str, Any] | None = None, executor: str = "") -> dict[str, Any]:
        return self.update_status(task_id, "completed", result=result, executor=executor)

    def mark_failed(self, task_id: str, error: str, result: dict[str, Any] | None = None, executor: str = "") -> dict[str, Any]:
        return self.update_status(task_id, "failed", result=result, error=error, executor=executor)

    def validate_executor(self, task: dict[str, Any], executor: str = "") -> None:
        claimed_by = str(task.get("claimed_by") or "").strip()
        executor = str(executor or "").strip()
        if claimed_by and executor and executor != claimed_by:
            raise TaskQueueTransitionError(f"Task is claimed by {claimed_by}, not {executor}")

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
        extras = {key: value for key, value in result.items() if key not in normalized}
        for key in TEXT_RESULT_FIELDS:
            normalized[key] = str(result.get(key) or "")
        for key in LIST_RESULT_FIELDS:
            value = result.get(key)
            if value is None or value == "":
                normalized[key] = []
            elif isinstance(value, list):
                normalized[key] = [str(item).strip() for item in value if str(item).strip()]
            else:
                normalized[key] = [str(value).strip()] if str(value).strip() else []
        normalized.update(extras)
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
            for path in list(self.tasks_dir.glob("task_*.json")) + list(self.tasks_dir.glob("tsk_*.json")):
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

    def clear_terminal(self) -> int:
        """Deletes all tasks with a terminal status to keep the queue clean."""
        count = 0
        terminal_states = TERMINAL_STATUSES | {"error", "cancel_requested", "rejected"}
        with self.lock:
            for path in list(self.tasks_dir.glob("task_*.json")) + list(self.tasks_dir.glob("tsk_*.json")):
                try:
                    task = read_json(path)
                    if str(task.get("status") or "") in terminal_states:
                        path.unlink(missing_ok=True)
                        log_path = self.logs_dir / f"{task.get('task_id')}.log"
                        log_path.unlink(missing_ok=True)
                        count += 1
                except Exception:
                    pass
        return count
