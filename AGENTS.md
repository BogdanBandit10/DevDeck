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

## Brain/Hands/Bridge Architecture

### Roles & Responsibilities

1. **Brain (ChatGPT):** Gathers context (GitHub/snippets), decides architecture, writes exact packets, decides code changes, and reviews results.
2. **Hands (OpenCode):** Reads local files, applies surgical patches, runs exact commands, and reports results/errors/diffs.
3. **Bridge (Dev Deck MCP):** Queues tasks, enforces packet structure, captures git status before/after, and returns results to the Brain.

### Core Mandates

- **Context First:** ChatGPT must determine if it has enough project context before creating any OpenCode task. Prefer GitHub/repo context, exact file paths, or user-provided snippets over asking OpenCode to read the whole codebase.
- **Surgical Execution:** OpenCode tasks must be small, surgical, and spoon-fed. Never ask OpenCode to "think." Ask it only to: **inspect**, **patch**, **execute**, or **report**.
- **No Broad Scopes:** OpenCode should never be asked to broadly understand, refactor, clean up, or explore the whole project.
- **ChatGPT Decides:** ChatGPT decides which files matter, what should change, what code should be written, and what exact verification should run. ChatGPT performs all reasoning and strategy.
- **OpenCode Executes:** OpenCode should only inspect specific files, apply specific edits, run specific commands, and return structured results.
- **Task Packet Structure:** Every OpenCode packet must include: TASK, FILES, ACTIONS, RESTRICTIONS, OUTPUT FORMAT, and STOP CONDITIONS.
- **Small Model Safeguards:** Assume 32k context max and 9B reasoning. Avoid broad prompts. Limit files and changed lines. Require concise output.
- **Result-Review Loop:** ChatGPT reviews all results, errors, and diffs before deciding the next task.
- **Efficient Retry Strategy:** If a task fails, do NOT resend the full context. Send only the **error**, the **minimal surrounding code**, and the **exact failure point** to preserve the context budget.
- **No Unrelated Changes:** No unrelated refactors or dependency installs. Keep implementations minimal.

### Codex-Based Agents
Codex-based agents (e.g., subagents) are also expected to follow the Brain/Hands philosophy, but they can handle significantly larger prompts and more complex reasoning tasks. Use Codex for repo-wide investigations, multi-file refactors, and comprehensive verification.

### Short System Rule
Always treat ChatGPT as the planner/reviewer and OpenCode as a small local executor. Use GitHub, exact paths, or snippets for context first. Send OpenCode only tiny, explicit, surgical tasks with strict restrictions and structured output.
