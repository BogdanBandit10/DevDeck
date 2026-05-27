# Task Packet Spec

Use this exact structure for OpenCode brawn tasks.

```text
TASK
One concise objective.

FILES
- Exact file or folder paths.

ACTIONS
1. Ordered steps to perform.

RESTRICTIONS
- Limits on edits, commands, scope, and retries.

OUTPUT FORMAT
- Required result fields.

STOP CONDITIONS
- When to stop and report instead of continuing.
```

## Example: Inspect One File

```text
TASK
Inspect the launcher startup flow.

FILES
- dev_deck_launcher.py

ACTIONS
1. Read the file.
2. Summarize startup sequence, port handling, and browser launch.

RESTRICTIONS
- Do not edit files.
- Do not run the app.
- Do not scan other files.

OUTPUT FORMAT
- Files read:
- Findings:
- Questions:

STOP CONDITIONS
- Stop if the file is missing.
- Stop if more files seem necessary and ask for the next packet.
```

## Example: Edit One File

```text
TASK
Update one documentation section.

FILES
- docs/PROJECT_STATE.md

ACTIONS
1. Read the file.
2. Edit only the named section.
3. Show the diff.

RESTRICTIONS
- Do not edit source code.
- Do not reformat unrelated sections.
- Do not touch Dev Deck.exe.

OUTPUT FORMAT
- Files read:
- Files changed:
- Diff:
- Unverified:

STOP CONDITIONS
- Stop if the requested section is unclear.
- Stop if the edit requires source changes.
```

## Example: Run Build Or Test

```text
TASK
Run one verification command.

FILES
- dev_deck_launcher.py

ACTIONS
1. Run: python -m py_compile dev_deck_launcher.py
2. Report the exact result.

RESTRICTIONS
- Run the command once.
- Do not edit files.
- Do not launch Dev Deck.

OUTPUT FORMAT
- Commands run:
- Result:
- Errors:

STOP CONDITIONS
- Stop after one run.
- Stop if the command hangs or needs interactive input.
```

## Example: Report Errors

```text
TASK
Summarize the latest bridge startup error.

FILES
- hermes_bridge/logs/bridge.log
- hermes_bridge/logs/dev_deck_launcher.log

ACTIONS
1. Read only recent relevant entries.
2. Summarize the likely cause.
3. Suggest the next narrow packet.

RESTRICTIONS
- Do not delete logs.
- Do not restart processes.
- Do not edit files.

OUTPUT FORMAT
- Files read:
- Latest error:
- Likely cause:
- Suggested next packet:

STOP CONDITIONS
- Stop if logs are missing.
- Stop if output is too large and ask for a narrower time range.
```

## Example: Stop A Tool Loop

```text
TASK
Stop repeated tool use and report state.

FILES
- The file or command already being inspected.

ACTIONS
1. Do not repeat the same failed command.
2. Report the last command, last output, and why progress stopped.
3. Ask for a narrower next packet.

RESTRICTIONS
- No retries of the same command.
- No broad recursive scans.
- No edits.

OUTPUT FORMAT
- Last command:
- Last output:
- Why stopped:
- Needed next packet:

STOP CONDITIONS
- Stop immediately after identifying repeated or low-value tool use.
```
