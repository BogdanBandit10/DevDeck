# Project State

## Current Summary

Dev Deck is a local Windows project that launches a Hermes bridge server and opens a Big Window browser interface. The repo currently contains the packaged launcher, Python launcher source, bridge server files, UI assets, OpenCode configuration, and local runtime logs.

## Entry Point

- Packaged app: `Dev Deck.exe`
- Readable launcher source: `dev_deck_launcher.py`

The launcher checks whether the bridge is healthy, starts it if needed, waits briefly, then opens the Big Window URL in an isolated Microsoft Edge app profile.

## Bridge Script

- Script: `hermes_bridge/hermes_bridge.py`
- Config: `hermes_bridge/config.json`
- Protocol definitions: `hermes_bridge/assistant_protocol.py`

## Local Server Port

- Host: `127.0.0.1`
- Default port: `44888`
- Health endpoint: `http://127.0.0.1:44888/health`
- Big Window endpoint: `http://127.0.0.1:44888/big-window`

The default port can be overridden by `hermes_bridge/config.json`.

## Important Files And Folders

- `.hermes/` - local app profile/support folder.
- `Dev Deck.exe` - normal user-facing launcher. Do not modify unless explicitly asked.
- `dev_deck_launcher.py` - launcher source.
- `hermes_bridge/` - bridge server, config, assets, status, and logs.
- `hermes_bridge/assets/` - Big Window frontend assets.
- `hermes_bridge/logs/` - runtime logs and task records.
- `opencode.json` - OpenCode provider config for LM Studio at `http://127.0.0.1:1234/v1`.
- `README_DEV_DECK.md` - existing short project README.
- `OPENCODE_TEST.md` - prior OpenCode inspection notes.
- `docs/BRAWN_WORKFLOW.md` - workflow for ChatGPT planning and OpenCode execution.

## Known Risks

- `Dev Deck.exe` is a generated binary and should not be edited by local agents.
- The bridge uses local ports and process startup timing; repeated launch attempts can create confusing state.
- Runtime logs under `hermes_bridge/logs/` may grow or contain transient local state.
- The `.hermes/` Edge profile is local machine state, not application source.
- Local agents can get stuck in broad scans or repeated shell commands if tasks are not tightly scoped.
- Source changes should be made only from explicit instructions and reviewed with diffs.

## Current Local-Agent Setup

- Brawn agent: OpenCode.
- Local model: `qwen3.5-9b-mtp` through LM Studio.
- LM Studio API base URL: `http://127.0.0.1:1234/v1`.
- Planner/reviewer: ChatGPT.
- Executor role: OpenCode/local model handles narrow file reads, small edits, command runs, and reports results back.
