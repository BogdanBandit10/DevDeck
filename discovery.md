# Discovery: Task Execution Failure (All Talk, No Action)

## Issue Summary
The Dev Deck assistant personas (e.g., Assistant, Forge) are claiming background tasks from the bridge but marking them as "completed" after providing only a conversational acknowledgment. Despite Dev Deck being a "face-lifted" Codex CLI (which is inherently capable of tool use), no actual file modifications or shell commands are being executed. The "Brain" is currently acting as if it has no "Hands," even though it is the same engine.

## Root Cause Analysis
1. **Engine Misuse:** Dev Deck is designed as an interface for the Codex CLI. The Codex CLI *is* the executor. However, the bridge's `CodexRuntime.chat` method (in `runtime.py`) is treating the engine as a pure text generator. It parses the CLI's JSONL output specifically for `agent_message` events and ignores any tool execution signals or side effects.
2. **Persona Behavior & Prompt Leakage:** The system prompts in `hermes_bridge/assistant_protocol.py` instruct agents like **Forge** to "PERFORM THE WORK IMMEDIATELY." However, `hermes_bridge/runtime.py` wraps messages in a way that seems to prioritize a conversational response. The model (currently `gpt-5.5`) is prioritizing the "helpful assistant" persona over the "executor" mandate, often responding with "Understood" despite instructions not to.
3. **Execution Path Mismatch:** 
   - The bridge's `run_codex_task_capture` function sends a multi-line "Task Packet" (TASK, FILES, ACTIONS, etc.) as the instruction to `codex exec`.
   - This complex formatting likely causes the Codex CLI to "chat" about the packet instead of parsing the `TASK` section as a direct command to execute.
4. **The "Hands" Paradox:** While `OpenCode` is designated as the specialist "Hands," the primary `Codex` backend should be able to function autonomously. The bridge currently lacks a robust verification loop that confirms a file was actually touched or a command actually succeeded before allowing the LLM to say "I'm done."

## Implementation Findings (Session 2026-05-29)
- **Prompt Simplification:** The bridge-to-Codex prompt was successfully refactored to prioritize direct `TASK:` instructions, removing the complex JSON packet injection that was confusing the engine.
- **Event Capture:** Updated `runtime.py` and `hermes_bridge.py` now successfully capture `command_run`, `file_change`, and `command_execution` events.
- **Persistent Conversational Bias:** Despite prompt simplification and explicit instructions to "PERFORM THE WORK IMMEDIATELY" and "DO NOT say 'Understood'", the model still frequently responds with conversational filler ("Understood. I'll...") and marks tasks as completed without actually performing the work in many cases.
- **Conclusion:** The bridge's communication with the Codex CLI engine is now functionally correct, but the underlying LLM's adherence to the system persona instructions is insufficient for reliable autonomous tool invocation.

## Current Status
- **Bridge-Engine Handoff:** ✅ Streamlined/Correct
- **Tool Event Parsing:** ✅ Implemented
- **Autonomous Tool Execution:** ❌ Unreliable/Conversational Bias persists

---
*Created by Gemini CLI during session 2026-05-29*
