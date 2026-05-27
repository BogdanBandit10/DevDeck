# Decisions

## Local Brawn

- Decision: Use OpenCode instead of Aider.
- Reason: OpenCode is better suited for workspace-aware task execution in this repo.

## Local Model

- Decision: Use `qwen3.5-9b-mtp` through LM Studio.
- Reason: It is fast enough for local executor tasks.

## Context Size

- Decision: Use 16K context instead of 65K by default.
- Reason: 16K is faster and sufficient for scoped brawn tasks when project memory files are maintained.

## Planner Role

- Decision: ChatGPT remains the planner and reviewer.
- Reason: Architecture, sequencing, and review decisions should stay with the stronger planning model.

## Executor Role

- Decision: The local model is executor, not architect.
- Reason: OpenCode + LM Studio should perform narrow inspections, edits, commands, and reports from explicit task packets.
