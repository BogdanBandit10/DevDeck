# OpenCode Workspace Inspection Report

## Project Name
**Dev Deck** - Local AI assistant bridge with big-window chat interface

## Entry Point
- **Primary:** `dev_deck_launcher.py` (Python script)
- **Secondary:** `Dev Deck.exe` (standalone executable)

## Bridge Script
- **Path:** `hermes_bridge/hermes_bridge.py`
- **Purpose:** Local HTTP server bridging to external Hermes agent system

## Local Server Port
- **Host:** 127.0.0.1
- **Port:** 44888 (configurable via `hermes_bridge/config.json`)
- **Health endpoint:** http://127.0.0.1:44888/health
- **Big-window endpoint:** http://127.0.0.1:44888/big-window

## Summary of Inspection
OpenCode successfully inspected the following components:

| Component | Status | Details |
|-----------|--------|---------|
| Project structure | ✅ Inspected | 9 top-level items, 3 subdirectories |
| Entry point logic | ✅ Analyzed | `dev_deck_launcher.py` - startup sequence mapped |
| Bridge server | ✅ Identified | `hermes_bridge.py` with config at port 44888 |
| Configuration | ✅ Read | `config.json` contains 60+ settings |
| Browser integration | ✅ Documented | Edge launcher with isolated profile, fixed window size/position |
| Launch scripts | ✅ Found | Python launcher + compiled exe |
| Documentation | ✅ Reviewed | README_DEV_DECK.md provides usage instructions |

## Key Findings
- Project wraps external Hermes agent system in browser-based UI
- Uses isolated Edge user-data-dir for clean sessions
- Bridge process runs hidden with no visible console output
- Startup includes 7-second wait timeout for bridge readiness
- Config supports async tasks, crew feedback, and model switching features
