from __future__ import annotations

import sys
from dataclasses import dataclass, field

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.skills import LocalSkills, Skills
from agno.team import Team
from agno.team.mode import TeamMode
from agno.tools.mcp import MCPTools

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


@dataclass(slots=True)
class RunResult:
    answer: str
    mode: str
    selected_agents: list[str]
    member_outputs: list[dict]
    knowledge_hits: list[dict]
    notes: list[str]
    model_routes: dict[str, str] = field(default_factory=dict)


class OrchestratorRuntime:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        model_router: ModelRouter,
        health_checker: LiteLLMHealthChecker,
        external_agent_broker: ExternalAgentBroker,
    ) -> None:
        self.settings = settings
        self.database = database
        self.model_router = model_router
        self.health_checker = health_checker
        self.external_agent_broker = external_agent_broker
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

    def build_team(
        self,
        ctx: RequestContext,
        *,
        healthy_aliases: set[str] | None = None,
    ) -> tuple[Team, list[dict], dict[str, str]]:
        captured_hits: list[dict] = []

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
            env={
                "USER_WORKSPACE_ROOT": str(workspace_root),
                "MCP_AUDIT_DB": str(self.settings.resolved_db_file),
                "MCP_AUDIT_TRACE_ID": ctx.trace_id,
                "MCP_AUDIT_REQUEST_ID": ctx.request_id,
                "MCP_AUDIT_SESSION_ID": ctx.session_id,
                "MCP_AUDIT_TENANT_ID": ctx.tenant_id,
                "MCP_AUDIT_USER_ID": ctx.user_id,
            },
        )
        orchestrate_route = self.model_router.resolve(
            TASK_ORCHESTRATE, preferred_aliases=healthy_aliases
        )
        knowledge_route = self.model_router.resolve(
            TASK_KNOWLEDGE, preferred_aliases=healthy_aliases
        )
        workspace_route = self.model_router.resolve(
            TASK_WORKSPACE, preferred_aliases=healthy_aliases
        )
        testing_route = self.model_router.resolve(
            TASK_TESTING, preferred_aliases=healthy_aliases
        )
        external_broker_route = self.model_router.resolve(
            TASK_EXTERNAL_BROKER, preferred_aliases=healthy_aliases
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

        knowledge_agent = Agent(
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
        workspace_agent = Agent(
            name="Workspace Agent",
            role="查看当前用户自己的文件空间，提取与任务相关的本地上下文",
            model=build_agno_model(self.settings, workspace_route.alias),
            tools=[workspace_tools],
            markdown=True,
            instructions=[
                f"你只能访问当前用户 {ctx.user_id} 的工作区。",
                "需要先列出文件，再读取最相关的文件。",
                "不要假设存在某个文件，先确认再读取。",
                f"当前为统一模型网关路由，task_type=workspace，alias={workspace_route.alias}。",
            ],
            db=self.agno_db,
            telemetry=self.settings.telemetry_enabled,
        )
        test_agent = Agent(
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
        external_broker_agent = Agent(
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
        team = Team(
            id="enterprise_orchestrator",
            name="Enterprise Orchestrator",
            role="协调知识、用户空间和测试建议的企业主智能体",
            model=build_agno_model(self.settings, orchestrate_route.alias),
            members=[knowledge_agent, workspace_agent, test_agent, external_broker_agent],
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
                "当请求需要项目背景时，先委托 Knowledge Agent。",
                "当请求需要用户文件、草稿、笔记时，先委托 Workspace Agent。",
                "当请求包含测试、验证、验收、风险时，把上下文继续交给 Test Agent。",
                "优先使用内部智能体，只有在确实需要外部专业能力时才委托 External Agent Broker。",
                "当 External Agent Broker 返回结果后，要重新总结整合，不要把协议细节原样转发给用户。",
                "严禁跨项目、跨用户引用信息。",
                f"当前为统一模型网关路由，task_type=orchestrate，alias={orchestrate_route.alias}。",
            ],
            telemetry=self.settings.telemetry_enabled,
        )
        return team, captured_hits, model_routes

    def run(self, ctx: RequestContext, prompt: str, use_mock: bool | None = None) -> RunResult:
        if use_mock is True:
            return self.run_mock(ctx, prompt)

        # Reuse the latest successful probe within the health TTL so a transient
        # re-check does not flip a live session back to mock mode.
        health = self.health_checker.probe()
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
        prefetched_member_outputs, prefetched_agents, enriched_prompt = self._maybe_prefetch_external_context(
            ctx,
            prompt,
        )
        team, captured_hits, model_routes = self.build_team(
            ctx,
            healthy_aliases=healthy_aliases,
        )
        response = team.run(enriched_prompt, user_id=ctx.user_id, session_id=ctx.session_id)
        member_outputs: list[dict] = []
        selected_agents: list[str] = []
        for item in getattr(response, "member_responses", []) or []:
            name = getattr(item, "agent_name", None) or getattr(item, "name", None) or "member"
            content = getattr(item, "content", None) or str(item)
            selected_agents.append(name)
            member_outputs.append({"name": name, "content": content})
        existing_names = {item["name"] for item in member_outputs}
        for item in prefetched_member_outputs:
            if item["name"] not in existing_names:
                member_outputs.append(item)
        selected_agents = list(dict.fromkeys([*prefetched_agents, *selected_agents]))
        if not selected_agents:
            selected_agents = ["Enterprise Orchestrator"]
        return RunResult(
            answer=response.content,
            mode="agno",
            selected_agents=selected_agents,
            member_outputs=member_outputs,
            knowledge_hits=captured_hits,
            notes=[],
            model_routes=model_routes,
        )

    def _maybe_prefetch_external_context(
        self,
        ctx: RequestContext,
        prompt: str,
    ) -> tuple[list[dict], list[str], str]:
        category = self._infer_external_category(prompt)
        if category is None:
            return [], [], prompt
        try:
            result = self.external_agent_broker.invoke(
                ctx=ctx,
                message=prompt,
                category=category,
                metadata={"prefetch": True, "role": ctx.role},
            )
        except Exception as exc:
            content = f"External Agent Broker 预取失败: {exc}"
            return (
                [{"name": "External Agent Broker", "content": content}],
                ["External Agent Broker"],
                prompt,
            )
        broker_text = self.external_agent_broker.format_invocation_result(result)
        enriched_prompt = (
            f"{prompt}\n\n"
            f"以下是已经通过 External Agent Broker 获得的外部专业上下文，请整合后回答最终结论：\n"
            f"{broker_text}"
        )
        return (
            [{"name": "External Agent Broker", "content": broker_text}],
            ["External Agent Broker"],
            enriched_prompt,
        )

    def _infer_external_category(self, prompt: str) -> str | None:
        lowered = prompt.lower()
        has_external_intent = any(
            keyword in lowered
            for keyword in [
                "external",
                "a2a",
                "broker",
                "外部",
                "动态发现",
                "外部智能体",
                "external agent broker",
            ]
        )
        if not has_external_intent:
            return None
        if any(keyword in lowered for keyword in ["security", "安全", "审计", "隔离", "边界"]):
            return "security"
        if any(keyword in lowered for keyword in ["compliance", "合规", "制度", "审批", "验收"]):
            return "compliance"
        if any(keyword in lowered for keyword in ["analytics", "分析", "指标", "日志", "运营"]):
            return "analytics"
        return ""

    def run_mock(self, ctx: RequestContext, prompt: str) -> RunResult:
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
        team, _, _ = self.build_team(ctx)
        return team
