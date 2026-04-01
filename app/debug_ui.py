from __future__ import annotations


def render_debug_page() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Agno 调试台</title>
    <style>
      :root {
        --bg: #f3efe8;
        --panel: rgba(255, 251, 246, 0.94);
        --ink: #1f2937;
        --muted: #627082;
        --accent: #0f766e;
        --accent-soft: #dff4f1;
        --warn: #b45309;
        --error: #b91c1c;
        --border: rgba(15, 118, 110, 0.16);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "Avenir Next", "Segoe UI", sans-serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(15,118,110,0.18), transparent 28%),
          radial-gradient(circle at 85% 10%, rgba(190,24,93,0.12), transparent 25%),
          linear-gradient(135deg, #fbf8f1 0%, #f4efe5 50%, #efe8db 100%);
      }
      .shell { max-width: 1380px; margin: 0 auto; padding: 28px 20px 40px; }
      .hero, .panel {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: 0 18px 40px rgba(31, 41, 55, 0.07);
        backdrop-filter: blur(10px);
      }
      .hero { padding: 26px; }
      .hero h1 { margin: 0; font-size: clamp(30px, 4.6vw, 52px); letter-spacing: -0.03em; }
      .hero p { margin: 12px 0 0; color: var(--muted); line-height: 1.7; max-width: 880px; }
      .badges { margin-top: 16px; display: flex; flex-wrap: wrap; gap: 10px; }
      .badge {
        padding: 10px 14px;
        border-radius: 999px;
        background: var(--accent-soft);
        color: var(--accent);
        font-weight: 700;
        font-size: 14px;
      }
      .grid { margin-top: 18px; display: grid; grid-template-columns: 360px 1fr; gap: 18px; align-items: start; }
      .panel { padding: 20px; }
      .left { position: sticky; top: 18px; }
      .section-title { margin: 0 0 12px; font-size: 18px; }
      .tabs { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
      .tab {
        padding: 10px 14px;
        border-radius: 999px;
        border: 1px solid rgba(95,108,128,0.18);
        background: white;
        color: var(--ink);
        cursor: pointer;
        font-weight: 700;
      }
      .tab.active { background: linear-gradient(135deg, #0f766e, #115e59); color: white; border-color: transparent; }
      .tab-panel { display: none; }
      .tab-panel.active { display: block; }
      label { display: block; font-size: 13px; font-weight: 700; color: var(--muted); margin-bottom: 8px; }
      input, select, textarea, button {
        width: 100%;
        border-radius: 14px;
        border: 1px solid rgba(95,108,128,0.18);
        padding: 11px 13px;
        font: inherit;
        background: white;
      }
      textarea { min-height: 150px; resize: vertical; }
      button {
        border: none;
        cursor: pointer;
        font-weight: 700;
        background: linear-gradient(135deg, #0f766e, #115e59);
        color: white;
      }
      button.secondary { background: linear-gradient(135deg, #475569, #334155); }
      button.warn { background: linear-gradient(135deg, #b45309, #92400e); }
      button:disabled { opacity: 0.55; cursor: wait; }
      .stack { display: grid; gap: 12px; }
      .row2 { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .row3 { display: grid; gap: 12px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .actions { display: flex; flex-wrap: wrap; gap: 10px; }
      .actions button { flex: 1; min-width: 120px; }
      .result {
        margin-top: 14px;
        border-radius: 18px;
        background: rgba(248, 250, 252, 0.95);
        border: 1px solid rgba(95,108,128,0.14);
        padding: 14px;
      }
      pre {
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: "SF Mono", "JetBrains Mono", monospace;
        font-size: 12.5px;
        line-height: 1.65;
      }
      .muted { color: var(--muted); }
      .status-line {
        padding: 10px 12px;
        border-radius: 16px;
        background: rgba(255,255,255,0.78);
        border: 1px solid rgba(95,108,128,0.14);
        color: var(--ink);
      }
      .ok { color: var(--accent); }
      .warn-text { color: var(--warn); }
      .error-text { color: var(--error); }
      .agent-card, .timeline-item {
        border: 1px solid rgba(95,108,128,0.14);
        border-radius: 18px;
        padding: 14px;
        background: rgba(255,255,255,0.82);
      }
      .agent-grid, .timeline { display: grid; gap: 12px; }
      .mini { font-size: 12px; color: var(--muted); }
      .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
      .chip {
        padding: 5px 10px;
        border-radius: 999px;
        background: rgba(15,118,110,0.10);
        color: var(--accent);
        font-size: 12px;
        font-weight: 700;
      }
      .summary-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
      @media (max-width: 980px) {
        .grid { grid-template-columns: 1fr; }
        .left { position: static; }
      }
      @media (max-width: 720px) {
        .shell { padding: 16px 12px 24px; }
        .hero, .panel { padding: 16px; border-radius: 18px; }
        .row2, .row3 { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <h1>Agno 本地调试台</h1>
        <p>这个页面会把用户、项目、workspace、external agents、trace 和 agent 配置串起来，方便你验证内部智能体、External Agent Broker、Workspace MCP、A2A 和审计链路是否真的生效。</p>
        <div class="badges">
          <div class="badge" id="runtimeBadge">加载运行状态中...</div>
          <div class="badge" id="authBadge">尚未获取 token</div>
          <div class="badge" id="traceBadge">最近 trace: 无</div>
        </div>
      </section>

      <div class="grid">
        <aside class="panel left">
          <h2 class="section-title">会话控制</h2>
          <div class="stack">
            <div>
              <label for="userSelect">演示用户</label>
              <select id="userSelect"></select>
            </div>
            <div>
              <label for="projectSelect">项目</label>
              <select id="projectSelect"></select>
            </div>
            <div class="actions">
              <button id="tokenBtn" type="button">获取本地 Token</button>
              <button id="codexBtn" type="button" class="secondary">使用 Codex 登录态</button>
            </div>
            <div class="status-line" id="userStatus">等待初始化用户列表...</div>
            <div class="status-line" id="actionStatus">等待操作...</div>
            <div class="status-line mini">
              切换用户后会自动清空旧 token，并重新刷新项目下拉与后续请求上下文。
            </div>
            <div class="result">
              <pre id="sessionOutput">等待请求...</pre>
            </div>
          </div>
        </aside>

        <main class="panel">
          <div class="tabs">
            <button class="tab active" data-tab="chat">Chat</button>
            <button class="tab" data-tab="knowledge">Knowledge</button>
            <button class="tab" data-tab="workspace">Workspace</button>
            <button class="tab" data-tab="execution">Execution</button>
            <button class="tab" data-tab="external">External</button>
            <button class="tab" data-tab="agents">Agents</button>
            <button class="tab" data-tab="trace">Trace</button>
          </div>

          <section class="tab-panel active" data-panel="chat">
            <div class="stack">
              <div>
                <label for="chatMessage">提问</label>
                <textarea id="chatMessage">请结合我的项目知识库和个人空间文件，给我一份 PoC 的测试建议，重点关注多用户隔离、MCP 调用、Agent 配置和审计链路。</textarea>
              </div>
              <div class="actions">
                <button id="chatBtn" type="button">发送到主智能体</button>
                <button id="mockBtn" type="button" class="warn">强制走 Mock 模式</button>
                <button id="chatTraceBtn" type="button" class="secondary">查看最近 Trace</button>
              </div>
              <div class="result"><pre id="chatOutput">等待请求...</pre></div>
            </div>
          </section>

          <section class="tab-panel" data-panel="knowledge">
            <div class="stack">
              <div>
                <label for="knowledgeQuery">Knowledge 查询</label>
                <textarea id="knowledgeQuery">多用户隔离、测试基线、运维手册、个人知识</textarea>
              </div>
              <div class="actions">
                <button id="knowledgeBtn" type="button">测试 Knowledge Agent</button>
              </div>
              <div class="result"><pre id="knowledgeOutput">等待请求...</pre></div>
            </div>
          </section>

          <section class="tab-panel" data-panel="workspace">
            <div class="stack">
              <div class="summary-grid">
                <div class="status-line mini">Workspace direct view 只用于人工检查，不代表 MCP。</div>
                <div class="status-line mini">Workspace MCP probe 会真正起 MCP server 并留下 `mcp_tool_call` 审计。</div>
              </div>
              <div class="row2">
                <div>
                  <label for="workspacePath">文件路径</label>
                  <input id="workspacePath" value="notes/debug-note.md" />
                </div>
                <div>
                  <label for="workspacePrefix">目录前缀</label>
                  <input id="workspacePrefix" placeholder="例如 notes" />
                </div>
              </div>
              <div>
                <label for="workspaceContent">文件内容</label>
                <textarea id="workspaceContent">这是通过 debug 页面写入的验证内容。</textarea>
              </div>
              <div class="actions">
                <button id="workspaceDirectBtn" type="button">Direct 列文件</button>
                <button id="workspaceReadBtn" type="button" class="secondary">Direct 读文件</button>
                <button id="workspaceWriteBtn" type="button" class="secondary">Direct 写文件</button>
              </div>
              <div class="actions">
                <button id="workspaceMcpListBtn" type="button">MCP 列文件</button>
                <button id="workspaceMcpReadBtn" type="button" class="secondary">MCP 读文件</button>
                <button id="workspaceMcpWriteBtn" type="button" class="secondary">MCP 写文件</button>
              </div>
              <div class="result"><pre id="workspaceOutput">等待请求...</pre></div>
            </div>
          </section>

          <section class="tab-panel" data-panel="execution">
            <div class="stack">
              <div class="summary-grid">
                <div class="status-line mini">Execution tab 走的是独立 sandbox，不是普通 chat 环境。</div>
                <div class="status-line mini">默认禁网，默认不写回 workspace，执行目录来自当前用户 workspace 的 staged 副本。</div>
              </div>
              <div class="row3">
                <div>
                  <label for="executionLanguage">language</label>
                  <select id="executionLanguage">
                    <option value="python" selected>python</option>
                  </select>
                </div>
                <div>
                  <label for="executionTimeout">timeout_seconds</label>
                  <input id="executionTimeout" type="number" min="1" value="20" />
                </div>
                <div>
                  <label for="executionWriteback">writeback</label>
                  <select id="executionWriteback">
                    <option value="false" selected>false</option>
                    <option value="true">true</option>
                  </select>
                </div>
              </div>
              <div class="row2">
                <div>
                  <label for="executionCommand">command</label>
                  <input id="executionCommand" value="python inline_task.py" />
                </div>
                <div>
                  <label for="executionEntrypoint">entrypoint</label>
                  <input id="executionEntrypoint" placeholder="例如 notes/script.py，可留空" />
                </div>
              </div>
              <div>
                <label for="executionScript">script content</label>
                <textarea id="executionScript">print("hello from sandbox")</textarea>
              </div>
              <div class="row2">
                <div>
                  <label for="executionJobId">job_id</label>
                  <input id="executionJobId" placeholder="执行后会自动填充最近一次 job_id" />
                </div>
                <div class="status-line mini">
                  当前显示的是用户可控的 sandbox 执行结果：状态、stdout/stderr、产物和 trace。
                </div>
              </div>
              <div class="actions">
                <button id="executionRunBtn" type="button">执行到 Sandbox</button>
                <button id="executionStatusBtn" type="button" class="secondary">查看 Job 状态</button>
                <button id="executionLogsBtn" type="button" class="secondary">查看完整日志</button>
                <button id="executionArtifactsBtn" type="button" class="secondary">查看产物列表</button>
              </div>
              <div class="result"><pre id="executionOutput">等待请求...</pre></div>
              <div class="result"><pre id="executionLogsOutput">等待日志...</pre></div>
              <div class="result"><pre id="executionArtifactsOutput">等待产物...</pre></div>
            </div>
          </section>

          <section class="tab-panel" data-panel="external">
            <div class="stack">
              <div class="row2">
                <div>
                  <label for="externalCategory">category</label>
                  <input id="externalCategory" placeholder="例如 compliance / security / analytics" />
                </div>
                <div>
                  <label for="externalAgentId">agent_id</label>
                  <input id="externalAgentId" placeholder="留空则由 Broker 自动选择" />
                </div>
              </div>
              <div class="actions">
                <button id="externalListBtn" type="button">查看 external agents</button>
                <button id="externalRefreshBtn" type="button" class="secondary">刷新目录</button>
                <button id="externalInvokeBtn" type="button" class="secondary">直接调用 external agent</button>
              </div>
              <div class="result"><pre id="externalOutput">等待请求...</pre></div>
            </div>
          </section>

          <section class="tab-panel" data-panel="agents">
            <div class="stack">
              <div class="actions">
                <button id="agentsRefreshBtn" type="button">刷新 Agent 配置</button>
              </div>
              <div id="agentsOutput" class="agent-grid"></div>
            </div>
          </section>

          <section class="tab-panel" data-panel="trace">
            <div class="stack">
              <div class="row2">
                <div>
                  <label for="traceIdInput">trace_id</label>
                  <input id="traceIdInput" placeholder="输入 trace_id 或使用最近一次 trace" />
                </div>
                <div class="actions">
                  <button id="traceBtn" type="button">查看 Trace Summary</button>
                  <button id="traceLatestBtn" type="button" class="secondary">载入最近 Trace</button>
                </div>
              </div>
              <div class="result"><pre id="traceOutput">等待请求...</pre></div>
              <div id="traceTimeline" class="timeline"></div>
            </div>
          </section>
        </main>
      </div>
    </div>

    <script>
      const state = {
        token: "",
        tokenUserId: "",
        tokenKind: "",
        users: [],
        currentProjects: [],
        lastTraceId: "",
        lastExecutionJobId: "",
        aliases: [],
      };

      const tabs = [...document.querySelectorAll(".tab")];
      const panels = [...document.querySelectorAll(".tab-panel")];

      function $(id) { return document.getElementById(id); }
      function pretty(value) { return JSON.stringify(value, null, 2); }
      function setBadge(id, text) { $(id).textContent = text; }
      function setActionStatus(text, kind = "ok") {
        const el = $("actionStatus");
        el.textContent = text;
        el.className = "status-line " + (kind === "error" ? "error-text" : kind === "warn" ? "warn-text" : "ok");
      }
      function setOutput(id, value) { $(id).textContent = typeof value === "string" ? value : pretty(value); }
      function disable(ids, disabled) { ids.forEach((id) => { $(id).disabled = disabled; }); }

      function activateTab(name) {
        tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
        panels.forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
      }

      tabs.forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));

      function selectedUser() { return $("userSelect").value; }
      function selectedProject() { return $("projectSelect").value; }

      function clearToken(reason) {
        state.token = "";
        state.tokenUserId = "";
        state.tokenKind = "";
        setBadge("authBadge", "尚未获取 token");
        setActionStatus(reason || "已清空旧 token", "warn");
      }

      function updateTrace(traceId) {
        state.lastTraceId = traceId || "";
        $("traceIdInput").value = state.lastTraceId;
        setBadge("traceBadge", state.lastTraceId ? `最近 trace: ${state.lastTraceId}` : "最近 trace: 无");
      }

      function updateExecutionJob(jobId) {
        state.lastExecutionJobId = jobId || "";
        if (jobId) $("executionJobId").value = jobId;
      }

      function renderProjectOptions(user) {
        const select = $("projectSelect");
        select.innerHTML = "";
        const projectIds = (user && user.project_ids) || [];
        state.currentProjects = projectIds;
        projectIds.forEach((projectId) => {
          const option = document.createElement("option");
          option.value = projectId;
          option.textContent = projectId;
          select.appendChild(option);
        });
        select.value = user?.default_project_id || projectIds[0] || "";
      }

      function renderUserOptions() {
        const select = $("userSelect");
        select.innerHTML = "";
        state.users.forEach((user) => {
          const option = document.createElement("option");
          option.value = user.user_id;
          option.textContent = `${user.user_id} / ${user.display_name} / ${user.role}`;
          select.appendChild(option);
        });
        const first = state.users[0];
        if (first) {
          select.value = first.user_id;
          renderProjectOptions(first);
          $("userStatus").textContent = `当前演示用户: ${first.user_id}，可访问项目: ${(first.project_ids || []).join(", ")}`;
        }
      }

      function upsertUser(user) {
        const existing = state.users.findIndex((item) => item.user_id === user.user_id);
        if (existing >= 0) {
          state.users[existing] = user;
        } else {
          state.users.push(user);
          state.users.sort((a, b) => a.user_id.localeCompare(b.user_id));
        }
        renderUserOptions();
        $("userSelect").value = user.user_id;
        renderProjectOptions(user);
        $("userStatus").textContent = `当前演示用户: ${user.user_id}，可访问项目: ${(user.project_ids || []).join(", ")}`;
      }

      async function loadUsers() {
        const res = await fetch("/gateway/dev-users");
        const data = await res.json();
        state.users = data.users || [];
        renderUserOptions();
      }

      $("userSelect").addEventListener("change", () => {
        const user = state.users.find((item) => item.user_id === selectedUser());
        renderProjectOptions(user);
        $("userStatus").textContent = `当前演示用户: ${user?.user_id || "unknown"}，可访问项目: ${(user?.project_ids || []).join(", ")}`;
        clearToken("检测到用户切换，已清空旧 token。后续请求会自动为新用户重新获取 token。");
        refreshAgents().catch(() => {});
      });

      $("projectSelect").addEventListener("change", () => {
        setActionStatus(`已切换到项目 ${selectedProject()}`);
        refreshAgents().catch(() => {});
      });

      async function fetchRuntimeStatus() {
        const res = await fetch("/gateway/runtime-status");
        const data = await res.json();
        state.aliases = (data.aliases || []).map((item) => item.alias);
        const healthy = (data.healthy_aliases || []).join(", ") || "无";
        const exec = data.execution || {};
        setBadge(
          "runtimeBadge",
          data.live
            ? `live | healthy=${healthy} | external=${data.external_agents?.count ?? 0} | exec=${exec.configured_mode || "n/a"} | prefetch=${data.external_prefetch?.mode}`
            : `mock | ${data.reason} | exec=${exec.configured_mode || "n/a"}`
        );
      }

      async function fetchDemoToken() {
        const userId = selectedUser();
        setActionStatus(`正在为 ${userId} 获取本地 token...`);
        const res = await fetch(`/gateway/dev-token/${userId}`);
        const data = await res.json();
        state.token = data.token;
        state.tokenUserId = data.user;
        state.tokenKind = "demo";
        const user = state.users.find((item) => item.user_id === data.user);
        if (user) renderProjectOptions(user);
        setBadge("authBadge", `当前 token: ${data.user} / demo`);
        setOutput("sessionOutput", data);
        setActionStatus(`已获取 ${data.user} 的本地 token`);
        return data.token;
      }

      async function fetchCodexToken() {
        setActionStatus("正在桥接 Codex 登录态...");
        const res = await fetch("/gateway/codex-login", { method: "POST" });
        const data = await res.json();
        state.token = data.token;
        state.tokenUserId = data.user.user_id;
        state.tokenKind = "codex";
        upsertUser({
          tenant_id: data.user.tenant_id,
          user_id: data.user.user_id,
          display_name: data.user.display_name,
          role: data.user.role,
          project_ids: data.user.project_ids,
          default_project_id: data.user.default_project_id,
        });
        setBadge("authBadge", `当前 token: ${data.user.user_id} / codex`);
        setOutput("sessionOutput", data);
        updateTrace(data.trace_id);
        setActionStatus(`已桥接 Codex 登录态 -> ${data.user.user_id}`);
        return data.token;
      }

      async function ensureToken() {
        if (state.token && state.tokenUserId === selectedUser() && state.tokenKind === "demo") {
          return state.token;
        }
        if (state.token && state.tokenKind === "codex" && state.tokenUserId === selectedUser()) {
          return state.token;
        }
        return fetchDemoToken();
      }

      async function authorizedFetch(url, options = {}) {
        const token = await ensureToken();
        const headers = new Headers(options.headers || {});
        headers.set("Authorization", `Bearer ${token}`);
        return fetch(url, { ...options, headers });
      }

      async function runAction({ ids, outputId, loadingText, fn, onDone }) {
        disable(ids, true);
        if (outputId) setOutput(outputId, loadingText || "请求中...");
        setActionStatus(loadingText || "请求中...");
        try {
          const data = await fn();
          if (outputId) setOutput(outputId, data);
          if (data && data.trace_id) updateTrace(data.trace_id);
          if (onDone) onDone(data);
          setActionStatus("请求成功");
          return data;
        } catch (error) {
          const detail = error?.message || String(error);
          if (outputId) setOutput(outputId, { error: detail });
          setActionStatus(detail, "error");
          throw error;
        } finally {
          disable(ids, false);
        }
      }

      async function parseJson(res) {
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
        return data;
      }

      async function chat(useMock = false) {
        return runAction({
          ids: ["chatBtn", "mockBtn", "chatTraceBtn"],
          outputId: "chatOutput",
          loadingText: "主智能体请求中...",
          fn: async () => parseJson(await authorizedFetch("/gateway/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              message: $("chatMessage").value,
              project_id: selectedProject(),
              use_mock: useMock,
            }),
          })),
          onDone: async (data) => {
            if (data.trace_id) {
              await loadTraceSummary(data.trace_id, false);
            }
          },
        });
      }

      async function loadKnowledge() {
        return runAction({
          ids: ["knowledgeBtn"],
          outputId: "knowledgeOutput",
          loadingText: "Knowledge 查询中...",
          fn: async () => parseJson(await authorizedFetch(
            `/gateway/knowledge?q=${encodeURIComponent($("knowledgeQuery").value)}&project_id=${encodeURIComponent(selectedProject())}`
          )),
        });
      }

      async function workspaceDirectList() {
        return runAction({
          ids: ["workspaceDirectBtn", "workspaceReadBtn", "workspaceWriteBtn", "workspaceMcpListBtn", "workspaceMcpReadBtn", "workspaceMcpWriteBtn"],
          outputId: "workspaceOutput",
          loadingText: "正在读取 direct workspace 视图...",
          fn: async () => parseJson(await authorizedFetch("/gateway/workspace")),
        });
      }

      async function workspaceDirectRead() {
        return runAction({
          ids: ["workspaceDirectBtn", "workspaceReadBtn", "workspaceWriteBtn", "workspaceMcpListBtn", "workspaceMcpReadBtn", "workspaceMcpWriteBtn"],
          outputId: "workspaceOutput",
          loadingText: "正在 direct 读取文件...",
          fn: async () => parseJson(await authorizedFetch(`/gateway/workspace/file?path=${encodeURIComponent($("workspacePath").value)}`)),
        });
      }

      async function workspaceDirectWrite() {
        return runAction({
          ids: ["workspaceDirectBtn", "workspaceReadBtn", "workspaceWriteBtn", "workspaceMcpListBtn", "workspaceMcpReadBtn", "workspaceMcpWriteBtn"],
          outputId: "workspaceOutput",
          loadingText: "正在 direct 写入文件...",
          fn: async () => parseJson(await authorizedFetch("/gateway/workspace/file", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              path: $("workspacePath").value,
              content: $("workspaceContent").value,
              overwrite: true,
            }),
          })),
        });
      }

      async function workspaceMcp(tool) {
        const body = { project_id: selectedProject(), path: $("workspacePath").value, prefix: $("workspacePrefix").value, content: $("workspaceContent").value, overwrite: true };
        const routes = {
          list: { url: "/gateway/debug/workspace-mcp/list", payload: { project_id: selectedProject(), prefix: $("workspacePrefix").value, limit: 50 } },
          read: { url: "/gateway/debug/workspace-mcp/read", payload: { project_id: selectedProject(), path: $("workspacePath").value, max_chars: 6000 } },
          write: { url: "/gateway/debug/workspace-mcp/write", payload: { project_id: selectedProject(), path: $("workspacePath").value, content: $("workspaceContent").value, overwrite: true } },
        };
        const target = routes[tool];
        return runAction({
          ids: ["workspaceDirectBtn", "workspaceReadBtn", "workspaceWriteBtn", "workspaceMcpListBtn", "workspaceMcpReadBtn", "workspaceMcpWriteBtn"],
          outputId: "workspaceOutput",
          loadingText: `正在通过 MCP 执行 workspace_${tool}...`,
          fn: async () => parseJson(await authorizedFetch(target.url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(target.payload),
          })),
          onDone: async (data) => { if (data.trace_id) await loadTraceSummary(data.trace_id, false); },
        });
      }

      async function listExternalAgents(forceRefresh = false) {
        return runAction({
          ids: ["externalListBtn", "externalRefreshBtn", "externalInvokeBtn"],
          outputId: "externalOutput",
          loadingText: "正在查询 external agents...",
          fn: async () => {
            const params = new URLSearchParams();
            if ($("externalCategory").value) params.set("category", $("externalCategory").value);
            if (selectedProject()) params.set("project_id", selectedProject());
            if (forceRefresh) params.set("force_refresh", "true");
            return parseJson(await authorizedFetch(`/gateway/external-agents?${params.toString()}`));
          },
        });
      }

      async function refreshExternalAgents() {
        return runAction({
          ids: ["externalListBtn", "externalRefreshBtn", "externalInvokeBtn"],
          outputId: "externalOutput",
          loadingText: "正在刷新 external catalog...",
          fn: async () => parseJson(await authorizedFetch("/gateway/external-agents/refresh", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: selectedProject() }),
          })),
        });
      }

      async function invokeExternalAgent() {
        return runAction({
          ids: ["externalListBtn", "externalRefreshBtn", "externalInvokeBtn"],
          outputId: "externalOutput",
          loadingText: "正在调用 external agent...",
          fn: async () => parseJson(await authorizedFetch("/gateway/external-agents/invoke", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              message: $("chatMessage").value,
              project_id: selectedProject(),
              agent_id: $("externalAgentId").value || null,
              category: $("externalCategory").value || null,
            }),
          })),
          onDone: async (data) => { if (data.trace_id) await loadTraceSummary(data.trace_id, false); },
        });
      }

      function currentExecutionJobId() {
        return $("executionJobId").value || state.lastExecutionJobId;
      }

      function buildExecutionPayload() {
        const script = $("executionScript").value.trim();
        const entrypoint = $("executionEntrypoint").value.trim();
        const command = $("executionCommand").value.trim();
        return {
          project_id: selectedProject(),
          language: $("executionLanguage").value,
          command: command || null,
          entrypoint: entrypoint || null,
          files: script ? [{ path: entrypoint || "inline_task.py", content: script }] : [],
          timeout_seconds: Number($("executionTimeout").value || 20),
          writeback: $("executionWriteback").value === "true",
        };
      }

      async function runExecution() {
        return runAction({
          ids: ["executionRunBtn", "executionStatusBtn", "executionLogsBtn", "executionArtifactsBtn"],
          outputId: "executionOutput",
          loadingText: "正在提交 sandbox 执行任务...",
          fn: async () => parseJson(await authorizedFetch("/gateway/exec/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(buildExecutionPayload()),
          })),
          onDone: async (data) => {
            updateExecutionJob(data.job?.job_id || "");
            setOutput("executionLogsOutput", {
              stdout: data.stdout,
              stderr: data.stderr,
              stdout_truncated: data.stdout_truncated,
              stderr_truncated: data.stderr_truncated,
            });
            setOutput("executionArtifactsOutput", data.artifacts || []);
            if (data.trace_id) await loadTraceSummary(data.trace_id, false);
          },
        });
      }

      async function loadExecutionStatus() {
        const jobId = currentExecutionJobId();
        if (!jobId) throw new Error("当前没有可用 execution job_id");
        return runAction({
          ids: ["executionRunBtn", "executionStatusBtn", "executionLogsBtn", "executionArtifactsBtn"],
          outputId: "executionOutput",
          loadingText: "正在加载 execution job 状态...",
          fn: async () => parseJson(await authorizedFetch(`/gateway/exec/${encodeURIComponent(jobId)}`)),
          onDone: (data) => updateExecutionJob(data.job?.job_id || jobId),
        });
      }

      async function loadExecutionLogs() {
        const jobId = currentExecutionJobId();
        if (!jobId) throw new Error("当前没有可用 execution job_id");
        return runAction({
          ids: ["executionRunBtn", "executionStatusBtn", "executionLogsBtn", "executionArtifactsBtn"],
          outputId: "executionLogsOutput",
          loadingText: "正在加载 execution 日志...",
          fn: async () => parseJson(await authorizedFetch(`/gateway/exec/${encodeURIComponent(jobId)}/logs`)),
        });
      }

      async function loadExecutionArtifacts() {
        const jobId = currentExecutionJobId();
        if (!jobId) throw new Error("当前没有可用 execution job_id");
        return runAction({
          ids: ["executionRunBtn", "executionStatusBtn", "executionLogsBtn", "executionArtifactsBtn"],
          outputId: "executionArtifactsOutput",
          loadingText: "正在加载 execution 产物...",
          fn: async () => parseJson(await authorizedFetch(`/gateway/exec/${encodeURIComponent(jobId)}/artifacts`)),
        });
      }

      function aliasOptions(selectedAlias) {
        const aliases = ["", ...state.aliases];
        return aliases.map((alias) => `<option value="${alias}" ${alias === (selectedAlias || "") ? "selected" : ""}>${alias || "默认路由"}</option>`).join("");
      }

      function renderAgentCards(items) {
        const container = $("agentsOutput");
        if (!items.length) {
          container.innerHTML = '<div class="agent-card">当前没有可展示的 agent 配置。</div>';
          return;
        }
        container.innerHTML = items.map((item) => `
          <div class="agent-card" data-agent-key="${item.agent_key}">
            <div class="row2">
              <div>
                <strong>${item.display_name}</strong>
                <div class="mini">${item.agent_key} | source=${item.source} | ${item.included_in_team ? "已加入 Team" : "未加入 Team"}</div>
              </div>
              <div class="mini">${item.description}</div>
            </div>
            <div class="chips">
              ${(item.skills_group || []).map((skill) => `<span class="chip">skill:${skill}</span>`).join("")}
              ${(item.tool_summary || []).map((tool) => `<span class="chip">tool:${tool}</span>`).join("")}
            </div>
            <div class="row3" style="margin-top:12px;">
              <div>
                <label>enabled</label>
                <select class="agent-enabled" ${item.is_editable ? "" : "disabled"}>
                  <option value="true" ${item.enabled ? "selected" : ""}>true</option>
                  <option value="false" ${!item.enabled ? "selected" : ""}>false</option>
                </select>
              </div>
              <div>
                <label>priority</label>
                <input class="agent-priority" type="number" value="${item.priority}" ${item.is_editable ? "" : "disabled"} />
              </div>
              <div>
                <label>allow_auto_route</label>
                <select class="agent-auto" ${item.is_editable ? "" : "disabled"}>
                  <option value="true" ${item.allow_auto_route ? "selected" : ""}>true</option>
                  <option value="false" ${!item.allow_auto_route ? "selected" : ""}>false</option>
                </select>
              </div>
            </div>
            <div class="row2" style="margin-top:12px;">
              <div>
                <label>preferred_model_alias</label>
                <select class="agent-alias" ${item.is_editable ? "" : "disabled"}>
                  ${aliasOptions(item.preferred_model_alias)}
                </select>
              </div>
              <div>
                <label>note</label>
                <input class="agent-note" value="${(item.note || "").replaceAll('"', '&quot;')}" ${item.is_editable ? "" : "disabled"} />
              </div>
            </div>
            <div class="actions" style="margin-top:12px;">
              <button class="agent-save" ${item.is_editable ? "" : "disabled"}>保存当前项目 override</button>
              <button class="agent-reset secondary" ${item.is_editable ? "" : "disabled"}>恢复默认</button>
            </div>
          </div>
        `).join("");

        container.querySelectorAll(".agent-save").forEach((button) => {
          button.addEventListener("click", async (event) => {
            const card = event.target.closest(".agent-card");
            const agentKey = card.dataset.agentKey;
            await runAction({
              ids: ["agentsRefreshBtn"],
              loadingText: `正在保存 ${agentKey}...`,
              fn: async () => parseJson(await authorizedFetch(`/gateway/agent-configs/${agentKey}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  project_id: selectedProject(),
                  enabled: card.querySelector(".agent-enabled").value === "true",
                  priority: Number(card.querySelector(".agent-priority").value),
                  allow_auto_route: card.querySelector(".agent-auto").value === "true",
                  preferred_model_alias: card.querySelector(".agent-alias").value || null,
                  note: card.querySelector(".agent-note").value,
                }),
              })),
              onDone: async () => refreshAgents(),
            });
          });
        });

        container.querySelectorAll(".agent-reset").forEach((button) => {
          button.addEventListener("click", async (event) => {
            const card = event.target.closest(".agent-card");
            const agentKey = card.dataset.agentKey;
            await runAction({
              ids: ["agentsRefreshBtn"],
              loadingText: `正在恢复 ${agentKey} 默认配置...`,
              fn: async () => parseJson(await authorizedFetch(`/gateway/agent-configs/${agentKey}?project_id=${encodeURIComponent(selectedProject())}`, {
                method: "DELETE",
              })),
              onDone: async () => refreshAgents(),
            });
          });
        });
      }

      async function refreshAgents() {
        return runAction({
          ids: ["agentsRefreshBtn"],
          loadingText: "正在加载 Agent 配置...",
          fn: async () => {
            const [catalog, effective, bindings] = await Promise.all([
              parseJson(await authorizedFetch("/gateway/agent-catalog")),
              parseJson(await authorizedFetch(`/gateway/agent-configs/effective?project_id=${encodeURIComponent(selectedProject())}`)),
              parseJson(await authorizedFetch("/gateway/agent-configs")),
            ]);
            const payload = { catalog, effective, bindings };
            renderAgentCards(effective.items || []);
            return payload;
          },
        });
      }

      function renderTraceTimeline(summary) {
        setOutput("traceOutput", summary);
        const container = $("traceTimeline");
        const items = summary.audit_timeline || [];
        if (!items.length) {
          container.innerHTML = "";
          return;
        }
        container.innerHTML = items.map((item) => `
          <div class="timeline-item">
            <strong>${item.title}</strong>
            <div class="mini">${item.timestamp} | ${item.event_type}</div>
            ${item.payload?.phase ? `<div class="mini">phase=${item.payload.phase}</div>` : ""}
            <div style="margin-top:8px;">${item.summary}</div>
          </div>
        `).join("");
      }

      async function loadTraceSummary(traceId = "", switchTab = true) {
        const effectiveTraceId = traceId || $("traceIdInput").value || state.lastTraceId;
        if (!effectiveTraceId) {
          throw new Error("当前没有可用 trace_id");
        }
        const data = await runAction({
          ids: ["traceBtn", "traceLatestBtn", "chatTraceBtn"],
          outputId: "traceOutput",
          loadingText: "正在加载 Trace Summary...",
          fn: async () => parseJson(await fetch(`/gateway/trace/${encodeURIComponent(effectiveTraceId)}/summary`)),
        });
        renderTraceTimeline(data);
        if (switchTab) activateTab("trace");
        return data;
      }

      $("tokenBtn").addEventListener("click", fetchDemoToken);
      $("codexBtn").addEventListener("click", fetchCodexToken);
      $("chatBtn").addEventListener("click", () => chat(false));
      $("mockBtn").addEventListener("click", () => chat(true));
      $("chatTraceBtn").addEventListener("click", () => loadTraceSummary());
      $("knowledgeBtn").addEventListener("click", loadKnowledge);
      $("workspaceDirectBtn").addEventListener("click", workspaceDirectList);
      $("workspaceReadBtn").addEventListener("click", workspaceDirectRead);
      $("workspaceWriteBtn").addEventListener("click", workspaceDirectWrite);
      $("workspaceMcpListBtn").addEventListener("click", () => workspaceMcp("list"));
      $("workspaceMcpReadBtn").addEventListener("click", () => workspaceMcp("read"));
      $("workspaceMcpWriteBtn").addEventListener("click", () => workspaceMcp("write"));
      $("executionRunBtn").addEventListener("click", runExecution);
      $("executionStatusBtn").addEventListener("click", loadExecutionStatus);
      $("executionLogsBtn").addEventListener("click", loadExecutionLogs);
      $("executionArtifactsBtn").addEventListener("click", loadExecutionArtifacts);
      $("externalListBtn").addEventListener("click", () => listExternalAgents(false));
      $("externalRefreshBtn").addEventListener("click", refreshExternalAgents);
      $("externalInvokeBtn").addEventListener("click", invokeExternalAgent);
      $("agentsRefreshBtn").addEventListener("click", refreshAgents);
      $("traceBtn").addEventListener("click", () => loadTraceSummary());
      $("traceLatestBtn").addEventListener("click", () => loadTraceSummary(state.lastTraceId || "", true));

      async function bootstrap() {
        try {
          await Promise.all([fetchRuntimeStatus(), loadUsers()]);
          await refreshAgents();
          setActionStatus("调试台已准备好");
        } catch (error) {
          setActionStatus(error?.message || String(error), "error");
        }
      }

      bootstrap();
    </script>
  </body>
</html>
"""
