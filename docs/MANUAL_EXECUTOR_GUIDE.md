# Manual Executor Guide

This guide documents how a human-operated OpenCode executor consumes one queued Dev Deck task. The bridge stores task packets and results only. It does not launch OpenCode, execute subprocesses, run background workers, or decide how the task should be performed.

## Workflow

The manual executor workflow is:

```text
ChatGPT task packet
  -> POST /task-queue/tasks
  -> pending task JSON
  -> human lists pending tasks
  -> human claims one task
  -> human reads the packet
  -> human runs the task in OpenCode
  -> human submits the result
  -> human marks the task completed or failed
```

Use the queue as a coordination record. OpenCode remains a separate manual tool operated by the user.

## Base URL

The default local bridge URL is:

```text
http://127.0.0.1:44888
```

If `hermes_bridge/config.json` overrides the port, use that configured port instead.

## List Pending Tasks

List pending tasks before choosing work. Keep the limit small enough to scan.

```powershell
curl.exe -s "http://127.0.0.1:44888/task-queue/tasks?status=pending&limit=20"
```

List all recent tasks:

```powershell
curl.exe -s "http://127.0.0.1:44888/task-queue/tasks?limit=20"
```

## Claim A Task

Claiming a task moves it from `pending` to `running` and records advisory ownership in `claimed_by` and `claimed_at`. Ownership is local coordination, not authentication.

```powershell
curl.exe -s -X POST "http://127.0.0.1:44888/task-queue/tasks/task_YYYYMMDD_HHMMSS_abcdef/claim" `
  -H "Content-Type: application/json" `
  -d "{\"executor\":\"opencode-user\",\"note\":\"Starting manual execution.\"}"
```

Only pending tasks can be claimed. A second claim on the same task should return a conflict.

## Get The Task Packet

Read the packet-only view after claiming. This is the input to give OpenCode.

```powershell
curl.exe -s "http://127.0.0.1:44888/task-queue/tasks/task_YYYYMMDD_HHMMSS_abcdef/packet"
```

Get the full task record, including status, result, and history:

```powershell
curl.exe -s "http://127.0.0.1:44888/task-queue/tasks/task_YYYYMMDD_HHMMSS_abcdef"
```

## Execute Safely In OpenCode

Give OpenCode only the packet contents and the project rules it needs. OpenCode should execute the packet exactly as written.

Required execution rules:

- Work from the task packet only.
- Do not expand scope or make architecture decisions.
- Prefer exact file reads over broad scans.
- Do not run broad recursive commands unless the packet explicitly requests them.
- Do not repeat the same command more than once.
- Do not run watch commands, polling loops, long-running servers, or background tasks unless the packet explicitly asks for them.
- Do not modify source code unless the packet explicitly allows edits.
- Do not touch `Dev Deck.exe` unless the packet explicitly allows it.
- Stop and fail the task if instructions are ambiguous.
- Report files read, commands run, files changed, diffs or summaries, errors, and anything unverified.

If OpenCode needs broader context, stop and return that need as the result instead of improvising.

## Submit Results

Submit results after OpenCode has produced its report. This stores output without marking the task terminal.

```powershell
curl.exe -s -X POST "http://127.0.0.1:44888/task-queue/tasks/task_YYYYMMDD_HHMMSS_abcdef/result" `
  -H "Content-Type: application/json" `
  -d "{\"executor\":\"opencode-user\",\"result\":{\"summary\":\"Inspected the requested file and found no source changes were needed.\",\"files_read\":[\"docs/TASK_PACKET_SPEC.md\"],\"commands_run\":[],\"files_changed\":[],\"diff\":\"\",\"errors\":\"\",\"unverified\":\"No tests were run because the packet requested documentation inspection only.\"}}"
```

The result may also be submitted as top-level result fields:

```powershell
curl.exe -s -X POST "http://127.0.0.1:44888/task-queue/tasks/task_YYYYMMDD_HHMMSS_abcdef/result" `
  -H "Content-Type: application/json" `
  -d "{\"executor\":\"opencode-user\",\"summary\":\"Task report submitted.\",\"files_read\":[\"AGENTS.md\"],\"commands_run\":[],\"files_changed\":[],\"diff\":\"\",\"errors\":\"\",\"unverified\":\"No verification command was requested.\"}"
```

## Mark Completed

Mark the task completed after submitting a satisfactory result. If the result was already submitted, the completion request can include only the executor.

```powershell
curl.exe -s -X POST "http://127.0.0.1:44888/task-queue/tasks/task_YYYYMMDD_HHMMSS_abcdef/complete" `
  -H "Content-Type: application/json" `
  -d "{\"executor\":\"opencode-user\"}"
```

You may also complete with a final result in one request:

```powershell
curl.exe -s -X POST "http://127.0.0.1:44888/task-queue/tasks/task_YYYYMMDD_HHMMSS_abcdef/complete" `
  -H "Content-Type: application/json" `
  -d "{\"executor\":\"opencode-user\",\"result\":{\"summary\":\"Task completed.\",\"files_read\":[\"AGENTS.md\",\"docs/BRAWN_WORKFLOW.md\"],\"commands_run\":[],\"files_changed\":[],\"diff\":\"\",\"errors\":\"\",\"unverified\":\"No tests were requested.\"}}"
```

Completed tasks are terminal and cannot be changed.

## Mark Failed

Fail the task when OpenCode cannot safely complete it, when instructions are ambiguous, when required files are missing, or when the task hits a stop condition.

```powershell
curl.exe -s -X POST "http://127.0.0.1:44888/task-queue/tasks/task_YYYYMMDD_HHMMSS_abcdef/fail" `
  -H "Content-Type: application/json" `
  -d "{\"executor\":\"opencode-user\",\"error\":\"Task instructions were ambiguous; stopped instead of guessing.\",\"result\":{\"summary\":\"Stopped before making changes.\",\"files_read\":[],\"commands_run\":[],\"files_changed\":[],\"diff\":\"\",\"errors\":\"Ambiguous instructions.\",\"unverified\":\"No work was completed.\"}}"
```

Failed tasks are terminal and cannot be changed.

## Result Format

The standard result object is:

```json
{
  "summary": "What was done or why work stopped.",
  "files_read": ["relative/path.ext"],
  "commands_run": ["exact command text"],
  "files_changed": ["relative/path.ext"],
  "diff": "Diff text or a concise summary when no diff exists.",
  "errors": "Errors encountered, or an empty string.",
  "unverified": "Anything not checked or not proven."
}
```

Successful documentation-only example:

```json
{
  "summary": "Read the requested docs and wrote the manual executor guide.",
  "files_read": ["AGENTS.md", "docs/TASK_PACKET_SPEC.md", "docs/BRAWN_WORKFLOW.md"],
  "commands_run": [],
  "files_changed": ["docs/MANUAL_EXECUTOR_GUIDE.md"],
  "diff": "Added a guide for listing, claiming, reading, reporting, completing, and failing manual tasks.",
  "errors": "",
  "unverified": "No tests were run because this was documentation-only."
}
```

Failure example:

```json
{
  "summary": "Stopped before execution because the packet did not identify the target file.",
  "files_read": [],
  "commands_run": [],
  "files_changed": [],
  "diff": "",
  "errors": "Missing exact file path in FILES.",
  "unverified": "No task work was performed."
}
```

Extra result fields are allowed for forward compatibility, but the standard fields should always be present.

## Loop Prevention

Use these rules for every manual execution:

- Do not run broad recursive commands unless requested.
- Do not repeat the same command more than once.
- Prefer exact file reads.
- Stop and fail the task if instructions are ambiguous.
- Stop if output is too large and report the need for a narrower packet.
- Stop if a command hangs or needs interactive input.
- Stop if the packet would require touching files outside its scope.

## Restart Behavior

Pending tasks stay pending across bridge restarts. Completed and failed tasks remain terminal. Running tasks are failed on bridge startup with a manual-review error so an operator can inspect whether work was interrupted.
