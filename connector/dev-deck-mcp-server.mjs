#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import * as z from "zod/v4";

const PORT = Number(process.env.DEV_DECK_MCP_PORT || 3000);
const HOST = process.env.DEV_DECK_MCP_HOST || "127.0.0.1";
const HERMES_BASE_URL = (process.env.DEV_DECK_HERMES_URL || "http://127.0.0.1:44888").replace(/\/+$/, "");
const CONNECTOR_TOKEN = process.env.DEV_DECK_CONNECTOR_TOKEN || "";
const ALLOW_NO_AUTH = process.env.DEV_DECK_CONNECTOR_ALLOW_NO_AUTH !== "0";
const MAX_BODY_BYTES = Number(process.env.DEV_DECK_MCP_MAX_BODY_BYTES || 1024 * 128);
const ALLOWED_HOSTS = (process.env.DEV_DECK_MCP_ALLOWED_HOSTS || [
  "127.0.0.1",
  "localhost",
  "buy-oxide-course-selections.trycloudflare.com"
].join(","))
  .split(",")
  .map((host) => host.trim())
  .filter(Boolean);

const REQUIRED_PACKET_FIELDS = ["TASK", "FILES", "ACTIONS", "RESTRICTIONS", "OUTPUT FORMAT", "STOP CONDITIONS"];
const BLOCKED_PATTERNS = [
  /dev\s*deck\.exe/i,
  /\bgit\s+reset\b/i,
  /\bgit\s+checkout\s+--\b/i,
  /\brm\s+-rf\b/i,
  /\bremove-item\b.*\b-recurse\b/i,
  /\bdel\s+\/[sq]\b/i,
  /\bformat\b.*\b\/q\b/i,
  /\bwatch\b/i,
  /\bwhile\s*\(/i,
  /\bwhile\s+\$true\b/i,
  /\bstart-process\b/i,
  /\bbackground\b/i,
  /\bserver\b.*\bkeep\b/i
];

const nonEmptyString = z.string().trim().min(1);
const nonEmptyStringArray = z.array(nonEmptyString).min(1);
const packetSchema = z.object({
  TASK: nonEmptyString,
  FILES: nonEmptyStringArray,
  ACTIONS: nonEmptyStringArray,
  RESTRICTIONS: nonEmptyStringArray,
  "OUTPUT FORMAT": nonEmptyStringArray,
  "STOP CONDITIONS": nonEmptyStringArray
});

function textContent(value) {
  return {
    content: [{ type: "text", text: JSON.stringify(value, null, 2) }]
  };
}

function checkAuth(req) {
  if (!CONNECTOR_TOKEN && ALLOW_NO_AUTH) return true;
  const header = req.headers.authorization || "";
  return header === `Bearer ${CONNECTOR_TOKEN}`;
}

function validatePacket(packet) {
  const parsed = packetSchema.safeParse(packet);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    throw new Error(`${issue.path.join(".") || "packet"}: ${issue.message}`);
  }
  const combined = JSON.stringify(parsed.data);
  const match = BLOCKED_PATTERNS.find((pattern) => pattern.test(combined));
  if (match) throw new Error(`Packet rejected by safety rule: ${match}`);
  return parsed.data;
}

async function hermesFetch(path, options = {}) {
  const res = await fetch(`${HERMES_BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || data.message || `Hermes request failed: ${res.status}`);
  }
  return data;
}

function createServer() {
  const server = new McpServer(
    {
      name: "dev-deck-local-opencode",
      version: "0.2.0"
    },
    {
      capabilities: {
        tools: {}
      }
    }
  );

  server.registerTool(
    "queue_opencode_task",
    {
      title: "Queue OpenCode Task",
      description: "Create a pending Dev Deck task for local OpenCode execution after local approval.",
      inputSchema: {
        packet: packetSchema
      }
    },
    async ({ packet }) => {
      const validatedPacket = validatePacket(packet);
      const data = await hermesFetch("/task-queue/tasks", {
        method: "POST",
        body: JSON.stringify({ packet: validatedPacket, source: "chatgpt-mcp" })
      });
      return textContent({
        ok: true,
        task_id: data.task_id || data.task?.task_id,
        status: data.task?.status || "pending",
        message: "Task queued. Open Dev Deck locally and approve it before OpenCode runs."
      });
    }
  );

  server.registerTool(
    "list_opencode_tasks",
    {
      title: "List OpenCode Tasks",
      description: "List recent Dev Deck task queue records.",
      inputSchema: {
        status: z.enum(["pending", "running", "completed", "failed"]).optional(),
        limit: z.number().int().min(1).max(100).optional()
      }
    },
    async ({ status, limit = 20 }) => {
      const query = new URLSearchParams();
      if (status) query.set("status", status);
      query.set("limit", String(Math.min(Math.max(Number(limit || 20), 1), 100)));
      const data = await hermesFetch(`/task-queue/tasks?${query.toString()}`);
      return textContent({
        ok: true,
        tasks: (data.tasks || []).map((task) => ({
          task_id: task.task_id,
          status: task.status,
          title: task.packet?.TASK || "",
          claimed_by: task.claimed_by || "",
          updated_at: task.updated_at || task.created_at || "",
          error: task.error || ""
        }))
      });
    }
  );

  server.registerTool(
    "get_opencode_task",
    {
      title: "Get OpenCode Task",
      description: "Fetch one Dev Deck task by id, including status, packet, result, and error.",
      inputSchema: {
        task_id: nonEmptyString
      }
    },
    async ({ task_id }) => {
      const data = await hermesFetch(`/task-queue/tasks/${encodeURIComponent(task_id)}`);
      return textContent({ ok: true, task: data.task });
    }
  );

  return server;
}

function jsonRpcError(res, status, code, message) {
  res.status(status).json({
    jsonrpc: "2.0",
    error: { code, message },
    id: null
  });
}

const app = createMcpExpressApp({ host: HOST, allowedHosts: ALLOWED_HOSTS });

app.use((req, res, next) => {
  const length = Number(req.headers["content-length"] || 0);
  if (length > MAX_BODY_BYTES) {
    return jsonRpcError(res, 413, -32000, "Request body is too large");
  }
  return next();
});

app.get(["/", "/health"], (_req, res) => {
  res.json({
    ok: true,
    service: "dev-deck-mcp-server",
    transport: "streamable-http",
    mcp_endpoint: "/mcp",
    hermes_base_url: HERMES_BASE_URL,
    allowed_hosts: ALLOWED_HOSTS,
    auth_required: Boolean(CONNECTOR_TOKEN) || !ALLOW_NO_AUTH
  });
});

app.post("/mcp", async (req, res) => {
  if (!checkAuth(req)) {
    return jsonRpcError(res, 401, -32001, "missing or invalid bearer token");
  }

  const server = createServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined
  });
  res.on("close", () => {
    transport.close().catch(() => {});
    server.close().catch(() => {});
  });

  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (error) {
    console.error("Error handling MCP request:", error);
    if (!res.headersSent) {
      jsonRpcError(res, 500, -32603, "Internal server error");
    }
  }
});

app.get("/mcp", (_req, res) => {
  jsonRpcError(res, 405, -32000, "Method not allowed. POST JSON-RPC MCP requests to this endpoint.");
});

app.delete("/mcp", (_req, res) => {
  jsonRpcError(res, 405, -32000, "Method not allowed.");
});

app.use((error, _req, res, _next) => {
  if (res.headersSent) return;
  jsonRpcError(res, 400, -32700, error?.message || "Invalid JSON request");
});

app.listen(PORT, HOST, (error) => {
  if (error) {
    console.error("Failed to start Dev Deck MCP server:", error);
    process.exit(1);
  }
  if (!CONNECTOR_TOKEN && ALLOW_NO_AUTH) {
    console.warn("Dev Deck MCP server is running with no auth. Only expose this tunnel while you are actively using it.");
  }
  console.log(`Dev Deck MCP server listening on http://${HOST}:${PORT}/mcp`);
});
