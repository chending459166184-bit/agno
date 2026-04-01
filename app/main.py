from __future__ import annotations

import json
from uuid import uuid4

from agno.os import AgentOS
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.auth import decode_token, issue_demo_token, read_codex_bridge_user
from app.config import Settings, get_settings
from app.context import AuthenticatedUser, RequestContext
from app.db import Database
from app.external_agents import (
    A2AClient,
    ExternalAgentBroker,
    ExternalAgentDiscovery,
    ExternalAgentRegistry,
)
from app.model_gateway import LiteLLMHealthChecker, ModelRegistry, ModelRouter
from app.runtime import OrchestratorRuntime
from app.workspace import list_files


class ChatRequest(BaseModel):
    message: str
    project_id: str | None = None
    session_id: str | None = None
    use_mock: bool | None = None


class ExternalAgentRefreshRequest(BaseModel):
    project_id: str | None = None
    session_id: str | None = None


class ExternalAgentInvokeRequest(BaseModel):
    message: str
    project_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    category: str | None = None
    capability: str | None = None
    preferred_name: str | None = None
    force_refresh: bool = False
    metadata: dict | None = None


class AppServices:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.resolved_db_file)
        self.database.init_schema()
        if not self.database.list_users():
            self.database.seed_demo_data(
                settings.project_root,
                settings.resolved_workspace_root,
                settings.resolved_external_agent_catalog_file,
            )
        else:
            self.database.ensure_demo_external_agent_catalog(
                settings.resolved_external_agent_catalog_file
            )
        self.model_registry = ModelRegistry(settings)
        self.model_router = ModelRouter(self.model_registry)
        self.health_checker = LiteLLMHealthChecker(settings, self.model_registry)
        self.external_discovery = ExternalAgentDiscovery(settings)
        self.external_registry = ExternalAgentRegistry(self.external_discovery)
        self.a2a_client = A2AClient(settings, self.external_discovery.config.default_a2a)
        self.external_broker = ExternalAgentBroker(
            settings,
            self.database,
            self.external_registry,
            self.a2a_client,
        )
        self.runtime = OrchestratorRuntime(
            settings,
            self.database,
            self.model_router,
            self.health_checker,
            self.external_broker,
        )

    def build_context(
        self,
        *,
        user: AuthenticatedUser,
        project_id: str | None,
        session_id: str | None,
    ) -> RequestContext:
        effective_project_id = project_id or user.default_project_id
        if effective_project_id not in user.project_ids:
            raise HTTPException(status_code=403, detail="当前用户无权访问该项目")
        ctx = RequestContext(
            trace_id=f"trace_{uuid4().hex[:12]}",
            request_id=f"req_{uuid4().hex[:12]}",
            session_id=session_id or f"session_{uuid4().hex[:12]}",
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            display_name=user.display_name,
            role=user.role,
            project_id=effective_project_id,
            workspace_root=self.settings.resolved_workspace_root / user.tenant_id / user.user_id,
        )
        self.database.touch_session_context(ctx)
        return ctx


def get_services(request: Request) -> AppServices:
    return request.app.state.services


def get_current_user(
    authorization: str | None = Header(default=None),
    services: AppServices = Depends(get_services),
) -> AuthenticatedUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return decode_token(services.settings, token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"无效 token: {exc}") from exc


def load_demo_external_agents(settings: Settings) -> list[dict]:
    catalog_file = settings.resolved_external_agent_catalog_file
    if not catalog_file.exists():
        return []
    payload = json.loads(catalog_file.read_text(encoding="utf-8"))
    return [item for item in payload.get("agents", []) if isinstance(item, dict)]


def get_demo_external_agent(settings: Settings, agent_id: str) -> dict | None:
    for agent in load_demo_external_agents(settings):
        if str(agent.get("agent_id")) == agent_id:
            return agent
    return None


def extract_a2a_prompt(payload: dict) -> str:
    parts = (
        payload.get("params", {})
        .get("message", {})
        .get("parts", [])
    )
    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text":
            texts.append(str(part.get("text") or ""))
    return "\n".join(texts).strip()


def build_demo_external_answer(
    agent: dict,
    *,
    prompt: str,
    project_id: str,
    user_id: str,
) -> str:
    metadata = agent.get("metadata") or {}
    specialty = str(metadata.get("specialty") or agent.get("category") or "external review")
    response_style = str(metadata.get("response_style") or "给出结论和下一步")
    category = str(agent.get("category") or "general")
    lines = [
        f"{agent.get('name')} 已通过 A2A 收到任务。",
        f"- category: {category}",
        f"- specialty: {specialty}",
        f"- style: {response_style}",
        f"- project: {project_id}",
        f"- requester: {user_id}",
        "",
        "判断:",
    ]
    if category == "compliance":
        lines.extend(
            [
                "- 先确认流程边界、审批规则和验收口径是否已经定义清楚。",
                "- 如果要引入外部能力，必须把 discovery、selection、A2A request/response 纳入同一审计链。",
                "- 对外部建议的采用要保留最终内部确认人，不要把责任边界完全外包。",
            ]
        )
    elif category == "security":
        lines.extend(
            [
                "- 重点看租户、项目、用户三层边界是否同时生效。",
                "- 外部委托前后都应保留 trace_id，并校验是否存在跨用户数据泄露面。",
                "- 对 A2A 返回结果要做二次摘要，避免把协议细节直接暴露给最终用户。",
            ]
        )
    elif category == "analytics":
        lines.extend(
            [
                "- 先把关键目标转成可验证指标，例如 discovery 成功率、A2A 成功率和审计完整度。",
                "- 区分静态内部智能体输出与外部动态发现结果，避免混淆来源。",
                "- 为外部调用补一条失败回退路径，便于本地 PoC 演示稳定性。",
            ]
        )
    else:
        lines.append("- 建议先确认任务目标、边界和预期输出，再决定是否需要更深的外部协作。")
    lines.extend(
        [
            "",
            "任务摘录:",
            prompt[:240] or "无",
        ]
    )
    return "\n".join(lines)


def render_index() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Agno Enterprise Local PoC</title>
    <style>
      :root {
        --bg: #f4efe5;
        --panel: rgba(255, 251, 245, 0.92);
        --ink: #1f2937;
        --muted: #5f6c80;
        --accent: #0f766e;
        --accent-soft: #dff4f1;
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
      .shell {
        max-width: 1120px;
        margin: 0 auto;
        padding: 32px 20px 48px;
      }
      .hero {
        padding: 28px;
        border-radius: 28px;
        background: linear-gradient(135deg, rgba(255,255,255,0.7), rgba(255,248,240,0.95));
        border: 1px solid rgba(255,255,255,0.7);
        box-shadow: 0 22px 48px rgba(31, 41, 55, 0.08);
      }
      .hero h1 {
        margin: 0;
        font-size: clamp(32px, 5vw, 54px);
        line-height: 1.02;
        letter-spacing: -0.03em;
      }
      .hero p {
        max-width: 720px;
        color: var(--muted);
        font-size: 16px;
        line-height: 1.7;
      }
      .grid {
        margin-top: 22px;
        display: grid;
        gap: 18px;
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      }
      .panel {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 24px;
        padding: 22px;
        box-shadow: 0 16px 36px rgba(31, 41, 55, 0.06);
        backdrop-filter: blur(10px);
      }
      label {
        display: block;
        font-size: 13px;
        font-weight: 600;
        margin-bottom: 8px;
        color: var(--muted);
      }
      input, select, textarea, button {
        width: 100%;
        border-radius: 16px;
        border: 1px solid rgba(95,108,128,0.18);
        padding: 12px 14px;
        font: inherit;
        background: white;
      }
      textarea { min-height: 160px; resize: vertical; }
      button {
        cursor: pointer;
        border: none;
        background: linear-gradient(135deg, #0f766e, #115e59);
        color: white;
        font-weight: 700;
      }
      .row { display: grid; gap: 12px; }
      .actions { display: flex; gap: 10px; }
      .actions button { flex: 1; }
      pre {
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: "SF Mono", "JetBrains Mono", monospace;
        font-size: 13px;
        line-height: 1.6;
      }
      .badge {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        margin-top: 12px;
        padding: 10px 14px;
        border-radius: 999px;
        background: var(--accent-soft);
        color: var(--accent);
        font-weight: 700;
      }
      .muted { color: var(--muted); }
      @media (max-width: 700px) {
        .shell { padding: 18px 14px 30px; }
        .hero, .panel { padding: 18px; border-radius: 20px; }
      }
    </style>
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <h1>Agno 企业主智能体本地 PoC</h1>
        <p>这个页面会走自定义 Agent Gateway，由它注入用户、项目、用户空间和审计上下文，再把请求交给 Agno Team。除了内部智能体，你现在还可以通过 MCP discovery + A2A 调试外部动态发现智能体。</p>
        <div class="badge" id="status">尚未获取 token</div>
      </section>

      <div class="grid">
        <section class="panel">
          <div class="row">
            <div>
              <label for="user">演示用户</label>
              <select id="user">
                <option value="alice">alice / Alpha 项目经理</option>
                <option value="bob">bob / Beta 测试同学</option>
              </select>
            </div>
            <div>
              <label for="project">项目</label>
              <select id="project">
                <option value="alpha">alpha</option>
                <option value="beta">beta</option>
              </select>
            </div>
            <div class="actions">
              <button id="tokenBtn" type="button">获取本地 Token</button>
              <button id="codexBtn" type="button">使用 Codex 登录态</button>
              <button id="filesBtn" type="button">查看用户空间</button>
            </div>
            <div>
              <label for="message">提问</label>
              <textarea id="message">请结合我的项目知识库和个人空间文件，给我一份 PoC 的测试建议，重点关注多用户隔离、MCP 调用和审计。</textarea>
            </div>
            <div>
              <label for="externalCategory">外部 agent category</label>
              <input id="externalCategory" placeholder="例如 compliance / security / analytics" />
            </div>
            <div>
              <label for="externalAgentId">外部 agent id（可选）</label>
              <input id="externalAgentId" placeholder="留空则由 Broker 自动选择" />
            </div>
            <div class="actions">
              <button id="chatBtn" type="button">发送到主智能体</button>
              <button id="mockBtn" type="button">强制走 Mock 模式</button>
            </div>
            <div class="actions">
              <button id="externalListBtn" type="button">查看外部 Agents</button>
              <button id="externalRefreshBtn" type="button">刷新外部目录</button>
              <button id="externalInvokeBtn" type="button">直接调用外部 Agent</button>
            </div>
            <p class="muted">也可以直接打开 <a href="/docs">/docs</a> 或 Agno Team 的原生接口。</p>
          </div>
        </section>

        <section class="panel">
          <label>返回结果</label>
          <pre id="output">等待请求...</pre>
        </section>

        <section class="panel">
          <label>External Agents</label>
          <pre id="externalOutput">等待查询...</pre>
        </section>
      </div>
    </div>

    <script>
      const statusEl = document.getElementById("status");
      const outputEl = document.getElementById("output");
      const externalEl = document.getElementById("externalOutput");
      let token = "";

      async function fetchRuntimeStatus() {
        const res = await fetch("/gateway/runtime-status");
        const data = await res.json();
        const healthy = (data.healthy_aliases || []).join(", ") || "无";
        const externalCount = data.external_agents?.count ?? 0;
        statusEl.textContent = data.live
          ? `当前为 live 模式，healthy aliases=${healthy}，external agents=${externalCount}`
          : `当前为 mock 模式，原因：${data.reason}`;
      }

      async function fetchToken() {
        const user = document.getElementById("user").value;
        const res = await fetch(`/gateway/dev-token/${user}`);
        const data = await res.json();
        token = data.token;
        statusEl.textContent = `已获取 ${user} 的本地 token`;
        outputEl.textContent = JSON.stringify(data, null, 2);
      }

      async function fetchCodexToken() {
        const res = await fetch(`/gateway/codex-login`, { method: "POST" });
        const data = await res.json();
        token = data.token;
        statusEl.textContent = `已桥接 Codex 登录态 -> ${data.user.user_id}`;
        outputEl.textContent = JSON.stringify(data, null, 2);
      }

      async function chat(useMock = false) {
        if (!token) {
          await fetchToken();
        }
        const res = await fetch("/gateway/chat", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${token}`
          },
          body: JSON.stringify({
            message: document.getElementById("message").value,
            project_id: document.getElementById("project").value,
            use_mock: useMock
          })
        });
        const data = await res.json();
        outputEl.textContent = JSON.stringify(data, null, 2);
        const routeText = data.model_routes
          ? Object.entries(data.model_routes).map(([k, v]) => `${k}:${v}`).join(" | ")
          : "未返回路由";
        statusEl.textContent = `已完成 ${data.mode} 响应，trace_id=${data.trace_id}，routes=${routeText}`;
      }

      async function viewWorkspace() {
        if (!token) {
          await fetchToken();
        }
        const res = await fetch("/gateway/workspace", {
          headers: { "Authorization": `Bearer ${token}` }
        });
        const data = await res.json();
        outputEl.textContent = JSON.stringify(data, null, 2);
        statusEl.textContent = `已读取 ${data.user_id} 的用户空间`;
      }

      async function listExternalAgents(forceRefresh = false) {
        if (!token) {
          await fetchToken();
        }
        const category = document.getElementById("externalCategory").value;
        const params = new URLSearchParams();
        if (category) params.set("category", category);
        if (forceRefresh) params.set("force_refresh", "true");
        params.set("project_id", document.getElementById("project").value);
        const res = await fetch(`/gateway/external-agents?${params.toString()}`, {
          headers: { "Authorization": `Bearer ${token}` }
        });
        const data = await res.json();
        externalEl.textContent = JSON.stringify(data, null, 2);
        statusEl.textContent = `已获取 external agents，count=${data.count}`;
      }

      async function refreshExternalAgents() {
        if (!token) {
          await fetchToken();
        }
        const res = await fetch("/gateway/external-agents/refresh", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${token}`
          },
          body: JSON.stringify({
            project_id: document.getElementById("project").value
          })
        });
        const data = await res.json();
        externalEl.textContent = JSON.stringify(data, null, 2);
        statusEl.textContent = `已刷新 external catalog，count=${data.count}`;
      }

      async function invokeExternalAgent() {
        if (!token) {
          await fetchToken();
        }
        const res = await fetch("/gateway/external-agents/invoke", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${token}`
          },
          body: JSON.stringify({
            message: document.getElementById("message").value,
            project_id: document.getElementById("project").value,
            agent_id: document.getElementById("externalAgentId").value || null,
            category: document.getElementById("externalCategory").value || null
          })
        });
        const data = await res.json();
        externalEl.textContent = JSON.stringify(data, null, 2);
        statusEl.textContent = `已调用 external agent，trace_id=${data.trace_id}`;
      }

      document.getElementById("tokenBtn").addEventListener("click", fetchToken);
      document.getElementById("codexBtn").addEventListener("click", fetchCodexToken);
      document.getElementById("chatBtn").addEventListener("click", () => chat(false));
      document.getElementById("mockBtn").addEventListener("click", () => chat(true));
      document.getElementById("filesBtn").addEventListener("click", viewWorkspace);
      document.getElementById("externalListBtn").addEventListener("click", () => listExternalAgents(false));
      document.getElementById("externalRefreshBtn").addEventListener("click", refreshExternalAgents);
      document.getElementById("externalInvokeBtn").addEventListener("click", invokeExternalAgent);
      fetchRuntimeStatus();
    </script>
  </body>
</html>
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    services = AppServices(settings)
    base_app = FastAPI(
        title="Agno Enterprise Local PoC",
        version="0.1.0",
        description="主智能体 + 子智能体 + MCP + 多用户隔离的本地演示版",
    )
    base_app.state.services = services

    @base_app.get("/debug", response_class=HTMLResponse)
    def index() -> str:
        return render_index()

    @base_app.get("/gateway/dev-token/{user_id}")
    def create_dev_token(user_id: str, svc: AppServices = Depends(get_services)) -> dict:
        row = svc.database.get_user(user_id)
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        user = AuthenticatedUser(
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            display_name=row["display_name"],
            role=row["role"],
            project_ids=list(row["project_ids_json"]),
            default_project_id=row["default_project_id"],
        )
        token = issue_demo_token(svc.settings, user)
        return {"token": token, "user": user.user_id, "project_ids": user.project_ids}

    @base_app.get("/gateway/codex-status")
    def codex_status(svc: AppServices = Depends(get_services)) -> dict:
        try:
            user, identity = read_codex_bridge_user(svc.settings)
            return {
                "available": True,
                "auth_file": str(svc.settings.resolved_codex_auth_file),
                "user": {
                    "user_id": user.user_id,
                    "display_name": user.display_name,
                    "role": user.role,
                    "project_ids": user.project_ids,
                },
                "identity": {
                    "email": identity.get("email"),
                    "name": identity.get("name"),
                    "exp": identity.get("exp"),
                    "auth_mode": identity.get("auth_mode"),
                    "token_freshness": identity.get("token_freshness"),
                },
            }
        except Exception as exc:
            return {
                "available": False,
                "auth_file": str(svc.settings.resolved_codex_auth_file),
                "detail": str(exc),
            }

    @base_app.get("/gateway/runtime-status")
    def runtime_status(svc: AppServices = Depends(get_services)) -> dict:
        status = svc.health_checker.probe(force_refresh=True)
        payload = status.as_dict()
        payload.update(
            {
                "allow_mock_fallback": svc.settings.allow_mock_fallback,
                "router_defaults": svc.model_registry.default_aliases_by_task(),
                "external_agents": svc.external_registry.status(),
            }
        )
        return payload

    @base_app.post("/gateway/codex-login")
    def codex_login(svc: AppServices = Depends(get_services)) -> dict:
        user, identity = read_codex_bridge_user(svc.settings)
        svc.database.provision_codex_bridge_user(
            user=user,
            workspace_root=svc.settings.resolved_workspace_root,
            identity=identity,
        )
        token = issue_demo_token(svc.settings, user)
        trace_id = f"trace_{uuid4().hex[:12]}"
        request_id = f"req_{uuid4().hex[:12]}"
        svc.database.append_audit(
            trace_id=trace_id,
            request_id=request_id,
            session_id=None,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            event_type="codex_bridge_login",
            payload={
                "bridge_mode": "codex_auth_json",
                "email": identity.get("email"),
                "auth_mode": identity.get("auth_mode"),
                "token_freshness": identity.get("token_freshness"),
                "project_ids": user.project_ids,
            },
        )
        return {
            "token": token,
            "bridge_mode": "codex_auth_json",
            "auth_file": str(svc.settings.resolved_codex_auth_file),
            "trace_id": trace_id,
            "user": {
                "tenant_id": user.tenant_id,
                "user_id": user.user_id,
                "display_name": user.display_name,
                "role": user.role,
                "project_ids": user.project_ids,
                "default_project_id": user.default_project_id,
            },
            "identity": {
                "email": identity.get("email"),
                "name": identity.get("name"),
                "exp": identity.get("exp"),
                "auth_mode": identity.get("auth_mode"),
                "token_freshness": identity.get("token_freshness"),
            },
            "note": (
                "这是本机 Codex 登录态桥接模式，适合本地 PoC，不等同于官方开放 SSO。"
                if identity.get("token_freshness") == "fresh"
                else "检测到的是 stale 的本地 Codex 会话材料，当前按本机可信模式允许桥接。"
            ),
        }

    @base_app.get("/gateway/me")
    def me(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
        return {
            "tenant_id": user.tenant_id,
            "user_id": user.user_id,
            "display_name": user.display_name,
            "role": user.role,
            "project_ids": user.project_ids,
        }

    @base_app.get("/gateway/workspace")
    def workspace(
        user: AuthenticatedUser = Depends(get_current_user),
        svc: AppServices = Depends(get_services),
    ) -> dict:
        root = svc.settings.resolved_workspace_root / user.tenant_id / user.user_id
        return {
            "user_id": user.user_id,
            "root": str(root),
            "files": list_files(root, limit=20),
        }

    @base_app.get("/gateway/knowledge")
    def knowledge_search(
        q: str = Query(..., min_length=1),
        project_id: str | None = Query(default=None),
        user: AuthenticatedUser = Depends(get_current_user),
        svc: AppServices = Depends(get_services),
    ) -> dict:
        ctx = svc.build_context(user=user, project_id=project_id, session_id=None)
        hits = svc.database.search_knowledge(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            project_id=ctx.project_id,
            query=q,
        )
        return {"trace_id": ctx.trace_id, "hits": hits}

    @base_app.get("/gateway/external-agents")
    def external_agents(
        category: str | None = Query(default=None),
        capability: str | None = Query(default=None),
        name_query: str | None = Query(default=None),
        force_refresh: bool = Query(default=False),
        project_id: str | None = Query(default=None),
        user: AuthenticatedUser = Depends(get_current_user),
        svc: AppServices = Depends(get_services),
    ) -> dict:
        ctx = svc.build_context(user=user, project_id=project_id, session_id=None)
        snapshot = svc.external_broker.list_agents(
            ctx=ctx,
            force_refresh=force_refresh,
            category=category,
            capability=capability,
            name_query=name_query,
        )
        return {
            "trace_id": ctx.trace_id,
            "project_id": ctx.project_id,
            "count": len(snapshot.agents),
            "from_cache": snapshot.from_cache,
            "fetched_at": snapshot.fetched_at,
            "expires_at": snapshot.expires_at,
            "sources": [item.model_dump() for item in snapshot.sources],
            "agents": [item.model_dump() for item in snapshot.agents],
        }

    @base_app.post("/gateway/external-agents/refresh")
    def refresh_external_agents(
        body: ExternalAgentRefreshRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        svc: AppServices = Depends(get_services),
    ) -> dict:
        ctx = svc.build_context(user=user, project_id=body.project_id, session_id=body.session_id)
        snapshot = svc.external_broker.refresh_agents(ctx)
        return {
            "trace_id": ctx.trace_id,
            "project_id": ctx.project_id,
            "count": len(snapshot.agents),
            "from_cache": snapshot.from_cache,
            "fetched_at": snapshot.fetched_at,
            "expires_at": snapshot.expires_at,
            "sources": [item.model_dump() for item in snapshot.sources],
            "agents": [item.model_dump() for item in snapshot.agents],
        }

    @base_app.post("/gateway/external-agents/invoke")
    def invoke_external_agent(
        body: ExternalAgentInvokeRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        svc: AppServices = Depends(get_services),
    ) -> dict:
        ctx = svc.build_context(user=user, project_id=body.project_id, session_id=body.session_id)
        try:
            result = svc.external_broker.invoke(
                ctx=ctx,
                message=body.message,
                agent_id=body.agent_id,
                category=body.category,
                capability=body.capability,
                preferred_name=body.preferred_name,
                force_refresh=body.force_refresh,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "session_id": ctx.session_id,
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "project_id": ctx.project_id,
            **result.model_dump(),
        }

    @base_app.get("/gateway/audit/{trace_id}")
    def trace_audit(trace_id: str, svc: AppServices = Depends(get_services)) -> dict:
        return {"trace_id": trace_id, "events": svc.database.list_audit_events(trace_id)}

    @base_app.get("/demo-a2a/agents/{agent_id}/.well-known/agent-card.json")
    def demo_external_agent_card(
        agent_id: str,
        request: Request,
        svc: AppServices = Depends(get_services),
    ) -> dict:
        agent = get_demo_external_agent(svc.settings, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="external agent 不存在")
        base_url = str(request.base_url).rstrip("/")
        return {
            "name": agent.get("name"),
            "version": "1.0.0",
            "description": agent.get("description"),
            "url": f"{base_url}/demo-a2a/agents/{agent_id}/v1/message:send",
            "default_input_modes": ["text"],
            "default_output_modes": ["text"],
            "skills": [
                {
                    "id": capability,
                    "name": capability,
                    "description": f"{agent.get('name')} capability: {capability}",
                    "tags": [agent.get("category")],
                }
                for capability in agent.get("capabilities") or []
            ],
        }

    @base_app.post("/demo-a2a/agents/{agent_id}/v1/message:send")
    async def demo_external_agent_message(
        agent_id: str,
        request: Request,
        svc: AppServices = Depends(get_services),
    ) -> dict:
        agent = get_demo_external_agent(svc.settings, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="external agent 不存在")
        payload = await request.json()
        prompt = extract_a2a_prompt(payload)
        user_id = request.headers.get("X-User-ID", "unknown")
        project_id = request.headers.get("X-Project-ID", "unknown")
        response_text = build_demo_external_answer(
            agent,
            prompt=prompt,
            project_id=project_id,
            user_id=user_id,
        )
        context_id = (
            payload.get("params", {})
            .get("message", {})
            .get("contextId")
            or f"context_{uuid4().hex[:12]}"
        )
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id", f"a2a_{uuid4().hex[:12]}"),
            "result": {
                "task": {
                    "id": f"task_{uuid4().hex[:12]}",
                    "context_id": context_id,
                    "status": {"state": "completed"},
                    "history": [
                        {
                            "message_id": f"msg_{uuid4().hex[:12]}",
                            "role": "agent",
                            "parts": [{"kind": "text", "text": response_text}],
                        }
                    ],
                }
            },
        }

    @base_app.post("/gateway/chat")
    def chat(
        body: ChatRequest,
        user: AuthenticatedUser = Depends(get_current_user),
        svc: AppServices = Depends(get_services),
    ) -> dict:
        ctx = svc.build_context(user=user, project_id=body.project_id, session_id=body.session_id)
        svc.database.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="gateway_request",
            payload={"project_id": ctx.project_id, "message": body.message},
        )
        result = svc.runtime.run(ctx, body.message, use_mock=body.use_mock)
        run_id = svc.database.create_run(
            ctx=ctx,
            input_text=body.message,
            output_text=result.answer,
            status="success",
            mode=result.mode,
            selected_agents=result.selected_agents,
        )
        svc.database.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="gateway_response",
            payload={
                "run_id": run_id,
                "mode": result.mode,
                "selected_agents": result.selected_agents,
                "knowledge_hit_count": len(result.knowledge_hits),
                "model_routes": result.model_routes,
            },
        )
        return {
            "answer": result.answer,
            "mode": result.mode,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "run_id": run_id,
            "session_id": ctx.session_id,
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "project_id": ctx.project_id,
            "workspace_root": str(ctx.workspace_root),
            "knowledge_hits": result.knowledge_hits,
            "member_outputs": result.member_outputs,
            "model_routes": result.model_routes,
            "notes": result.notes,
        }

    default_team = services.runtime.build_default_team(settings.project_root)
    agent_os = AgentOS(
        name="Agno Enterprise Local PoC",
        db=services.runtime.agno_db,
        teams=[default_team],
        base_app=base_app,
        tracing=settings.agno_tracing_enabled,
        telemetry=settings.telemetry_enabled,
    )
    app = agent_os.get_app()
    app.state.services = services
    return app


app = create_app()
