from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field

import httpx
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.skills import LocalSkills, Skills
from agno.team import Team
from agno.team.mode import TeamMode
from agno.tools.mcp import MCPTools

from app.agent_configs import AgentConfigService, EffectiveAgentConfig
from app.config import Settings
from app.context import RequestContext
from app.db import Database
from app.external_agents import ExternalAgentBroker
from app.model_gateway import LiteLLMHealthChecker, ModelRouter, build_agno_model
from app.model_gateway.task_types import (
    TASK_EXTERNAL_BROKER,
    TASK_KNOWLEDGE,
    TASK_ORCHESTRATE,
    TASK_TESTING,
    TASK_WORKSPACE,
)
from app.workspace import list_files, read_text_file
from app.workspace_mcp import build_workspace_mcp_env, call_workspace_mcp_tool


@dataclass(slots=True)
class RunResult:
    answer: str
    mode: str
    selected_agents: list[str]
    member_outputs: list[dict]
    knowledge_hits: list[dict]
    notes: list[str]
    model_routes: dict[str, str] = field(default_factory=dict)
    prefetch_info: dict = field(default_factory=dict)
    effective_agents: list[dict] = field(default_factory=list)


class OrchestratorRuntime:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        model_router: ModelRouter,
        health_checker: LiteLLMHealthChecker,
        external_agent_broker: ExternalAgentBroker,
        agent_config_service: AgentConfigService,
    ) -> None:
        self.settings = settings
        self.database = database
        self.model_router = model_router
        self.health_checker = health_checker
        self.external_agent_broker = external_agent_broker
        self.agent_config_service = agent_config_service
        self.agno_db = SqliteDb(db_file=str(settings.resolved_db_file))
        self.orchestrator_skills = self._load_skills("shared", "orchestrator")
        self.external_broker_skills = self._load_skills("shared", "external-broker")
        self.testing_skills = self._load_skills("testing")

    def _load_skills(self, *relative_dirs: str) -> Skills | None:
        loaders: list[LocalSkills] = []
        for rel in relative_dirs:
            path = self.settings.resolved_skills_root / rel
            if path.exists():
                loaders.append(LocalSkills(str(path)))
        if not loaders:
            return None
        return Skills(loaders=loaders)

    def _effective_agents(self, ctx: RequestContext) -> list[EffectiveAgentConfig]:
        return self.agent_config_service.get_effective_configs(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            project_id=ctx.project_id,
        )

    def _preferred_aliases(
        self,
        config: EffectiveAgentConfig | None,
        healthy_aliases: set[str] | None,
    ) -> set[str] | None:
        preferred = (
            {config.preferred_model_alias}
            if config and config.preferred_model_alias
            else set()
        )
        if healthy_aliases:
            if preferred and preferred & healthy_aliases:
                return preferred & healthy_aliases
            return healthy_aliases
        return preferred or None

    def _detect_workspace_guard(self, prompt: str) -> dict | None:
        lowered = (prompt or "").lower()
        guard_patterns = [
            r"(当前|我|我的).{0,4}(目录|工作区|workspace|文件)",
            r"(目录|工作区|workspace|current directory|my directory).{0,8}(有哪些|有什么|文件|内容|list|files|show)",
            r"(文件|目录|workspace|folder|directory).{0,6}(有哪些|有什么|列表|列出|内容|list|show)",
            r"(读取|读|打开|查看|read|open).{0,8}(文件|目录|workspace|file|directory)",
            r"(写入|保存|写|save|write).{0,8}(文件|目录|工作区|workspace|file|directory)",
        ]
        if not any(re.search(pattern, lowered, re.IGNORECASE) for pattern in guard_patterns):
            return None

        action = "list"
        if any(keyword in lowered for keyword in ["写入", "保存", "save", "write"]):
            action = "write"
        elif any(keyword in lowered for keyword in ["读取", "查看文件", "读文件", "read", "open"]):
            action = "read"
        elif any(keyword in lowered for keyword in ["目录", "文件", "list", "有哪些"]):
            action = "list"

        path_match = re.search(
            r"([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+|[A-Za-z0-9._-]+\.[A-Za-z0-9._-]+)",
            prompt,
        )
        path = path_match.group(1) if path_match else None
        content = None
        content_match = re.search(r"(?:内容|content)\s*[:：]\s*(.+)$", prompt, re.IGNORECASE | re.DOTALL)
        if content_match:
            content = content_match.group(1).strip()
        return {"action": action, "path": path, "content": content}

    def _extract_completion_text(self, payload: dict) -> str:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
            return "".join(texts)
        return ""

    def _classify_workspace_access(
        self,
        prompt: str,
        *,
        healthy_aliases: set[str] | None,
    ) -> dict:
        heuristic = self._detect_workspace_guard(prompt)
        if heuristic is not None:
            return {
                "requires_workspace_access": True,
                "action": heuristic["action"],
                "path": heuristic.get("path"),
                "content": heuristic.get("content"),
                "source": "heuristic",
                "reason": "matched_explicit_workspace_pattern",
                "confidence": 1.0,
            }
        if not healthy_aliases:
            return {
                "requires_workspace_access": False,
                "action": None,
                "path": None,
                "content": None,
                "source": "no_live_alias",
                "reason": "classifier_skipped_without_live_alias",
                "confidence": 0.0,
            }
        route = self.model_router.resolve(TASK_ORCHESTRATE, preferred_aliases=healthy_aliases)
        headers = {"Authorization": f"Bearer {self.settings.litellm_master_key}"}
        system_prompt = (
            "你是一个安全访问判定器。"
            "判断用户请求是否需要访问当前用户的 workspace 文件或目录。"
            "只返回 JSON，不要返回其他文字。"
            "JSON 结构必须是"
            '{"requires_workspace_access": boolean, "action": "list|read|write|none", '
            '"path": string|null, "reason": string, "confidence": number}.'
            "凡是涉及列目录、列文件、查看我的文件、读取本地文件、保存到目录、写入工作区等，都应判为 true。"
            "如果不需要访问 workspace，返回 action=none。"
        )
        try:
            with httpx.Client(timeout=min(self.settings.litellm_request_timeout_seconds, 20.0)) as client:
                response = client.post(
                    f"{self.settings.litellm_proxy_base_url}/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": route.alias,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0,
                        "max_tokens": 180,
                    },
                )
                response.raise_for_status()
            text = self._extract_completion_text(response.json()).strip()
            parsed = json.loads(text)
            action = str(parsed.get("action") or "none").lower()
            if action not in {"list", "read", "write", "none"}:
                action = "none"
            return {
                "requires_workspace_access": bool(parsed.get("requires_workspace_access")),
                "action": None if action == "none" else action,
                "path": parsed.get("path"),
                "content": None,
                "source": "classifier",
                "reason": str(parsed.get("reason") or "classifier_decision"),
                "confidence": float(parsed.get("confidence") or 0.0),
            }
        except Exception as exc:
            return {
                "requires_workspace_access": False,
                "action": None,
                "path": None,
                "content": None,
                "source": "classifier_failed",
                "reason": f"classifier_failed:{exc}",
                "confidence": 0.0,
            }

    def _answer_looks_like_repo_listing(self, ctx: RequestContext, answer: str) -> bool:
        lowered = (answer or "").lower()
        project_root = str(self.settings.project_root).lower()
        workspace_root = str(ctx.workspace_root).lower()
        if project_root and project_root in lowered and workspace_root not in lowered:
            return True
        suspicious_markers = [
            ".git/",
            "app/",
            "configs/",
            "tests/",
            "requirements.txt",
            "README.md".lower(),
            ".env",
        ]
        hit_count = sum(1 for marker in suspicious_markers if marker.lower() in lowered)
        return hit_count >= 2

    def _record_member_outputs(self, ctx: RequestContext, member_outputs: list[dict]) -> None:
        for index, item in enumerate(member_outputs, start=1):
            self.database.record_member_output(
                ctx,
                member_name=item["name"],
                order=index,
                content=item["content"],
                phase=item.get("phase", "team"),
            )

    def _model_routes_snapshot(self) -> dict[str, str]:
        return {
            TASK_ORCHESTRATE: self.model_router.resolve(TASK_ORCHESTRATE).alias,
            TASK_KNOWLEDGE: self.model_router.resolve(TASK_KNOWLEDGE).alias,
            TASK_WORKSPACE: self.model_router.resolve(TASK_WORKSPACE).alias,
            TASK_TESTING: self.model_router.resolve(TASK_TESTING).alias,
            TASK_EXTERNAL_BROKER: self.model_router.resolve(TASK_EXTERNAL_BROKER).alias,
        }

    def _run_workspace_guard(
        self,
        ctx: RequestContext,
        prompt: str,
        *,
        access_decision: dict | None = None,
    ) -> RunResult | None:
        decision = access_decision or {}
        if not decision.get("requires_workspace_access"):
            return None

        effective_agents = [item.as_dict() for item in self._effective_agents(ctx)]
        member_outputs: list[dict] = []
        selected_agents = ["Workspace Agent"]
        notes = [
            "命中文件/目录类高风险请求，已启用 workspace guard。",
            "这次响应只允许基于当前用户 workspace_root 的真实 MCP 调用结果返回。",
            f"guard source={decision.get('source')}, reason={decision.get('reason')}",
        ]
        prefetch_info = {
            "enabled": False,
            "mode": "off",
            "triggered": False,
            "category": None,
            "matched_keywords": [],
        }
        model_routes = self._model_routes_snapshot()

        try:
            action = decision.get("action") or "list"
            if action == "write":
                if not decision.get("path") or not decision.get("content"):
                    raise ValueError("当前无法安全解析写入路径或内容，请明确给出相对路径和内容。")
                payload = call_workspace_mcp_tool(
                    self.settings,
                    ctx,
                    "workspace_save_text_file",
                    {"path": decision["path"], "content": decision["content"], "overwrite": True},
                )
                agent_text = (
                    "Workspace Agent 已通过 MCP 写入当前用户工作区。\n"
                    f"- root: {payload.get('root', ctx.workspace_root)}\n"
                    f"- path: {payload.get('path')}\n"
                    f"- size: {payload.get('size')}"
                )
                answer = agent_text
            elif action == "read":
                if not decision.get("path"):
                    raise ValueError("当前无法安全解析要读取的相对路径，请明确指定文件名。")
                payload = call_workspace_mcp_tool(
                    self.settings,
                    ctx,
                    "workspace_read_text_file",
                    {"path": decision["path"], "max_chars": 6000},
                )
                answer = (
                    "Workspace Agent 已通过 MCP 读取当前用户工作区文件。\n"
                    f"- root: {ctx.workspace_root}\n"
                    f"- path: {payload.get('path')}\n"
                    f"- content:\n{payload.get('content', '')}"
                )
                agent_text = answer
            else:
                payload = call_workspace_mcp_tool(
                    self.settings,
                    ctx,
                    "workspace_list_files",
                    {"prefix": "", "limit": 50},
                )
                files = payload.get("files") or []
                if files:
                    lines = [
                        "Workspace Agent 已通过 MCP 列出当前用户工作区文件。",
                        f"- root: {payload.get('root', ctx.workspace_root)}",
                        "- files:",
                    ]
                    for item in files:
                        lines.append(f"  - {item.get('path')}")
                    answer = "\n".join(lines)
                else:
                    answer = (
                        "Workspace Agent 已通过 MCP 检查当前用户工作区，但没有发现可见文件。\n"
                        f"- root: {payload.get('root', ctx.workspace_root)}"
                    )
                agent_text = answer
            member_outputs.append(
                {
                    "name": "Workspace Agent",
                    "content": agent_text,
                    "phase": "workspace_guard",
                }
            )
            self._record_member_outputs(ctx, member_outputs)
            return RunResult(
                answer=answer,
                mode="workspace_guard",
                selected_agents=selected_agents,
                member_outputs=member_outputs,
                knowledge_hits=[],
                notes=notes,
                model_routes=model_routes,
                prefetch_info=prefetch_info,
                effective_agents=effective_agents,
            )
        except Exception as exc:
            failure_text = (
                "当前未能访问你的用户工作区，Workspace Agent / MCP 未成功执行。\n"
                "为避免越权暴露工程根目录，本次不会返回任何目录或文件内容。\n"
                f"- workspace_root: {ctx.workspace_root}\n"
                f"- reason: {exc}"
            )
            member_outputs.append(
                {
                    "name": "Workspace Agent",
                    "content": failure_text,
                    "phase": "workspace_guard",
                }
            )
            self._record_member_outputs(ctx, member_outputs)
            return RunResult(
                answer=failure_text,
                mode="workspace_guard",
                selected_agents=selected_agents,
                member_outputs=member_outputs,
                knowledge_hits=[],
                notes=[*notes, "Workspace Agent 未成功执行，已安全失败，不返回任何目录内容。"] ,
                model_routes=model_routes,
                prefetch_info=prefetch_info,
                effective_agents=effective_agents,
            )

    def build_team(
        self,
        ctx: RequestContext,
        *,
        healthy_aliases: set[str] | None = None,
    ) -> tuple[Team, list[dict], dict[str, str], list[dict]]:
        captured_hits: list[dict] = []
        effective_agents = self._effective_agents(ctx)
        agent_map = {item.agent_key: item for item in effective_agents}

        def search_project_knowledge(query: str, limit: int = 4) -> str:
            hits = self.database.search_knowledge(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                project_id=ctx.project_id,
                query=query,
                limit=limit,
            )
            captured_hits[:] = hits
            if not hits:
                return "当前用户和项目作用域下未找到可用知识。"
            lines = []
            for hit in hits:
                lines.append(
                    f"- 标题: {hit['title']} | 作用域: {hit['scope_type']}:{hit['scope_id']} | 摘要: {hit['snippet']}"
                )
            return "\n".join(lines)

        workspace_root = ctx.workspace_root
        workspace_tools = MCPTools(
            command=f"{sys.executable} -m app.mcp.user_workspace_server",
            transport="stdio",
            timeout_seconds=20,
            tool_name_prefix="workspace",
            env=build_workspace_mcp_env(self.settings, ctx),
        )
        orchestrate_route = self.model_router.resolve(
            TASK_ORCHESTRATE,
            preferred_aliases=self._preferred_aliases(
                agent_map.get("enterprise_orchestrator"), healthy_aliases
            ),
        )
        knowledge_route = self.model_router.resolve(
            TASK_KNOWLEDGE,
            preferred_aliases=self._preferred_aliases(agent_map.get("knowledge_agent"), healthy_aliases),
        )
        workspace_route = self.model_router.resolve(
            TASK_WORKSPACE,
            preferred_aliases=self._preferred_aliases(agent_map.get("workspace_agent"), healthy_aliases),
        )
        testing_route = self.model_router.resolve(
            TASK_TESTING,
            preferred_aliases=self._preferred_aliases(agent_map.get("test_agent"), healthy_aliases),
        )
        external_broker_route = self.model_router.resolve(
            TASK_EXTERNAL_BROKER,
            preferred_aliases=self._preferred_aliases(
                agent_map.get("external_agent_broker"), healthy_aliases
            ),
        )
        model_routes = {
            TASK_ORCHESTRATE: orchestrate_route.alias,
            TASK_KNOWLEDGE: knowledge_route.alias,
            TASK_WORKSPACE: workspace_route.alias,
            TASK_TESTING: testing_route.alias,
            TASK_EXTERNAL_BROKER: external_broker_route.alias,
        }

        def list_external_agents(
            category: str = "",
            capability: str = "",
            name_query: str = "",
            force_refresh: bool = False,
        ) -> str:
            snapshot = self.external_agent_broker.list_agents(
                ctx=ctx,
                force_refresh=force_refresh,
                category=category or None,
                capability=capability or None,
                name_query=name_query or None,
            )
            return self.external_agent_broker.format_agents_summary(snapshot)

        def delegate_to_external_agent(
            task: str,
            agent_id: str = "",
            category: str = "",
            capability: str = "",
            preferred_name: str = "",
            force_refresh: bool = False,
        ) -> str:
            result = self.external_agent_broker.invoke(
                ctx=ctx,
                message=task,
                agent_id=agent_id or None,
                category=category or None,
                capability=capability or None,
                preferred_name=preferred_name or None,
                force_refresh=force_refresh,
                metadata={"role": ctx.role, "display_name": ctx.display_name},
            )
            return self.external_agent_broker.format_invocation_result(result)

        members: list[Agent] = []
        enabled_descriptions: list[str] = []

        knowledge_cfg = agent_map.get("knowledge_agent")
        if knowledge_cfg and knowledge_cfg.included_in_team:
            members.append(
                Agent(
                    name="Knowledge Agent",
                    role="基于项目知识库和个人知识为请求补充上下文",
                    model=build_agno_model(self.settings, knowledge_route.alias),
                    tools=[search_project_knowledge],
                    markdown=True,
                    instructions=[
                        "只在当前认证用户允许的作用域内检索知识。",
                        "回答时必须说明命中的项目或个人作用域。",
                        "如果没有命中知识，明确说明不要编造。",
                        f"当前为统一模型网关路由，task_type=knowledge，alias={knowledge_route.alias}。",
                    ],
                    db=self.agno_db,
                    telemetry=self.settings.telemetry_enabled,
                )
            )
            enabled_descriptions.append("当请求需要项目背景时，先委托 Knowledge Agent。")

        workspace_cfg = agent_map.get("workspace_agent")
        if workspace_cfg and workspace_cfg.included_in_team:
            members.append(
                Agent(
                    name="Workspace Agent",
                    role="查看当前用户自己的文件空间，提取与任务相关的本地上下文",
                    model=build_agno_model(self.settings, workspace_route.alias),
                    tools=[workspace_tools],
                    markdown=True,
                    instructions=[
                        f"你只能访问当前用户 {ctx.user_id} 的工作区。",
                        "需要先列出文件，再读取最相关的文件。",
                        "如用户明确要求写入，可通过 MCP 保存文本文件，并说明写入结果。",
                        "不要假设存在某个文件，先确认再读取。",
                        f"当前为统一模型网关路由，task_type=workspace，alias={workspace_route.alias}。",
                    ],
                    db=self.agno_db,
                    telemetry=self.settings.telemetry_enabled,
                )
            )
            enabled_descriptions.append("当请求需要用户文件、草稿、笔记时，先委托 Workspace Agent。")

        test_cfg = agent_map.get("test_agent")
        if test_cfg and test_cfg.included_in_team:
            members.append(
                Agent(
                    name="Test Agent",
                    role="把知识上下文和用户文件上下文整理成测试建议、风险清单和验收点",
                    model=build_agno_model(self.settings, testing_route.alias),
                    skills=self.testing_skills,
                    markdown=True,
                    instructions=[
                        "输出优先面向企业内部 PoC：覆盖隔离、审计、MCP、知识过滤和回归验证。",
                        "如果需要补充外部发现、A2A 或跨边界验收项，把这些风险写进测试建议。",
                        "如果前面成员给出了来源信息，要保留引用脉络。",
                        "输出要可执行，不要只给空泛建议。",
                        f"当前为统一模型网关路由，task_type=testing，alias={testing_route.alias}。",
                    ],
                    db=self.agno_db,
                    telemetry=self.settings.telemetry_enabled,
                )
            )
            enabled_descriptions.append("当请求包含测试、验证、验收、风险时，把上下文继续交给 Test Agent。")

        broker_cfg = agent_map.get("external_agent_broker")
        if broker_cfg and broker_cfg.included_in_team:
            members.append(
                Agent(
                    name="External Agent Broker",
                    role="通过 MCP discovery 查找外部专业智能体，并通过 A2A 发起最小必要委托",
                    model=build_agno_model(self.settings, external_broker_route.alias),
                    skills=self.external_broker_skills,
                    tools=[list_external_agents, delegate_to_external_agent],
                    markdown=True,
                    instructions=[
                        "先做 external agent discovery，再决定是否需要 A2A 委托。",
                        "只把最小必要上下文交给外部 agent，避免泄露无关的用户文件内容。",
                        "优先按 category、capability 选择最匹配的 external agent。",
                        "返回结果时要说明选中的 agent、选择原因、A2A 结果和 caveats。",
                        f"当前为统一模型网关路由，task_type=external_broker，alias={external_broker_route.alias}。",
                    ],
                    db=self.agno_db,
                    telemetry=self.settings.telemetry_enabled,
                )
            )
            enabled_descriptions.append(
                "优先使用内部智能体，只有在确实需要外部专业能力时才委托 External Agent Broker。"
            )

        members.sort(
            key=lambda item: (
                -agent_map[
                    {
                        "Knowledge Agent": "knowledge_agent",
                        "Workspace Agent": "workspace_agent",
                        "Test Agent": "test_agent",
                        "External Agent Broker": "external_agent_broker",
                    }[item.name]
                ].priority,
                item.name,
            )
        )
        team = Team(
            id="enterprise_orchestrator",
            name="Enterprise Orchestrator",
            role="协调知识、用户空间和测试建议的企业主智能体",
            model=build_agno_model(self.settings, orchestrate_route.alias),
            members=members,
            mode=TeamMode.coordinate,
            markdown=True,
            db=self.agno_db,
            tools=self.orchestrator_skills.get_tools() if self.orchestrator_skills else None,
            additional_context=(
                self.orchestrator_skills.get_system_prompt_snippet()
                if self.orchestrator_skills
                else None
            ),
            show_members_responses=True,
            store_member_responses=True,
            instructions=[
                f"当前用户: {ctx.user_id} ({ctx.display_name})，角色: {ctx.role}。",
                f"当前租户: {ctx.tenant_id}，当前项目: {ctx.project_id}。",
                *enabled_descriptions,
                "当 External Agent Broker 返回结果后，要重新总结整合，不要把协议细节原样转发给用户。",
                "严禁跨项目、跨用户引用信息。",
                f"当前为统一模型网关路由，task_type=orchestrate，alias={orchestrate_route.alias}。",
            ],
            telemetry=self.settings.telemetry_enabled,
        )
        return team, captured_hits, model_routes, [item.as_dict() for item in effective_agents]

    def run(self, ctx: RequestContext, prompt: str, use_mock: bool | None = None) -> RunResult:
        access_decision = self._classify_workspace_access(prompt, healthy_aliases=None)
        guarded = self._run_workspace_guard(ctx, prompt, access_decision=access_decision)
        if guarded is not None:
            return guarded
        if use_mock is True:
            return self.run_mock(ctx, prompt)

        # Reuse the latest successful probe within the health TTL so a transient
        # re-check does not flip a live session back to mock mode.
        health = self.health_checker.probe()
        access_decision = self._classify_workspace_access(prompt, healthy_aliases=health.healthy_aliases)
        guarded = self._run_workspace_guard(ctx, prompt, access_decision=access_decision)
        if guarded is not None:
            return guarded
        if not health.live:
            if not self.settings.allow_mock_fallback:
                raise RuntimeError(health.reason)
            mock = self.run_mock(ctx, prompt)
            mock.notes.append(f"LiteLLM live 不可用，已回退到 mock: {health.reason}")
            return mock
        try:
            return self.run_agno(ctx, prompt, healthy_aliases=health.healthy_aliases)
        except Exception as exc:  # pragma: no cover - exercised manually
            if not self.settings.allow_mock_fallback:
                raise
            mock = self.run_mock(ctx, prompt)
            mock.notes.append(f"Live Agno + LiteLLM 调用失败，已自动切换到 mock 模式: {exc}")
            return mock

    def run_agno(
        self,
        ctx: RequestContext,
        prompt: str,
        *,
        healthy_aliases: set[str] | None = None,
    ) -> RunResult:
        effective_agents = self._effective_agents(ctx)
        prefetched_member_outputs, prefetched_agents, enriched_prompt, prefetch_info = (
            self._apply_external_prefetch_strategy(
                ctx,
                prompt,
                effective_agents=effective_agents,
            )
        )
        team, captured_hits, model_routes, effective_agent_payload = self.build_team(
            ctx, healthy_aliases=healthy_aliases
        )
        response = team.run(enriched_prompt, user_id=ctx.user_id, session_id=ctx.session_id)
        member_outputs: list[dict] = list(prefetched_member_outputs)
        selected_agents: list[str] = list(prefetched_agents)
        for item in getattr(response, "member_responses", []) or []:
            name = getattr(item, "agent_name", None) or getattr(item, "name", None) or "member"
            content = getattr(item, "content", None) or str(item)
            selected_agents.append(name)
            member_outputs.append({"name": name, "content": content, "phase": "team"})
        selected_agents = list(dict.fromkeys(selected_agents)) or ["Enterprise Orchestrator"]
        has_workspace_evidence = any(item["name"] == "Workspace Agent" for item in member_outputs)
        if not has_workspace_evidence and self._answer_looks_like_repo_listing(ctx, response.content):
            failure_text = (
                "检测到当前响应可能包含工程根目录或仓库结构信息，但本次并没有成功走 Workspace Agent / MCP。\n"
                "为避免权限越界，系统已拦截该结果，请改为通过 Workspace Agent 访问当前用户工作区。"
            )
            member_outputs = [
                {
                    "name": "Workspace Agent",
                    "content": failure_text,
                    "phase": "workspace_guard_block",
                }
            ]
            selected_agents = ["Workspace Agent"]
            self._record_member_outputs(ctx, member_outputs)
            return RunResult(
                answer=failure_text,
                mode="workspace_guard_blocked",
                selected_agents=selected_agents,
                member_outputs=member_outputs,
                knowledge_hits=captured_hits,
                notes=["已拦截疑似工程根目录越界响应。"],
                model_routes=model_routes,
                prefetch_info=prefetch_info,
                effective_agents=effective_agent_payload,
            )
        self._record_member_outputs(ctx, member_outputs)
        return RunResult(
            answer=response.content,
            mode="agno",
            selected_agents=selected_agents,
            member_outputs=member_outputs,
            knowledge_hits=captured_hits,
            notes=[],
            model_routes=model_routes,
            prefetch_info=prefetch_info,
            effective_agents=effective_agent_payload,
        )

    def _apply_external_prefetch_strategy(
        self,
        ctx: RequestContext,
        prompt: str,
        *,
        effective_agents: list[EffectiveAgentConfig],
    ) -> tuple[list[dict], list[str], str, dict]:
        mode = (self.settings.external_prefetch_mode or "off").strip().lower()
        mode = mode if mode in {"off", "hint", "prefetch"} else "off"
        broker_cfg = next(
            (item for item in effective_agents if item.agent_key == "external_agent_broker"),
            None,
        )
        info = {
            "enabled": bool(self.settings.external_prefetch_enabled),
            "mode": mode,
            "triggered": False,
            "category": None,
            "matched_keywords": [],
            "record_reason": bool(self.external_agent_broker.registry.config.prefetch.record_reason),
        }
        if (
            not self.settings.external_prefetch_enabled
            or mode == "off"
            or broker_cfg is None
            or not broker_cfg.enabled
            or not broker_cfg.allow_auto_route
        ):
            return [], [], prompt, info

        lowered = prompt.lower()
        matched_keywords: list[str] = []
        category: str | None = None
        for rule in self.external_agent_broker.registry.config.prefetch.rules:
            hits = [keyword for keyword in rule.keywords if keyword and keyword.lower() in lowered]
            if not hits:
                continue
            matched_keywords.extend(hits)
            if category is None and rule.category:
                category = rule.category
        if not matched_keywords:
            return [], [], prompt, info

        deduped_keywords = list(dict.fromkeys(matched_keywords))
        info.update(
            {
                "triggered": True,
                "category": category or "",
                "matched_keywords": deduped_keywords,
            }
        )
        if info["record_reason"]:
            info["reason"] = (
                f"matched_keywords={','.join(deduped_keywords)}"
                + (f"; category={category}" if category else "")
            )
        self.database.record_prefetch_triggered(ctx, payload=info)
        if mode == "hint":
            hint_text = (
                "系统检测到这次请求可能需要外部专业视角。"
                f" 如有必要，请考虑 External Agent Broker，参考 category={category or 'general'}，"
                f" matched_keywords={', '.join(deduped_keywords)}。"
            )
            return [], [], f"{prompt}\n\n[系统提示]\n{hint_text}", info
        try:
            result = self.external_agent_broker.invoke(
                ctx=ctx,
                message=prompt,
                category=category or None,
                metadata={"prefetch": True, "role": ctx.role, "matched_keywords": deduped_keywords},
            )
        except Exception as exc:
            content = f"External Agent Broker 预取失败: {exc}"
            return (
                [{"name": "External Agent Broker", "content": content, "phase": "prefetch"}],
                ["External Agent Broker"],
                prompt,
                {**info, "error": str(exc)},
            )
        broker_text = self.external_agent_broker.format_invocation_result(result)
        enriched_prompt = (
            f"{prompt}\n\n"
            f"以下是已经通过 External Agent Broker 获得的外部专业上下文，请整合后回答最终结论：\n"
            f"{broker_text}"
        )
        return (
            [{"name": "External Agent Broker", "content": broker_text, "phase": "prefetch"}],
            ["External Agent Broker"],
            enriched_prompt,
            info,
        )

    def run_mock(self, ctx: RequestContext, prompt: str) -> RunResult:
        effective_agents = [item.as_dict() for item in self._effective_agents(ctx)]
        hits = self.database.search_knowledge(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            project_id=ctx.project_id,
            query=prompt,
            limit=4,
        )
        external_summary = "当前 mock 模式未发现 external agents。"
        try:
            external_snapshot = self.external_agent_broker.list_agents(ctx=None)
            external_summary = self.external_agent_broker.format_agents_summary(external_snapshot)
        except Exception:
            pass
        files = list_files(ctx.workspace_root, limit=6)
        file_summaries: list[str] = []
        for file_meta in files[:2]:
            try:
                file_content = read_text_file(ctx.workspace_root, file_meta["path"], max_chars=240)
                preview = file_content["content"].replace("\n", " ")[:120]
                file_summaries.append(f"{file_meta['path']}: {preview}")
            except Exception:
                continue

        answer_parts = [
            f"当前为 `{ctx.user_id}` / `{ctx.project_id}` 的本地演示结果。",
            "",
            "知识命中:",
        ]
        if hits:
            for hit in hits:
                answer_parts.append(
                    f"- {hit['title']} ({hit['scope_type']}:{hit['scope_id']}): {hit['snippet']}"
                )
        else:
            answer_parts.append("- 当前作用域下没有命中知识。")

        answer_parts.append("")
        answer_parts.append("用户空间文件:")
        if file_summaries:
            for summary in file_summaries:
                answer_parts.append(f"- {summary}")
        else:
            answer_parts.append("- 当前用户空间没有可读取文件。")

        answer_parts.append("")
        answer_parts.append("建议:")
        answer_parts.extend(
            [
                "- 验证同一请求链路是否写入 trace_id、session_id、tenant_id 和 user_id。",
                "- 验证只命中当前项目知识库和当前用户个人文件，不能跨用户读取。",
                "- 验证 external agents 的 discovery、selection、A2A request/response 是否串到同一 trace_id。",
                "- 验证 MCP 文件列举、读取动作都有审计记录。",
                "- 验证主智能体在“知识检索 -> 用户文件 -> 测试建议”之间能够清晰分工。",
            ]
        )

        member_outputs = [
            {
                "name": "Knowledge Agent",
                "content": "\n".join(
                    [
                        f"- {hit['title']} ({hit['scope_type']}:{hit['scope_id']}): {hit['snippet']}"
                        for hit in hits
                    ]
                )
                or "未命中知识。",
            },
            {
                "name": "Workspace Agent",
                "content": "\n".join(f"- {summary}" for summary in file_summaries)
                or "未发现可读取文件。",
            },
            {
                "name": "External Agent Broker",
                "content": external_summary,
            },
            {
                "name": "Test Agent",
                "content": "\n".join(answer_parts[-5:]),
            },
        ]
        model_routes = {
            TASK_ORCHESTRATE: self.model_router.resolve(TASK_ORCHESTRATE).alias,
            TASK_KNOWLEDGE: self.model_router.resolve(TASK_KNOWLEDGE).alias,
            TASK_WORKSPACE: self.model_router.resolve(TASK_WORKSPACE).alias,
            TASK_TESTING: self.model_router.resolve(TASK_TESTING).alias,
            TASK_EXTERNAL_BROKER: self.model_router.resolve(TASK_EXTERNAL_BROKER).alias,
        }
        return RunResult(
            answer="\n".join(answer_parts),
            mode="mock",
            selected_agents=[
                "Knowledge Agent",
                "Workspace Agent",
                "External Agent Broker",
                "Test Agent",
            ],
            member_outputs=member_outputs,
            knowledge_hits=hits,
            notes=["当前未配置可用模型，返回的是本地 mock 演示结果。"],
            model_routes=model_routes,
            prefetch_info={
                "enabled": bool(self.settings.external_prefetch_enabled),
                "mode": (self.settings.external_prefetch_mode or "off").strip().lower(),
                "triggered": False,
                "category": None,
                "matched_keywords": [],
            },
            effective_agents=effective_agents,
        )

    def build_default_team(self, project_root) -> Team:
        demo_root = project_root / "data" / "workspaces" / "demo" / "alice"
        ctx = RequestContext(
            trace_id="trace_default",
            request_id="request_default",
            session_id="session_default",
            tenant_id=self.settings.default_tenant_id,
            user_id="alice",
            display_name="Alice Chen",
            role="manager",
            project_id=self.settings.default_project_id,
            workspace_root=demo_root,
        )
        team, _, _, _ = self.build_team(ctx)
        return team
