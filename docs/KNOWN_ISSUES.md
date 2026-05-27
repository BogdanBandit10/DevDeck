# Known Issues

- OpenCode/local model can loop on repeated shell commands if tasks are too broad.
- Avoid broad recursive audits unless they are explicitly needed.
- Prefer exact file reads over broad searches.
- For large tasks, inspect one file at a time and report before continuing.
- A `Popen` timeout argument is not a valid process startup control for this launcher pattern; use health checks and bounded polling instead.
- Do not rely on conversation context alone. Keep durable state in `docs/`.
- Runtime logs and local profile folders may contain transient machine state.
- Do not modify `Dev Deck.exe` unless explicitly asked.
