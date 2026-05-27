# Roadmap

## 1. Local Brawn Setup

- Install OpenCode.
- Connect LM Studio.
- Select a fast local model.
- Verify narrow task execution.

## 2. Persistent Project Memory

- Maintain `AGENTS.md`.
- Maintain concise docs under `docs/`.
- Keep current state, decisions, issues, roadmap, and task packet format readable by local agents.

## 3. Task Packet Workflow

- Standardize task packets.
- Require explicit files, actions, restrictions, output format, and stop conditions.
- Use small tasks that can be reviewed quickly.

## 4. Manual ChatGPT To OpenCode Relay

- ChatGPT writes packets.
- User sends packets to OpenCode.
- OpenCode reports results.
- ChatGPT reviews output and decides next steps.

## 5. Semi-Automated Bridge

- Map existing bridge endpoints.
- Design a task queue.
- Add controlled OpenCode execution behind the bridge.
- Store status, logs, output, and errors.

## 6. Safer Autonomous Execution

- Add stronger guardrails.
- Limit commands and file scopes.
- Require stop conditions.
- Review diffs before important changes.
