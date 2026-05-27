# Dev Deck Agent Instructions

## Project Purpose

Dev Deck is a local Windows launcher and browser UI for the Hermes bridge. It starts a local bridge server, then opens Big Window mode in an isolated Microsoft Edge app window.

ChatGPT should be treated as the planner, reviewer, and architecture brain. OpenCode with the local LM Studio model should be treated as the executor for narrow, explicit tasks.

## Important Files

- `Dev Deck.exe` - packaged launcher for normal use. Do not touch unless explicitly asked.
- `dev_deck_launcher.py` - Python launcher source and primary readable entry point.
- `hermes_bridge/hermes_bridge.py` - local HTTP bridge server.
- `hermes_bridge/assistant_protocol.py` - assistant roster, roles, and task protocol.
- `hermes_bridge/config.json` - bridge configuration, including the server port.
- `hermes_bridge/assets/` - Big Window UI files.
- `opencode.json` - OpenCode provider configuration for LM Studio.
- `docs/PROJECT_STATE.md` - current repo map and known risks.
- `docs/BRAWN_WORKFLOW.md` - task packet workflow for local brawn execution.

## Launching The App

Normal launch:

```powershell
.\Dev Deck.exe
```

Script launch for inspection or debugging:

```powershell
python .\dev_deck_launcher.py
```

The launcher starts `hermes_bridge/hermes_bridge.py` with `hermes_bridge/config.json`, waits for the local health endpoint, then opens `http://127.0.0.1:44888/big-window` unless the config overrides the port.

## OpenCode Working Rules

- Work from explicit task packets. Do not infer broad architecture changes.
- Prefer exact file reads over broad recursive scans.
- For large tasks, inspect one file at a time and report findings before continuing.
- Do not repeatedly run the same shell command. If a command fails or returns no useful information, change approach or ask for guidance.
- Do not run command loops, watch commands, long-running servers, or repeated polling unless explicitly requested.
- Never modify source code unless the task explicitly asks for code changes.
- Do not modify `Dev Deck.exe` unless explicitly asked.
- Do not refactor, rename, move, or reformat unrelated files.
- Show diffs before changing important files when practical.
- Keep edits small, reversible, and limited to the named files.
- Report commands run, files read, files changed, and anything unverified.

## Local Model Limits

The local LM Studio model is useful for execution, inspection, and small edits. It should not make product, architecture, or broad refactor decisions. For ambiguous or high-impact changes, stop and return a concise report for ChatGPT/user review.
