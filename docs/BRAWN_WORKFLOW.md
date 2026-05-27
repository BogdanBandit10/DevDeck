# Brawn Workflow

## Roles

- ChatGPT: brain, planner, reviewer, and task packet writer.
- OpenCode + LM Studio: brawn executor for scoped repo tasks.
- User: relays task packets to OpenCode and returns results, diffs, errors, or questions to ChatGPT.

OpenCode should execute narrow instructions. It should not decide architecture, expand task scope, or perform broad cleanup without a new task packet.

## Standard Task Packet

```text
TASK
What to accomplish in one or two sentences.

FILES
- Exact files or folders to inspect or edit.

ACTIONS
1. Read the named file(s).
2. Perform the requested command or edit.
3. Report the result.

RESTRICTIONS
- Do not modify source unless explicitly allowed.
- Do not touch Dev Deck.exe unless explicitly allowed.
- Do not repeatedly run the same command.
- Prefer exact file reads over broad recursive scans.
- Stop and report if the task becomes ambiguous.

OUTPUT FORMAT
- Files read:
- Commands run:
- Files changed:
- Diff or summary:
- Errors/unverified:
```

## Loop Prevention Rules

- Run each shell command once unless there is a clear reason to retry with a changed command.
- Do not poll, watch, or keep a dev server running unless the task explicitly asks for it.
- If a command fails twice, stop and report the exact command and error.
- If a broad search returns too much output, stop and ask for narrower files.
- For large tasks, inspect one file, report findings, then wait for the next packet.

## Example: Inspect

```text
TASK
Inspect the launcher startup flow and report how the bridge starts.

FILES
- dev_deck_launcher.py

ACTIONS
1. Read dev_deck_launcher.py.
2. Identify the startup sequence, port, and browser open behavior.
3. Do not edit anything.

RESTRICTIONS
- No recursive scans.
- No source edits.
- Do not run the app.

OUTPUT FORMAT
- Files read:
- Findings:
- Questions:
```

## Example: Edit

```text
TASK
Update documentation to clarify the local server port.

FILES
- docs/PROJECT_STATE.md

ACTIONS
1. Read docs/PROJECT_STATE.md.
2. Edit only the local server section.
3. Show the diff.

RESTRICTIONS
- Do not modify application source.
- Do not modify Dev Deck.exe.
- Do not reformat unrelated sections.

OUTPUT FORMAT
- Files read:
- Files changed:
- Diff:
- Unverified:
```

## Example: Run Test

```text
TASK
Run a single requested verification command and report the result.

FILES
- dev_deck_launcher.py

ACTIONS
1. Run: python -m py_compile dev_deck_launcher.py
2. Report success or the exact error.

RESTRICTIONS
- Run the command once.
- Do not launch Dev Deck.
- Do not edit files.

OUTPUT FORMAT
- Commands run:
- Result:
- Errors:
```

## Example: Report Errors

```text
TASK
Read the bridge startup logs and summarize the latest error.

FILES
- hermes_bridge/logs/bridge.log
- hermes_bridge/logs/dev_deck_launcher.log

ACTIONS
1. Read only the latest relevant log entries.
2. Summarize the likely cause.
3. Recommend the next narrow task packet.

RESTRICTIONS
- Do not delete logs.
- Do not restart processes.
- Do not edit files.

OUTPUT FORMAT
- Files read:
- Latest error:
- Likely cause:
- Suggested next task:
```
