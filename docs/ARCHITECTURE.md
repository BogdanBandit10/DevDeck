# Architecture

## Components

- Dev Deck launcher: `Dev Deck.exe` and `dev_deck_launcher.py` start or reuse the local bridge, then open Big Window mode.
- Hermes bridge: `hermes_bridge/hermes_bridge.py` exposes local HTTP endpoints and coordinates app state.
- Browser Big Window UI: `hermes_bridge/assets/` contains the local browser interface.
- OpenCode + LM Studio: OpenCode executes scoped repo tasks using LM Studio's OpenAI-compatible local API.
- ChatGPT: planner, reviewer, and task packet author.

## Control Flow

```text
User
  -> ChatGPT plans and reviews
  -> User relays task packet
  -> OpenCode executes scoped task
  -> LM Studio serves local model
  -> OpenCode returns output/diff/errors
  -> User relays result to ChatGPT
```

## App Flow

```text
Dev Deck launcher
  -> starts or reuses Hermes bridge
  -> bridge serves local endpoints
  -> browser opens Big Window UI
  -> UI talks to bridge
```

## Intended Brawn Integration

```text
Big Window UI
  -> Hermes bridge
  -> controlled task queue
  -> OpenCode command executor
  -> LM Studio local model
  -> result stored and returned through bridge
```

The bridge-to-OpenCode boundary should use structured task packets and explicit stop conditions. The UI should not directly improvise shell commands.
