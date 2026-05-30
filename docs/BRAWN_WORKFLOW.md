# Brain/Hands Workflow

## Roles

- **Brain (ChatGPT):** Planner, reviewer, and task packet writer. Determines context using GitHub, exact paths, or snippets before tasking.
- **Hands (OpenCode + LM Studio):** Surgical local executor for narrow, spoon-fed repo tasks.
- **User:** Relays task packets and returns structured results to the Brain.

OpenCode must execute narrow, explicit instructions. It should never broadly understand, refactor, or explore the codebase.

## Core Mandates

1. **Context First:** ChatGPT must determine if it has enough context before creating a task. Prefer external context over local exploration.
2. **Surgical Scope:** Tasks must be tiny and spoon-fed. Never ask OpenCode to "think." Ask it only to: **inspect**, **patch**, **execute**, or **report**.
3. **Brain Decides:** ChatGPT decides files, changes, and verification. ChatGPT performs all reasoning and strategy.
4. **Hands Executes:** OpenCode only inspects, edits, runs, and reports.
5. **Small Model Safeguards:** Assume 32k context and 9B reasoning. Limit files/lines.
6. **Result-Review Loop:** ChatGPT reviews every result before the next step.
7. **Efficient Retry Strategy:** If a task fails, do NOT resend the full context. Send only the **error**, the **minimal surrounding code**, and the **exact failure point** to preserve the context budget.
8. **Git Tracking:** Every task must track `git status` before and after. `files_changed` should only list files newly modified by the task.
9. **Verification Loop:** Every packet must include a verification action. The Bridge will report this in a `verification` field.

## Standard Task Packet

Every OpenCode packet must include these fields:

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
- No broad recursive scans.
- Run commands once.
- Assume 32k context / 9B reasoning.

OUTPUT FORMAT
- Files read:
- Commands run:
- Files changed:
- Diff or summary:
- Errors/unverified:

STOP CONDITIONS
- Stop if the file is missing.
- Stop if the task is ambiguous.
- Stop if a command fails twice.
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
