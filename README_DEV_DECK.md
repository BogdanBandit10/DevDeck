# Dev Deck

Use `Dev Deck.exe` to start Big Window mode.

This folder is the clean entry point for the current main project. The old VBS and Unity/widget launch files still exist in `widget-phase5` for backup/history, but normal Dev Deck work should start here.

Important files:

- `Dev Deck.exe` - standalone launcher with the Dev Deck icon.
- `dev_deck_launcher.py` - source for the launcher exe.
- `hermes_bridge/hermes_bridge.py` - local bridge server.
- `hermes_bridge/assistant_protocol.py` - agent roster, roles, and task protocol.
- `hermes_bridge/assets/` - big-window UI assets.
- `connector/dev-deck-mcp-server.mjs` - ChatGPT-facing MCP facade for queue tools.
- `docs/CHATGPT_CONNECTOR_SETUP.md` - local connector and ngrok setup.

Runtime logs are written under `hermes_bridge/logs/` when the launcher runs.
