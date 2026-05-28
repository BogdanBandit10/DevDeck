# Executor Routing

Dev Deck coordinates planning, task packets, local queue state, and results. It follows a strict **Brain/Hands** philosophy.

## System Roles

- **Brain (ChatGPT):** Planner, architect, reviewer, and task splitter. The Brain determines project context (using GitHub, exact paths, or snippets) *before* issuing any local tasks.
- **Hands (OpenCode + LM Studio):** Surgical local executor for tiny, spoon-fed task packets.
- **Hands (Codex):** Stronger cloud executor for implementation, repo-wide reasoning, and larger tasks. Codex follows Brain/Hands but can handle more context and complexity.
- **Dev Deck:** Coordination and interface layer.

## Core Routing Rules

1. **Context First:** The Brain must not ask the Hands (OpenCode) to "explore" or "understand" the codebase. The Brain provides the context or specific file paths first.
2. **Surgical OpenCode Tasks:** OpenCode tasks must be tiny, explicit, and restricted. Never ask OpenCode to "think." Ask it only to: **inspect**, **patch**, **execute**, or **report**.
3. **Brain Decides Everything:** The Brain decides files, changes, code, and verification. ChatGPT performs all reasoning and strategy.
4. **Hands-Only Execution:** The Hands only inspect, edit, run, and report.
5. **OpenCode Safeguards:** Assume 32k context and 9B reasoning ability. Limit files and changed lines.
6. **Result-Review Loop:** The Brain reviews every task result (including errors and diffs) before deciding the next step.
7. **Efficient Retry Strategy:** If a task fails, do NOT resend the full context. Send only the **error**, the **minimal surrounding code**, and the **exact failure point**.
8. **Git Tracking:** Tasks track `git status` before/after to distinguish pre-existing dirty files from those changed by the run.

## When To Use OpenCode Local

Use OpenCode only for tiny tasks with explicit packets and small context needs.

- Reading one or two named files.
- Making a small, precise edit in one file.
- Running one specified command.
- Producing a short diff or summary.

OpenCode **must** receive a full packet with TASK, FILES, ACTIONS, RESTRICTIONS, OUTPUT FORMAT, and STOP CONDITIONS.

For Brain-to-Brawn command routing, use the Brawn inbox instead of direct execution:

```json
{
  "source": "Brain",
  "task_id": "task_123",
  "instruction": "Open src/stores/activityStore.ts and return the ActivityEvent type only.",
  "allowed_actions": ["read_file"],
  "approval_required": false
}
```

Commands with any `source` other than `Brain` are rejected in Version 1.

## When To Ask ChatGPT First

Ask ChatGPT before routing work when the task is ambiguous, strategic, or likely to affect architecture.

Ask ChatGPT first for:

- Architecture decisions.
- Multi-phase implementation plans.
- Broad repo audits.
- Security, data-loss, or release-risk decisions.
- Tasks with unclear ownership or unclear files.
- Anything that may require changing queue semantics, bridge behavior, or launcher behavior.
- Any task where the right executor is not obvious.

ChatGPT should convert broad goals into smaller `CODEX_PROMPT`, `OPENCODE_PACKET`, or `REVIEW_REQUEST` items.

## OpenCode Task Size Limits

OpenCode/local should stay inside a 16K-context workflow.

- Prefer one or two files.
- Avoid broad repo audits.
- Avoid architecture decisions.
- Avoid ambiguous tasks.
- Avoid recursive scans unless the packet explicitly requests them.
- Avoid long command output.
- Stop when more context is needed instead of guessing.
- Report stop conditions clearly in the result.

If a task needs more than a small packet can describe, route it to ChatGPT for planning or Codex for execution.

## Standard Output Formats

### CODEX_PROMPT

Use this for Codex execution.

```text
CODEX_PROMPT
Goal:
- What to accomplish.

Context:
- Relevant files, decisions, or constraints.

Scope:
- What is included.
- What is excluded.

Verification:
- Commands or runtime checks to run.

Output:
- What to report back.
```

### OPENCODE_PACKET

Use this for OpenCode/local execution.

```text
OPENCODE_PACKET
TASK
One narrow objective.

FILES
- Exact file path.
- Optional second exact file path.

ACTIONS
1. Read the named file(s).
2. Perform only the requested work.
3. Report the result.

RESTRICTIONS
- Do not broaden scope.
- Do not make architecture decisions.
- Do not touch Dev Deck.exe.
- Do not repeat the same command.
- Stop if instructions are ambiguous.

OUTPUT FORMAT
- Files read:
- Commands run:
- Files changed:
- Diff or summary:
- Errors:
- Unverified:

STOP CONDITIONS
- Stop if a named file is missing.
- Stop if broader context is required.
- Stop if the command hangs or needs interaction.
```

### REVIEW_REQUEST

Use this when asking ChatGPT or Codex to review work.

```text
REVIEW_REQUEST
Subject:
- What should be reviewed.

Files:
- Changed or relevant files.

Review focus:
- Bugs.
- Regressions.
- Missing tests.
- Maintainability risks.

Output:
- Findings first, ordered by severity.
- Open questions.
- Residual risk.
```

### RESULT_SUMMARY

Use this after any executor finishes.

```json
{
  "summary": "What was done or why work stopped.",
  "files_read": ["relative/path.ext"],
  "commands_run": ["exact command text"],
  "files_changed": ["relative/path.ext"],
  "diff": "Diff text or concise summary.",
  "errors": "Errors encountered, or an empty string.",
  "unverified": "Anything not checked or not proven."
}
```

## Examples

### Route To Codex

```text
Goal:
Implement a queue dashboard change that touches HTML, CSS, JavaScript, and runtime verification.

Route:
Codex.

Reason:
The task spans multiple frontend concerns, needs careful integration with existing UI behavior, and should be verified after implementation.
```

### Route To OpenCode Local

```text
TASK
Read docs/TASK_PACKET_SPEC.md and report whether it mentions stop conditions.

FILES
- docs/TASK_PACKET_SPEC.md

ACTIONS
1. Read only the named file.
2. Report the answer and quote the relevant heading names.

RESTRICTIONS
- No edits.
- No recursive scans.
- Stop after reporting.

OUTPUT FORMAT
- Files read:
- Findings:
- Unverified:
```

### Route Back To ChatGPT

```text
Goal:
Decide whether Dev Deck should automatically launch OpenCode from queued tasks.

Route:
ChatGPT first.

Reason:
This is an architecture and safety decision. It affects execution boundaries, subprocess policy, queue ownership, and user approval flow.
```

## Safety Rules

- No automatic OpenCode execution yet.
- No file watching, background workers, or subprocess launching for OpenCode routing.
- Brawn commands must enter through the Brain-only inbox.
- Side agents must request Brawn help from Brain instead of calling Brawn directly.
- Human approval is required for local file edits by OpenCode/local.
- Commit or checkpoint before larger changes.
- Use GitHub and current repo state as the source of truth.
- Keep queue records as coordination state, not authorization.
- Prefer exact file reads and explicit commands.
- Stop and ask for planning when instructions are ambiguous.
- Do not modify `Dev Deck.exe` unless explicitly requested.
