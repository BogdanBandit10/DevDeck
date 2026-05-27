const cfg = window.COMMAND_DECK || {};
const endpoint = cfg.endpoint || "/codex/chat";
const stateEndpoint = cfg.stateEndpoint || "/big-window/state";

const app = document.querySelector("#app");
const canvas = document.querySelector("#starfield");
const ctx = canvas.getContext("2d");
const transcript = document.querySelector("#transcript");
const form = document.querySelector("#form");
const input = document.querySelector("#input");
const send = document.querySelector("#send");
const lanes = document.querySelector("#lanes");
const queueLanes = document.querySelector("#queueLanes");
const queueDetail = document.querySelector("#queueDetail");
const queueCount = document.querySelector("#queueCount");
const queueRefresh = document.querySelector("#queueRefresh");
const queueCreateForm = document.querySelector("#queueCreateForm");
const queuePacketInput = document.querySelector("#queuePacketInput");
const queueCreateStatus = document.querySelector("#queueCreateStatus");
const queueTemplate = document.querySelector("#queueTemplate");
const activityEl = document.querySelector("#activity");
const agentGrid = document.querySelector("#agentGrid");
const readyChip = document.querySelector("#readyChip");
const railReady = document.querySelector("#railReady");
const modeTitle = document.querySelector("#modeTitle");
const modeSummary = document.querySelector("#modeSummary");
const turnChip = document.querySelector("#turnChip");
const taskCount = document.querySelector("#taskCount");
const activityCount = document.querySelector("#activityCount");
const agentCount = document.querySelector("#agentCount");
const detailStatus = document.querySelector("#detailStatus");
const detailTitle = document.querySelector("#detailTitle");
const detailBody = document.querySelector("#detailBody");
const copyStatus = document.querySelector("#copyStatus");
const widgetChip = document.querySelector("#widgetChip");
const metricActive = document.querySelector("#metricActive");
const metricDone = document.querySelector("#metricDone");
const metricSuccess = document.querySelector("#metricSuccess");
const metricEvents = document.querySelector("#metricEvents");
const crewSignals = document.querySelector("#crewSignals");
const signalCount = document.querySelector("#signalCount");

const seen = new Set();
let selectedTaskId = null;
let selectedAgentId = null;
let selectedTask = null;
let selectedQueueTaskId = null;
let queueTasks = [];
let queueBusy = false;
let dots = [];

const laneDefs = [
  ["queued", "Queued", "Waiting for an agent claim."],
  ["running", "Running", "Active execution."],
  ["completed", "Done", "Completed task replies."],
  ["error", "Blocked", "Needs attention."]
];

const modeCopy = {
  command: ["Shared channel", "Terminal-style chat shared by the widget, backend, and crew coding agents."],
  team: ["Crew roster", "See which crew member is online, what they handle, and where current work is landing."],
  board: ["Kanban board", "Track background coding work by state so long tasks do not disappear."],
  queue: ["Task queue", "Create manual task packets and inspect durable queue status, packets, results, and errors."],
  log: ["Activity log", "Raw bridge activity from the widget, Dev Deck, control commands, and agent replies."]
};

const queueStatuses = [
  ["pending", "Pending", "Ready for a human executor."],
  ["running", "Running", "Claimed or in manual execution."],
  ["completed", "Completed", "Finished with a result."],
  ["failed", "Failed", "Stopped with an error or restart recovery."]
];

const packetTemplate = {
  TASK: "One concise objective.",
  FILES: ["Exact file or folder path"],
  ACTIONS: ["Read the named files.", "Perform the requested work.", "Report the result."],
  RESTRICTIONS: ["Do not modify source unless explicitly allowed.", "Do not touch Dev Deck.exe.", "Stop if instructions are ambiguous."],
  "OUTPUT FORMAT": ["Files read:", "Commands run:", "Files changed:", "Diff or summary:", "Errors/unverified:"],
  "STOP CONDITIONS": ["Stop if required files are missing.", "Stop if the task needs broader scope."]
};

function short(text, n = 170) {
  text = String(text || "").trim();
  return text.length > n ? text.slice(0, n - 3) + "..." : text;
}

function taskTitle(task, length = 190) {
  return short(task?.message || task?.progress_message || task?.intent || "Background task", length);
}

function statusClass(status) {
  if (status === "running") return "status-running";
  if (status === "completed") return "status-completed";
  if (status === "blocked" || status === "error" || status === "cancel_requested") return "status-blocked";
  if (status === "queued") return "status-queued";
  return "";
}

function renderDetails(item, kind = "session") {
  if (!item) {
    selectedTask = null;
    detailStatus.textContent = "Team feed";
    detailTitle.textContent = "Ready for a coding session";
    detailBody.textContent = "This panel is now your session brief: backend, workspace, bridge status, and the selected crew member/task. Pick a card to turn it into an action.";
    copyStatus.textContent = "Select a crew member or task";
    copyStatus.disabled = true;
    return;
  }
  if (kind === "agent") {
    selectedTask = null;
    detailStatus.textContent = item.status || "standby";
    detailTitle.textContent = item.name || "Agent";
    detailBody.textContent = `Role: ${String(item.role || "assistant").replaceAll("_", " ")}\n\nOwns: ${item.owns || "General assistance"}\n\nHands off to: ${item.hands_off_to || "Hermes when unsure"}\n\nWhy active: ${item.why_active || "Standing by."}\n\nLast work: ${short(item.last_task, 180)}`;
    copyStatus.textContent = "Brief this crew member";
    copyStatus.disabled = false;
    return;
  }
  selectedTask = item;
  detailStatus.textContent = item.status || "task";
  detailTitle.textContent = item.task_id || "Task";
  detailBody.textContent = `${taskTitle(item)}\n\nAgent: ${item.agent_name || "Agent"}\nIntent: ${item.intent || "coding"}`;
  copyStatus.textContent = "Copy task command";
  copyStatus.disabled = false;
}

if (typeof marked !== "undefined" && typeof hljs !== "undefined") {
  marked.setOptions({
    highlight: function(code, lang) {
      const language = hljs.getLanguage(lang) ? lang : 'plaintext';
      return hljs.highlight(code, { language }).value;
    }
  });
}

function addMessage(item) {
  const id = item.id || Math.random().toString(36);
  if (seen.has(id)) return;
  seen.add(id);
  const row = document.createElement("article");
  row.className = item.kind === "user" ? "user" : item.kind === "error" ? "error" : ["chatter", "team", "handoff"].includes(item.kind) ? `agent ${item.kind}` : "agent";
  if (item.agent_name) row.dataset.agent = item.agent_name;
  const label = document.createElement("strong");
  const body = document.createElement("div");
  const badge = item.kind === "chatter" ? " live" : item.kind === "task" ? " task" : item.kind === "team" ? " team" : item.kind === "handoff" ? " handoff" : "";
  label.textContent = item.kind === "user" ? "You" : item.kind === "task" ? `${item.agent_name || "Task"}${badge}` : item.kind === "control" ? "Control" : item.kind === "error" ? "Error" : `${item.agent_name || "Agent"}${badge}`;
  
  if (item.kind !== "user" && item.kind !== "error" && typeof marked !== "undefined") {
    body.className = "markdown-body";
    body.innerHTML = marked.parse(item.text || "");
  } else {
    body.className = "messageBody";
    body.textContent = item.text || "";
  }
  
  row.append(label, body);
  transcript.append(row);
  transcript.scrollTop = transcript.scrollHeight;
}

function renderMetrics(mission = {}) {
  metricActive.textContent = String(mission.tasks_active || 0);
  metricDone.textContent = String(mission.tasks_completed || 0);
  metricSuccess.textContent = `${mission.success_rate ?? 100}%`;
  metricEvents.textContent = String(mission.activity_events || 0);
}

function renderAgents(agents) {
  agentGrid.replaceChildren(...(agents || []).map(agent => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "agentCard";
    card.classList.toggle("isSelected", selectedAgentId === agent.id);
    card.setAttribute("aria-pressed", selectedAgentId === agent.id ? "true" : "false");

    const head = document.createElement("div");
    head.className = "agentHead";
    const name = document.createElement("div");
    name.className = "agentName";
    const strong = document.createElement("strong");
    const role = document.createElement("span");
    const dot = document.createElement("span");
    dot.className = `statusDot ${statusClass(agent.status)}`;
    strong.textContent = agent.name;
    role.textContent = String(agent.role || "assistant").replaceAll("_", " ");
    name.append(strong, role);
    head.append(name, dot);

    const body = document.createElement("p");
    body.textContent = short(agent.why_active || agent.last_task || agent.quip, 130);
    const stats = document.createElement("div");
    stats.className = "agentStats";
    stats.innerHTML = `<span>${agent.status || "standby"}</span><span>${agent.task_count || 0} tasks</span><span>${agent.completed || 0} done</span>`;
    const owns = document.createElement("p");
    owns.className = "agentOwns";
    owns.textContent = `Owns: ${short(agent.owns || "General assistance", 95)}`;
    card.append(head, body, owns, stats);
    card.addEventListener("click", () => {
      selectedAgentId = agent.id;
      selectedTaskId = null;
      renderDetails(agent, "agent");
      renderAgents(agents || []);
      input.value = `/task Ask ${agent.name} to `;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
    return card;
  }));
  agentCount.textContent = `${(agents || []).length} agents`;
}

function renderBoard(tasks) {
  if (selectedTaskId) {
    selectedTask = (tasks || []).find(t => t.task_id === selectedTaskId) || selectedTask;
    renderDetails(selectedTask, "task");
  }
  lanes.replaceChildren(...laneDefs.map(([key, label, help]) => {
    const filtered = (tasks || []).filter(t => key === "error" ? ["error", "cancel_requested"].includes(t.status) : t.status === key);
    const lane = document.createElement("section");
    lane.className = `lane lane-${key}`;
    const head = document.createElement("h3");
    const headLabel = document.createElement("span");
    const headCount = document.createElement("span");
    headLabel.textContent = label;
    headCount.textContent = String(filtered.length);
    head.append(headLabel, headCount);
    const helper = document.createElement("p");
    helper.className = "laneHelp";
    helper.textContent = help;
    const list = document.createElement("div");
    list.className = "laneList";
    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "emptyLane";
      empty.textContent = "No items";
      list.append(empty);
    }
    filtered.forEach(task => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = `taskCard ${task.status || ""}`;
      card.classList.toggle("isSelected", selectedTaskId === task.task_id);
      card.setAttribute("aria-pressed", selectedTaskId === task.task_id ? "true" : "false");
      const id = document.createElement("span");
      const title = document.createElement("span");
      const meta = document.createElement("span");
      id.className = "id";
      title.className = "title";
      meta.className = "meta";
      id.textContent = task.task_id || "task";
      title.textContent = taskTitle(task, 110);
      meta.textContent = `${task.agent_name || "Agent"} / ${task.intent || task.status || ""}`;
      card.append(id, title, meta);
      card.addEventListener("click", () => {
        selectedTaskId = task.task_id || null;
        selectedAgentId = null;
        renderDetails(task, "task");
        renderBoard(tasks || []);
        input.value = `/assistant/task/${task.task_id}`;
        input.focus();
      });
      list.append(card);
    });
    lane.append(head, helper, list);
    return lane;
  }));
  taskCount.textContent = `${(tasks || []).length} tasks`;
}

function queueTaskTitle(task, length = 120) {
  return short(task?.packet?.TASK || task?.task_id || "Queued task", length);
}

function formatQueueList(value) {
  if (Array.isArray(value)) return value.length ? value.join("\n") : "";
  return String(value || "");
}

function appendQueueField(parent, label, value, className = "") {
  const field = document.createElement("section");
  field.className = `queueField ${className}`.trim();
  const title = document.createElement("strong");
  const body = document.createElement("pre");
  title.textContent = label;
  body.textContent = formatQueueList(value) || "None";
  field.append(title, body);
  parent.append(field);
}

function renderQueueDetails(task) {
  if (!queueDetail) return;
  if (!task) {
    queueDetail.replaceChildren(Object.assign(document.createElement("div"), {
      className: "emptyLane",
      textContent: "Select a queue task"
    }));
    return;
  }
  const packet = task.packet || {};
  const result = task.result || {};
  const wrap = document.createElement("div");
  wrap.className = "queueDetailBody";

  const head = document.createElement("div");
  head.className = "queueDetailHead";
  const title = document.createElement("h2");
  const meta = document.createElement("p");
  title.textContent = queueTaskTitle(task, 160);
  meta.textContent = `${task.task_id || "task"} / ${task.status || "unknown"} / updated ${task.updated_at || "unknown"}`;
  head.append(title, meta);

  const statusRow = document.createElement("div");
  statusRow.className = "queueMetaGrid";
  [
    ["Status", task.status],
    ["Source", task.source],
    ["Claimed by", task.claimed_by || "None"],
    ["Created", task.created_at],
    ["Started", task.started_at || "Not started"],
    ["Completed", task.completed_at || "Not completed"]
  ].forEach(([label, value]) => {
    const item = document.createElement("div");
    item.className = "fact";
    const strong = document.createElement("strong");
    const span = document.createElement("span");
    strong.textContent = label;
    span.textContent = String(value || "Unknown");
    item.append(strong, span);
    statusRow.append(item);
  });

  const packetBlock = document.createElement("section");
  packetBlock.className = "queueSection";
  const packetTitle = document.createElement("h3");
  packetTitle.textContent = "Packet";
  packetBlock.append(packetTitle);
  appendQueueField(packetBlock, "TASK", packet.TASK);
  appendQueueField(packetBlock, "FILES", packet.FILES);
  appendQueueField(packetBlock, "ACTIONS", packet.ACTIONS);
  appendQueueField(packetBlock, "RESTRICTIONS", packet.RESTRICTIONS);
  appendQueueField(packetBlock, "OUTPUT FORMAT", packet["OUTPUT FORMAT"]);
  appendQueueField(packetBlock, "STOP CONDITIONS", packet["STOP CONDITIONS"]);

  const resultBlock = document.createElement("section");
  resultBlock.className = "queueSection";
  const resultTitle = document.createElement("h3");
  resultTitle.textContent = "Result and error";
  resultBlock.append(resultTitle);
  appendQueueField(resultBlock, "Summary", result.summary);
  appendQueueField(resultBlock, "Files read", result.files_read);
  appendQueueField(resultBlock, "Commands run", result.commands_run);
  appendQueueField(resultBlock, "Files changed", result.files_changed);
  appendQueueField(resultBlock, "Diff", result.diff, "queueDiff");
  appendQueueField(resultBlock, "Errors", task.error || result.errors);
  appendQueueField(resultBlock, "Unverified", result.unverified);

  const historyBlock = document.createElement("section");
  historyBlock.className = "queueSection";
  const historyTitle = document.createElement("h3");
  historyTitle.textContent = "History";
  historyBlock.append(historyTitle);
  const history = Array.isArray(task.execution_history) ? task.execution_history : task.events || [];
  if (!history.length) {
    const empty = document.createElement("p");
    empty.className = "queueEmptyText";
    empty.textContent = "No history recorded.";
    historyBlock.append(empty);
  } else {
    history.slice().reverse().forEach(event => {
      const row = document.createElement("div");
      row.className = "queueHistoryItem";
      const strong = document.createElement("strong");
      const body = document.createElement("span");
      strong.textContent = `${event.type || "event"} / ${event.at || ""}`;
      body.textContent = event.message || "";
      row.append(strong, body);
      historyBlock.append(row);
    });
  }

  wrap.append(head, statusRow, packetBlock, resultBlock, historyBlock);
  queueDetail.replaceChildren(wrap);
}

function renderQueueDashboard(tasks = queueTasks) {
  if (!queueLanes) return;
  const selected = tasks.find(task => task.task_id === selectedQueueTaskId) || null;
  if (!selected && selectedQueueTaskId) selectedQueueTaskId = null;
  queueLanes.replaceChildren(...queueStatuses.map(([status, label, help]) => {
    const filtered = tasks.filter(task => task.status === status);
    const lane = document.createElement("section");
    lane.className = `lane queueLane lane-${status}`;
    const head = document.createElement("h3");
    const headLabel = document.createElement("span");
    const headCount = document.createElement("span");
    headLabel.textContent = label;
    headCount.textContent = String(filtered.length);
    head.append(headLabel, headCount);
    const helper = document.createElement("p");
    helper.className = "laneHelp";
    helper.textContent = help;
    const list = document.createElement("div");
    list.className = "laneList";
    if (!filtered.length) {
      list.append(Object.assign(document.createElement("div"), {
        className: "emptyLane",
        textContent: "No items"
      }));
    }
    filtered.forEach(task => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = `taskCard queueTaskCard ${task.status || ""}`;
      card.classList.toggle("isSelected", selectedQueueTaskId === task.task_id);
      card.setAttribute("aria-pressed", selectedQueueTaskId === task.task_id ? "true" : "false");
      const id = document.createElement("span");
      const title = document.createElement("span");
      const meta = document.createElement("span");
      id.className = "id";
      title.className = "title";
      meta.className = "meta";
      id.textContent = task.task_id || "task";
      title.textContent = queueTaskTitle(task);
      meta.textContent = `${task.claimed_by || "unclaimed"} / ${task.updated_at || task.created_at || ""}`;
      card.append(id, title, meta);
      card.addEventListener("click", async () => {
        selectedQueueTaskId = task.task_id || null;
        await loadQueueTask(selectedQueueTaskId);
      });
      list.append(card);
    });
    lane.append(head, helper, list);
    return lane;
  }));
  queueCount.textContent = `${tasks.length} tasks`;
  renderQueueDetails(selected);
}

async function loadQueueTasks() {
  if (!queueLanes || queueBusy) return;
  queueBusy = true;
  if (queueRefresh) queueRefresh.disabled = true;
  try {
    const res = await fetch("/task-queue/tasks?limit=100", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.error || data.message || "Could not load queue");
    queueTasks = Array.isArray(data.tasks) ? data.tasks : [];
    renderQueueDashboard(queueTasks);
  } catch (error) {
    if (queueCreateStatus) queueCreateStatus.textContent = error.message;
  } finally {
    queueBusy = false;
    if (queueRefresh) queueRefresh.disabled = false;
  }
}

async function loadQueueTask(taskId) {
  if (!taskId) {
    renderQueueDetails(null);
    return;
  }
  try {
    const res = await fetch(`/task-queue/tasks/${encodeURIComponent(taskId)}`, { cache: "no-store" });
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.error || data.message || "Could not load task");
    const task = data.task || null;
    queueTasks = queueTasks.map(item => item.task_id === taskId ? task : item);
    renderQueueDashboard(queueTasks);
    renderQueueDetails(task);
  } catch (error) {
    if (queueCreateStatus) queueCreateStatus.textContent = error.message;
  }
}

function renderActivity(items) {
  const recent = (items || []).slice(-26).reverse();
  activityEl.replaceChildren(...recent.map(item => {
    const row = document.createElement("div");
    row.className = "event";
    const kind = document.createElement("strong");
    const body = document.createElement("p");
    kind.textContent = item.kind || "event";
    body.textContent = short(item.text, 190);
    row.append(kind, body);
    return row;
  }));
  activityCount.textContent = String((items || []).length);
}

function renderSignals(items, tasks) {
  if (!crewSignals || !signalCount) return;
  const signalItems = (items || [])
    .filter(item => ["agent", "task", "chatter", "team", "handoff", "error"].includes(item.kind) && (item.agent_name || item.intent || item.task_id))
    .slice(-4)
    .reverse();
  const runningTasks = (tasks || [])
    .filter(task => ["running", "queued"].includes(task.status))
    .slice(0, Math.max(0, 4 - signalItems.length))
    .map(task => ({
      kind: "task",
      agent_name: task.agent_name || "Agent",
      status: task.status || "queued",
      text: task.progress_message || task.message || "Working.",
      task_id: task.task_id
    }));
  const merged = [...runningTasks, ...signalItems].slice(0, 4);
  if (!merged.length) {
    crewSignals.replaceChildren(Object.assign(document.createElement("p"), {
      className: "emptySignal",
      textContent: "No live crew notes yet. Launch a task or ask for ideas."
    }));
    signalCount.textContent = "0 live";
    return;
  }
  crewSignals.replaceChildren(...merged.map(item => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `signalItem ${item.kind || ""} ${item.status || ""}`;
    const who = document.createElement("strong");
    const body = document.createElement("span");
    who.textContent = item.agent_name || (item.kind === "task" ? "Task" : "Agent");
    body.textContent = short(item.text, 120);
    row.append(who, body);
    row.addEventListener("click", () => {
      if (item.task_id) {
        setView("board");
        selectedTaskId = item.task_id;
      } else {
        setView("command");
      }
    });
    return row;
  }));
  signalCount.textContent = `${merged.length} live`;
}

function setView(view) {
  app.classList.toggle("view-command", view === "command");
  app.classList.toggle("view-team", view === "team");
  app.classList.toggle("view-board", view === "board");
  app.classList.toggle("view-queue", view === "queue");
  app.classList.toggle("view-log", view === "log");
  const copy = modeCopy[view] || modeCopy.command;
  modeTitle.textContent = copy[0];
  modeSummary.textContent = copy[1];
  document.querySelectorAll("[data-tab]").forEach(button => {
    button.classList.toggle("isActive", button.dataset.tab === view);
    button.setAttribute("aria-pressed", button.dataset.tab === view ? "true" : "false");
  });
  if (view === "command") {
    transcript.scrollTop = transcript.scrollHeight;
    input.focus();
  }
  if (view === "queue") {
    loadQueueTasks();
  }
}

async function refresh() {
  try {
    const res = await fetch(stateEndpoint, { cache: "no-store" });
    const data = await res.json();
    readyChip.textContent = data.busy ? "Working" : data.ready ? "Ready" : "Offline";
    railReady.textContent = readyChip.textContent;
    turnChip.textContent = `${data.turns || 0} turns`;
    widgetChip.textContent = data.widget?.connected ? "Online" : "Idle";
    (data.activity || []).forEach(addMessage);
    renderMetrics(data.mission || {});
    renderAgents(data.agents || []);
    renderBoard(data.tasks || []);
    renderActivity(data.activity || []);
    renderSignals(data.activity || [], data.tasks || []);
  } catch (error) {
    readyChip.textContent = "Bridge lost";
    railReady.textContent = "Bridge lost";
  }
}

async function sendMessage(text) {
  input.disabled = true;
  send.disabled = true;
  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, taskId: "command-deck" })
    });
    const data = await res.json();
    const responseText = data.response || data.message || data.error || "";
    if (responseText) {
      addMessage({
        id: data.request_id || "response-" + Date.now(),
        kind: data.success === false || data.ok === false ? "error" : data.task_id ? "task" : "agent",
        text: responseText
      });
    }
    await refresh();
  } catch (error) {
    addMessage({ id: "local-" + Date.now(), kind: "error", text: "Could not send that: " + error.message });
  } finally {
    input.disabled = false;
    send.disabled = false;
    input.focus();
  }
}

function resizeStars() {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(window.innerWidth * ratio);
  canvas.height = Math.floor(window.innerHeight * ratio);
  canvas.style.width = `${window.innerWidth}px`;
  canvas.style.height = `${window.innerHeight}px`;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const count = Math.min(120, Math.max(42, Math.floor(window.innerWidth / 13)));
  dots = Array.from({ length: count }, (_, i) => {
    const lane = i % 5;
    const cyan = "98, 215, 255";
    const green = "82, 221, 137";
    const violet = "167, 139, 250";
    return {
      x: Math.random() * window.innerWidth,
      y: Math.random() * window.innerHeight,
      len: Math.random() * 46 + 18,
      v: Math.random() * .38 + .12,
      drift: (lane - 2) * 0.035,
      a: Math.random() * .38 + .16,
      color: lane === 1 ? green : lane === 3 ? violet : cyan,
      pulse: Math.random() * Math.PI * 2
    };
  });
}

function drawStars() {
  ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
  const w = window.innerWidth;
  const h = window.innerHeight;
  const t = performance.now() * 0.001;

  ctx.save();
  ctx.lineWidth = 1;
  for (const dot of dots) {
    dot.y += dot.v;
    dot.x += dot.drift;
    if (dot.y > h + dot.len + 12) {
      dot.y = -dot.len - 12;
      dot.x = Math.random() * w;
    }
    if (dot.x < -30) dot.x = w + 30;
    if (dot.x > w + 30) dot.x = -30;
    const shimmer = dot.a + Math.sin(t * 2.4 + dot.pulse) * 0.07;
    const grad = ctx.createLinearGradient(dot.x, dot.y - dot.len, dot.x, dot.y);
    grad.addColorStop(0, `rgba(${dot.color}, 0)`);
    grad.addColorStop(.54, `rgba(${dot.color}, ${Math.max(0.04, shimmer * .55)})`);
    grad.addColorStop(1, `rgba(${dot.color}, ${Math.max(0.08, shimmer)})`);
    ctx.strokeStyle = grad;
    ctx.beginPath();
    ctx.moveTo(dot.x, dot.y - dot.len);
    ctx.lineTo(dot.x + dot.drift * 70, dot.y);
    ctx.stroke();

    ctx.beginPath();
    ctx.fillStyle = `rgba(${dot.color}, ${Math.max(0.12, shimmer)})`;
    ctx.arc(dot.x + dot.drift * 70, dot.y, 1.35, 0, Math.PI * 2);
    ctx.fill();
  }

  // A few faint scanlines make the deck feel like a live coding surface instead of empty space.
  ctx.globalAlpha = .08;
  ctx.strokeStyle = "rgba(98, 215, 255, 1)";
  for (let y = (t * 18) % 88; y < h; y += 88) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  ctx.restore();

  requestAnimationFrame(drawStars);
}

form.addEventListener("submit", event => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  sendMessage(text);
});

document.querySelectorAll("[data-prompt]").forEach(button => {
  button.addEventListener("click", () => {
    input.value = button.dataset.prompt || "";
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });
});

document.querySelectorAll("[data-tab]").forEach(button => {
  button.addEventListener("click", () => setView(button.dataset.tab || "command"));
});

if (queuePacketInput) {
  queuePacketInput.value = JSON.stringify(packetTemplate, null, 2);
}

if (queueTemplate) {
  queueTemplate.addEventListener("click", () => {
    queuePacketInput.value = JSON.stringify(packetTemplate, null, 2);
    queuePacketInput.focus();
  });
}

if (queueRefresh) {
  queueRefresh.addEventListener("click", () => loadQueueTasks());
}

if (queueCreateForm) {
  queueCreateForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (!queuePacketInput || !queueCreateStatus) return;
    let packet;
    try {
      packet = JSON.parse(queuePacketInput.value);
    } catch (error) {
      queueCreateStatus.textContent = `Invalid JSON: ${error.message}`;
      return;
    }
    queueCreateStatus.textContent = "Creating task...";
    try {
      const res = await fetch("/task-queue/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(packet)
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || data.message || "Could not create task");
      selectedQueueTaskId = data.task_id || data.task?.task_id || null;
      queueCreateStatus.textContent = selectedQueueTaskId ? `Created ${selectedQueueTaskId}` : "Task created";
      await loadQueueTasks();
    } catch (error) {
      queueCreateStatus.textContent = error.message;
    }
  });
}

copyStatus.addEventListener("click", async () => {
  let command = "";
  if (selectedTask?.task_id) {
    command = `/assistant/task/${selectedTask.task_id}`;
  } else if (selectedAgentId) {
    command = `/task Ask ${selectedAgentId} to `;
  }
  if (!command) return;
  input.value = command;
  input.focus();
  input.setSelectionRange(command.length, command.length);
  try {
    await navigator.clipboard.writeText(command);
    copyStatus.textContent = "Copied";
    setTimeout(() => copyStatus.textContent = selectedAgentId ? "Brief this crew member" : "Copy task command", 1200);
  } catch (_error) {
    copyStatus.textContent = "Ready in composer";
  }
});

input.addEventListener("keydown", event => {
  if (event.key === "Enter" && event.ctrlKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.addEventListener("keydown", event => {
  if (event.target === input || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.key === "1") setView("command");
  if (event.key === "2") setView("team");
  if (event.key === "3") setView("board");
  if (event.key === "4") setView("queue");
  if (event.key === "5") setView("log");
  if (event.key === "/") {
    event.preventDefault();
    input.focus();
  }
});

const exitBtn = document.getElementById("exitButton");
if (exitBtn) {
  exitBtn.addEventListener("click", async () => {
    exitBtn.disabled = true;
    exitBtn.querySelector("strong").textContent = "Closing";
    document.body.innerHTML = `
        <main style="min-height:100vh;display:grid;place-items:center;background:#070b12;color:#e8f1ff;font-family:Inter,Segoe UI,Arial,sans-serif;text-align:center;padding:24px">
          <section>
            <h1 style="font-size:28px;margin:0 0 10px">Dev Deck closed</h1>
            <p style="margin:0;color:#9fb2c8">The bridge has stopped. This window can be closed.</p>
          </section>
        </main>
      `;
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/shutdown", new Blob(["{}"], { type: "application/json" }));
      } else {
        fetch("/shutdown", { method: "POST", keepalive: true }).catch(() => {});
      }
      setTimeout(() => {
        window.open("", "_self");
        window.close();
      }, 250);
    } catch (e) {
      // The bridge may terminate before the browser resolves the request.
      // The closed screen above is the expected fallback.
      setTimeout(() => {
        window.open("", "_self");
        window.close();
      }, 250);
    }
  });
}

addMessage({ id: "welcome", kind: "agent", text: "Dev Deck is live. The widget, backend, agents, Kanban board, and activity log are on the same shared channel." });
renderDetails(null);
resizeStars();
drawStars();
refresh();
setInterval(refresh, 1500);
window.addEventListener("resize", resizeStars);
input.focus();
