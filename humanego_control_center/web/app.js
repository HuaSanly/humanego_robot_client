(() => {
  "use strict";

  const query = new URLSearchParams(window.location.search);
  const incomingToken = query.get("token");
  if (incomingToken) {
    localStorage.setItem("teleop-token", incomingToken);
  }
  const token = incomingToken || localStorage.getItem("teleop-token") || "";
  const DOM_LOG_LIMIT = 650;

  const model = {
    tasks: new Map(),
    instanceId: null,
    lastLogSeq: new Map(),
    pendingLogs: new Map(),
    logFlushTimer: null,
    syncPromise: null,
    autoScroll: true,
    filter: "",
    eventSource: null,
  };

  const el = {
    groups: document.querySelector("#taskGroups"),
    connection: document.querySelector("#connectionState"),
    sequenceTitle: document.querySelector("#sequenceTitle"),
    sequenceMessage: document.querySelector("#sequenceMessage"),
    sequenceGlyph: document.querySelector("#sequenceGlyph"),
    running: document.querySelector("#runningCount"),
    ready: document.querySelector("#readyCount"),
    errors: document.querySelector("#errorCount"),
    operator: document.querySelector("#operatorLabel"),
    filter: document.querySelector("#logFilter"),
    scrollToggle: document.querySelector("#toggleScroll"),
    toastStack: document.querySelector("#toastStack"),
    dialog: document.querySelector("#confirmDialog"),
  };

  const STATUS_LABELS = {
    stopped: "STOPPED",
    cleaning: "CLEANING",
    starting: "STARTING",
    running: "RUNNING",
    stopping: "STOPPING",
    finished: "COMPLETE",
    error: "FAULT",
  };

  function toast(message, kind = "info") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    el.toastStack.append(node);
    window.setTimeout(() => node.remove(), 3600);
  }

  async function api(path, method = "GET", body = undefined) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(path, {
        method,
        headers: body
          ? { "X-Teleop-Token": token, "Content-Type": "application/json" }
          : { "X-Teleop-Token": token },
        cache: "no-store",
        signal: controller.signal,
        body,
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.error || `HTTP ${response.status}`);
      }
      return response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new Error("控制服务 5 秒内未响应，请等待自动重连");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function setConnection(mode, label) {
    el.connection.className = `connection-state ${mode}`;
    el.connection.lastElementChild.textContent = label;
  }

  function createTaskCard(task, index) {
    const card = document.createElement("article");
    card.className = "task-card";
    card.dataset.taskId = task.id;
    card.dataset.accent = task.accent;
    card.innerHTML = `
      <div class="card-head">
        <div class="order-mark">${String(index + 1).padStart(2, "0")}</div>
        <div class="task-title">
          <h3></h3>
          <p></p>
        </div>
        <div class="card-actions">
          <button class="icon-button" data-task-action="restart" title="清理残留并重启" aria-label="重启"></button>
          <button class="icon-button stop" data-task-action="stop" title="停止并清理残留" aria-label="停止">■</button>
          <button class="icon-button" data-task-action="clear" title="清空此处日志" aria-label="清空日志">⌫</button>
        </div>
      </div>
      <div class="terminal-meta">
        <span class="status-pill"></span>
        <span class="runtime-label">UPTIME --</span>
        <span class="pid-label">PID --</span>
      </div>
      <div class="terminal" role="log" aria-live="off"><div class="empty-log">WAITING FOR OUTPUT_</div></div>
    `;
    const title = card.querySelector("h3");
    title.textContent = task.title;
    if (task.privileged) {
      const root = document.createElement("span");
      root.className = "root-tag";
      root.textContent = "ROOT";
      title.append(root);
    }
    card.querySelector(".task-title p").textContent = task.subtitle;
    card.querySelector('[data-task-action="restart"]').textContent = "↻";
    if (task.control_modes?.length) {
      const controls = document.createElement("div");
      controls.className = "client-controls";
      const mode = document.createElement("select");
      mode.className = "mode-select";
      mode.dataset.controlMode = "true";
      mode.setAttribute("aria-label", "客户端控制模式");
      task.control_modes.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value === "record_only" ? "仅记录，不发布命令" : "真机执行，发布命令";
        if (value === (task.control_mode || "record_only")) option.selected = true;
        mode.append(option);
      });
      const start = document.createElement("button");
      start.className = "button primary client-start";
      start.dataset.taskAction = "start";
      start.type = "button";
      start.textContent = "启动客户端";
      controls.append(mode, start);
      card.querySelector(".card-head").after(controls);
    }
    return card;
  }

  function renderStructure(tasks) {
    el.groups.replaceChildren();
    const groups = new Map();
    tasks.forEach((task) => {
      if (!groups.has(task.group)) groups.set(task.group, []);
      groups.get(task.group).push(task);
    });
    let globalIndex = 0;
    groups.forEach((items, groupName) => {
      const section = document.createElement("section");
      section.className = "task-group";
      const heading = document.createElement("div");
      heading.className = "group-heading";
      const headingTitle = document.createElement("h2");
      headingTitle.textContent = groupName;
      const count = document.createElement("span");
      count.textContent = `${String(items.length).padStart(2, "0")} MODULES`;
      heading.append(headingTitle, count);
      const grid = document.createElement("div");
      grid.className = "task-grid";
      items.forEach((task) => {
        grid.append(createTaskCard(task, globalIndex));
        globalIndex += 1;
      });
      section.append(heading, grid);
      el.groups.append(section);
    });
  }

  function taskCard(taskId) {
    return el.groups.querySelector(`[data-task-id="${CSS.escape(taskId)}"]`);
  }

  function formatDuration(startedAt, status) {
    if (!startedAt || !["running", "starting", "cleaning", "stopping"].includes(status)) return "UPTIME --";
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    return `UPTIME ${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  }

  function updateTask(task) {
    model.tasks.set(task.id, { ...model.tasks.get(task.id), ...task });
    const merged = model.tasks.get(task.id);
    const card = taskCard(task.id);
    if (!card) return;
    const pill = card.querySelector(".status-pill");
    pill.dataset.status = merged.status;
    pill.textContent = STATUS_LABELS[merged.status] || merged.status.toUpperCase();
    card.querySelector(".pid-label").textContent = merged.pid ? `PID ${merged.pid}` : merged.exit_code != null ? `EXIT ${merged.exit_code}` : "PID --";
    card.querySelector(".runtime-label").textContent = formatDuration(merged.started_at, merged.status);
    const active = ["cleaning", "starting", "running", "stopping"].includes(merged.status);
    const start = card.querySelector('[data-task-action="start"]');
    const stop = card.querySelector('[data-task-action="stop"]');
    const mode = card.querySelector("[data-control-mode]");
    if (start) start.disabled = active;
    if (stop) stop.disabled = !active;
    if (mode) {
      mode.disabled = active;
      if (!active) mode.value = merged.status === "stopped" ? "record_only" : merged.control_mode || "record_only";
    }
    updateMetrics();
  }

  function createLogLine(item) {
    const line = document.createElement("div");
    line.className = `log-line ${item.level || "output"}`;
    line.dataset.text = item.text.toLocaleLowerCase();
    if (model.filter && !line.dataset.text.includes(model.filter)) line.classList.add("filtered");
    const timestamp = document.createElement("span");
    timestamp.className = "time";
    timestamp.textContent = item.time;
    const message = document.createElement("span");
    message.className = "message";
    message.textContent = item.text;
    line.append(timestamp, message);
    return line;
  }

  function appendLogs(taskId, items) {
    const card = taskCard(taskId);
    if (!card) return;
    const terminal = card.querySelector(".terminal");
    let lastSeq = model.lastLogSeq.get(taskId) || 0;
    const fresh = items.filter((item) => !item.seq || item.seq > lastSeq);
    if (!fresh.length) return;
    terminal.querySelector(".empty-log")?.remove();
    const fragment = document.createDocumentFragment();
    fresh.forEach((item) => {
      fragment.append(createLogLine(item));
      if (item.seq) lastSeq = Math.max(lastSeq, item.seq);
    });
    model.lastLogSeq.set(taskId, lastSeq);
    terminal.append(fragment);
    const overflow = terminal.childElementCount - DOM_LOG_LIMIT;
    for (let index = 0; index < overflow; index += 1) terminal.firstElementChild?.remove();
    if (model.autoScroll) terminal.scrollTop = terminal.scrollHeight;
  }

  function queueLogs(taskId, items) {
    if (!items?.length) return;
    const pending = model.pendingLogs.get(taskId) || [];
    pending.push(...items);
    if (pending.length > 2000) pending.splice(0, pending.length - 2000);
    model.pendingLogs.set(taskId, pending);
    if (model.logFlushTimer) return;
    model.logFlushTimer = window.setTimeout(() => {
      const batches = model.pendingLogs;
      model.pendingLogs = new Map();
      model.logFlushTimer = null;
      batches.forEach((batch, id) => appendLogs(id, batch));
    }, 100);
  }

  function clearTaskLogs(taskId) {
    model.pendingLogs.delete(taskId);
    const terminal = taskCard(taskId)?.querySelector(".terminal");
    if (terminal) terminal.innerHTML = '<div class="empty-log">WAITING FOR OUTPUT_</div>';
  }

  function updateMetrics() {
    const tasks = [...model.tasks.values()];
    el.running.textContent = tasks.filter((task) => task.status === "running").length;
    el.ready.textContent = tasks.filter((task) => task.status === "finished").length;
    el.errors.textContent = tasks.filter((task) => task.status === "error").length;
  }

  function updateSequence(sequence) {
    const labels = {
      idle: "系统待命",
      running: "按依赖顺序启动中",
      stopping: "正在安全停止",
      done: "启动序列完成",
      failed: "启动序列暂停",
      busy: "系统操作繁忙",
    };
    el.sequenceTitle.textContent = labels[sequence.status] || "控制台已连接";
    el.sequenceMessage.textContent = sequence.message || "—";
    if (sequence.current && model.tasks.has(sequence.current)) {
      const taskArray = [...model.tasks.values()];
      const index = taskArray.findIndex((task) => task.id === sequence.current);
      el.sequenceGlyph.textContent = String(index + 1).padStart(2, "0");
    } else {
      el.sequenceGlyph.textContent = sequence.status === "done" ? "OK" : "01";
    }
  }

  function applyState(state) {
    const instanceChanged = Boolean(model.instanceId && model.instanceId !== state.instance_id);
    const needsStructure = instanceChanged || !model.instanceId || !el.groups.childElementCount;
    if (instanceChanged) {
      model.tasks.clear();
      model.lastLogSeq.clear();
      model.pendingLogs.clear();
    }
    model.instanceId = state.instance_id;
    state.tasks.sort((a, b) => a.order - b.order);
    if (needsStructure) renderStructure(state.tasks);
    state.tasks.forEach((task) => {
      updateTask(task);
      appendLogs(task.id, task.logs || []);
    });
    updateSequence(state.sequence);
    el.operator.textContent = `OPERATOR · ${state.operator.toUpperCase()}`;
  }

  async function syncState() {
    if (!token) throw new Error("链接中缺少控制令牌，请使用启动终端打印的完整地址");
    if (model.syncPromise) return model.syncPromise;
    model.syncPromise = api("/api/state")
      .then((state) => applyState(state))
      .finally(() => { model.syncPromise = null; });
    return model.syncPromise;
  }

  function connectEvents() {
    model.eventSource?.close();
    const source = new EventSource(`/api/events?token=${encodeURIComponent(token)}`);
    model.eventSource = source;
    source.onopen = () => {
      setConnection("online", "CONTROL LINK ONLINE");
      syncState().catch((error) => toast(`状态同步失败：${error.message}`, "error"));
    };
    source.onerror = () => setConnection("offline", "LINK INTERRUPTED · RETRYING");
    source.addEventListener("update", (event) => {
      const payload = JSON.parse(event.data);
      if (model.instanceId && payload.instance_id !== model.instanceId) {
        syncState().catch(() => {});
        return;
      }
      if (payload.type === "log") queueLogs(payload.task_id, [payload.item]);
      if (payload.type === "logs") queueLogs(payload.task_id, payload.items);
      if (payload.type === "task") updateTask(payload.task);
      if (payload.type === "sequence") updateSequence(payload.sequence);
      if (payload.type === "logs_cleared") clearTaskLogs(payload.task_id);
    });
  }

  async function taskAction(taskId, action, button, controlMode = null) {
    button.disabled = true;
    try {
      const body = controlMode ? JSON.stringify({ control_mode: controlMode }) : undefined;
      await api(`/api/task/${encodeURIComponent(taskId)}/${action}`, "POST", body);
      const verbs = { start: "启动指令已发送", restart: "重启指令已发送", stop: "停止指令已发送", clear: "日志已清空" };
      toast(verbs[action] || "操作已发送");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      window.setTimeout(() => { button.disabled = false; }, 500);
    }
  }

  async function globalAction(action, button) {
    if (action === "stop") {
      el.dialog.showModal();
      const choice = await new Promise((resolve) => {
        el.dialog.addEventListener("close", () => resolve(el.dialog.returnValue), { once: true });
      });
      if (choice !== "confirm") return;
    }
    button.disabled = true;
    try {
      await api(`/api/all/${action}`, "POST");
      const messages = {
        start: "一键启动序列已开始",
        restart: "全部任务将逆序停止后重新启动",
        stop: "安全停止序列已开始",
        cleanup: "正在扫描并清理已知残留进程",
      };
      toast(messages[action]);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      window.setTimeout(() => { button.disabled = false; }, 800);
    }
  }

  document.addEventListener("click", (event) => {
    const taskButton = event.target.closest("[data-task-action]");
    if (taskButton) {
      const card = taskButton.closest("[data-task-id]");
      const selector = card.querySelector("[data-control-mode]");
      taskAction(card.dataset.taskId, taskButton.dataset.taskAction, taskButton, selector?.value || null);
      return;
    }
    const globalButton = event.target.closest("[data-global-action]");
    if (globalButton) globalAction(globalButton.dataset.globalAction, globalButton);
  });

  el.scrollToggle.addEventListener("click", () => {
    model.autoScroll = !model.autoScroll;
    el.scrollToggle.textContent = `自动滚动 · ${model.autoScroll ? "开" : "关"}`;
    el.scrollToggle.setAttribute("aria-pressed", String(model.autoScroll));
  });

  el.filter.addEventListener("input", () => {
    model.filter = el.filter.value.trim().toLocaleLowerCase();
    document.querySelectorAll(".log-line").forEach((line) => {
      line.classList.toggle("filtered", Boolean(model.filter) && !line.dataset.text.includes(model.filter));
    });
  });

  function updateClock() {
    const now = new Date();
    document.querySelector("#clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
    document.querySelector("#date").textContent = now.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).replaceAll("/", ".");
    model.tasks.forEach((task) => {
      const runtime = taskCard(task.id)?.querySelector(".runtime-label");
      if (runtime) runtime.textContent = formatDuration(task.started_at, task.status);
    });
  }
  updateClock();
  window.setInterval(updateClock, 1000);

  let bootstrapAttempts = 0;
  function bootstrap() {
    syncState()
      .then(() => connectEvents())
      .catch((error) => {
        bootstrapAttempts += 1;
        setConnection("offline", "WAITING FOR CONTROL SERVICE");
        el.sequenceTitle.textContent = "等待控制服务接管";
        el.sequenceMessage.textContent = `${error.message} · 正在自动重试`;
        if (bootstrapAttempts === 1) toast(error.message, "error");
        window.setTimeout(bootstrap, 1500);
      });
  }
  bootstrap();
})();
