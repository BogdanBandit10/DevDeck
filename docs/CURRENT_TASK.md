# Current Task

## Phase

Setting up the ChatGPT brain + OpenCode local brawn workflow.

## Status

Research and durable project-memory setup are in progress. The goal is to make OpenCode with LM Studio useful as a controlled executor under ChatGPT planning.

## Completed Setup

- OpenCode installed.
- LM Studio connected through `opencode.json`.
- Local model selected: `qwen3.5-9b-mtp`.
- Root `AGENTS.md` created.
- Project state and brawn workflow docs created.
- Task history started.

## Next Steps

- Map bridge endpoints.
- Design a precise task packet schema.
- Define a command queue boundary between Dev Deck and OpenCode.
- Keep manual ChatGPT to OpenCode relay working before adding automation.

## Local Brawn Restrictions

- Do not modify source code unless explicitly asked.
- Do not modify `Dev Deck.exe`.
- Do not refactor.
- Prefer exact file reads over broad recursive scans.
- Do not repeatedly run the same shell command.
- Stop and report if a task becomes ambiguous or starts looping.
