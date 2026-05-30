# ChatGPT Connector Setup

Dev Deck exposes ChatGPT-facing tools through a local Streamable HTTP MCP server. The server queues tasks into the existing Hermes task queue. OpenCode runs only after local approval in the Dev Deck Queue view.

## Local Services

1. Start Dev Deck normally.

```powershell
.\Dev Deck.exe
```

2. Start the connector facade from the repo root.

```powershell
npm run connector
```

The connector listens on `http://127.0.0.1:3000/mcp` and proxies to Hermes at `http://127.0.0.1:44888`.
By default it allows ChatGPT's `No Auth` setup. To require bearer auth instead, set both:

```powershell
$env:DEV_DECK_CONNECTOR_TOKEN = "choose-a-long-random-token"
$env:DEV_DECK_CONNECTOR_ALLOW_NO_AUTH = "0"
```

The default allowed host list includes `buy-oxide-course-selections.trycloudflare.com`, `127.0.0.1`, and `localhost`. If Cloudflare gives you a different hostname, set it before starting the connector:

```powershell
$env:DEV_DECK_MCP_ALLOWED_HOSTS = "127.0.0.1,localhost,your-name.trycloudflare.com"
```

3. Open the tunnel.

```powershell
cloudflared tunnel --url http://127.0.0.1:3000
```

4. In ChatGPT Developer Mode, add the connector with the Cloudflare forwarding URL plus `/mcp`.

For the current no-auth setup:

- Authentication: `No Auth`
- URL: `https://buy-oxide-course-selections.trycloudflare.com/mcp`

The browser view of `/mcp` is expected to reject GET requests. ChatGPT must POST JSON-RPC MCP requests to `/mcp`, including `initialize`, `tools/list`, and `tools/call`.

## Exposed Tools

- `queue_opencode_task` creates a pending task from a validated `OPENCODE_PACKET`.
- `list_opencode_tasks` lists recent task status.
- `get_opencode_task` returns one task with packet, result, status, and error.

The connector does not expose shell access, filesystem access, raw bridge control endpoints, shutdown, or direct OpenCode execution.

## Protocol Smoke Test

```powershell
$body = @{
  jsonrpc = "2.0"
  id = 1
  method = "initialize"
  params = @{
    protocolVersion = "2024-11-05"
    capabilities = @{}
    clientInfo = @{
      name = "chatgpt"
      version = "1.0"
    }
  }
} | ConvertTo-Json -Depth 10 -Compress

Invoke-WebRequest `
  -Method Post `
  -Uri "http://127.0.0.1:3000/mcp" `
  -ContentType "application/json" `
  -Headers @{ Accept = "application/json, text/event-stream" } `
  -Body $body
```

## Approved Local Execution

Open the Queue tab in Dev Deck, select a pending task, then click `Approve & Run`.

Approved execution runs:

```powershell
opencode run <OPENCODE_PACKET> --model lmstudio/qwen3.5-9b-mtp
```

The bridge captures stdout, stderr, exit code, duration, and git status before/after the run. Successful runs mark the task completed; failed or timed-out runs mark it failed with the captured output.

## Safety Defaults

- Local approval is required before OpenCode runs.
- `Dev Deck.exe` edits and destructive/broad command patterns are rejected at the connector.
- OpenCode runs from the repo root.
- Runtime behavior is configured in `hermes_bridge/config.json`.
