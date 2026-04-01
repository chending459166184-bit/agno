from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.skills import LocalSkills, Skills
from agno.team import Team
from agno.team.mode import TeamMode
from agno.tools.mcp import MCPTools

from app.agent_configs import AgentConfigService, EffectiveAgentConfig
from app.config import Settings
from app.context import RequestContext
from app.db import Database
from app.execution import ExecutionManager, ExecutionRequest
from app.external_agents import ExternalAgentBroker
import app.guard_response as guard_response
from app.model_gateway import LiteLLMHealthChecker, ModelRouter, build_agno_model
from app.model_gateway.task_types import (
    TASK_EXECUTION,
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


@dataclass(slots=True)
class TeamRoutingPlan:
    required_agents: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

class OrchestratorRuntime:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        model_router: ModelRouter,
        health_checker: LiteLLMHealthChecker,
        external_agent_broker: ExternalAgentBroker,
        agent_config_service: AgentConfigService,
        execution_manager: ExecutionManager,
    ) -> None:
        self.settings = settings
        self.database = database
        self.model_router = model_router
        self.health_checker = health_checker
        self.external_agent_broker = external_agent_broker
        self.agent_config_service = agent_config_service
        self.execution_manager = execution_manager
        self.agno_db = SqliteDb(db_file=str(settings.resolved_db_file))
        self.orchestrator_skills = self._load_skills("shared", "orchestrator")
        self.knowledge_skills = self._load_skills("knowledge")
        self.workspace_skills = self._load_skills("workspace")
        self.execution_skills = self._load_skills("execution")
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

    def _build_team_routing_plan(
        self,
        prompt: str,
        *,
        healthy_aliases: set[str] | None,
    ) -> TeamRoutingPlan:
        plan = TeamRoutingPlan()
        lowered = (prompt or "").lower()

        def add_requirement(agent_name: str, hint: str, note: str) -> None:
            if agent_name not in plan.required_agents:
                plan.required_agents.append(agent_name)
            if hint not in plan.hints:
                plan.hints.append(hint)
            if note not in plan.notes:
                plan.notes.append(note)

        workspace_decision = self._classify_workspace_access(
            prompt,
            healthy_aliases=healthy_aliases,
        )
        if workspace_decision.get("requires_workspace_access"):
            add_requirement(
                "Workspace Agent",
                (
                    "这次请求依赖当前用户工作区中的真实文件或目录证据。"
                    "请先委托 Workspace Agent，通过 Workspace MCP 获取结果，再基于该结果回答。"
                ),
                (
                    "routing_hint=Workspace Agent"
                    f" source={workspace_decision.get('source')}"
                    f" reason={workspace_decision.get('reason')}"
                ),
            )

        execution_decision = self._classify_execution_request(
            prompt,
            healthy_aliases=healthy_aliases,
        )
        if execution_decision.get("requires_execution"):
            add_requirement(
                "Execution Agent",
                (
                    "这次请求需要真实运行代码、脚本或受控命令。"
                    "请先委托 Execution Agent，并只基于 sandbox 结果回答。"
                ),
                (
                    "routing_hint=Execution Agent"
                    f" source={execution_decision.get('source')}"
                    f" reason={execution_decision.get('reason')}"
                ),
            )

        if any(
            keyword in lowered
            for keyword in [
                "知识库",
                "需求",
                "需求文档",
                "基线",
                "runbook",
                "文档",
                "项目背景",
                "内部资料",
                "baseline",
                "requirements",
            ]
        ):
            add_requirement(
                "Knowledge Agent",
                "这次请求依赖项目或个人知识上下文。请先委托 Knowledge Agent 检索证据，再继续回答。",
                "routing_hint=Knowledge Agent source=heuristic reason=knowledge_context_requested",
            )

        if any(
            keyword in lowered
            for keyword in [
                "测试",
                "验证",
                "验收",
                "回归",
                "风险",
                "test",
                "validate",
                "acceptance",
                "regression",
            ]
        ):
            add_requirement(
                "Test Agent",
                "这次请求包含测试或验收目标。请在拿到相关上下文后委托 Test Agent 给出测试建议。",
                "routing_hint=Test Agent source=heuristic reason=testing_language_detected",
            )

        return plan

    def _apply_team_routing_hints(
        self,
        prompt: str,
        routing_plan: TeamRoutingPlan,
        *,
        retry_missing_agents: list[str] | None = None,
    ) -> str:
        lines = [prompt]
        if routing_plan.hints:
            lines.extend(
                [
                    "",
                    "[系统调度提示]",
                    *[f"- {hint}" for hint in routing_plan.hints],
                ]
            )
        if retry_missing_agents:
            joined = ", ".join(retry_missing_agents)
            lines.extend(
                [
                    "",
                    "[系统重试约束]",
                    f"- 上一轮没有成功委托这些必需成员: {joined}",
                    f"- 本轮必须先调用: {joined}",
                    "- 在收到这些成员的结果之前，不要直接给最终用户答案。",
                ]
            )
        return "\n".join(lines).strip()

    def _ordered_required_agents(self, required_agents: list[str]) -> list[str]:
        order = [
            "Knowledge Agent",
            "Workspace Agent",
            "Execution Agent",
            "External Agent Broker",
            "Test Agent",
        ]
        ordered = [agent_name for agent_name in order if agent_name in required_agents]
        for agent_name in required_agents:
            if agent_name not in ordered:
                ordered.append(agent_name)
        return ordered

    def _summarize_plan(self, routing_plan: TeamRoutingPlan) -> str:
        required = ", ".join(routing_plan.required_agents) if routing_plan.required_agents else "none"
        hints = " | ".join(routing_plan.hints[:3]) if routing_plan.hints else "none"
        return f"required_agents={required}; hints={hints}"

    def _build_agent_task_prompt(
        self,
        agent_name: str,
        *,
        prompt: str,
        ctx: RequestContext,
        evidence_blocks: list[dict[str, Any]],
    ) -> str:
        evidence_lines: list[str] = []
        for block in evidence_blocks:
            evidence_lines.append(f"- {block['name']}: {block['content']}")
        evidence_text = "\n".join(evidence_lines) if evidence_lines else "- 当前还没有其他成员证据。"

        base = [
            f"用户原始请求: {prompt}",
            f"当前租户: {ctx.tenant_id}，当前用户: {ctx.user_id}，当前项目: {ctx.project_id}",
            "已有证据:",
            evidence_text,
        ]
        if agent_name == "Knowledge Agent":
            base.extend(
                [
                    "",
                    "你的任务：检索当前项目和当前用户作用域内与请求最相关的知识，并给出基于检索结果的简洁证据摘要。",
                    "禁止说“我会去查”或“我稍后查看”。请直接执行检索，并只基于检索结果回答。",
                ]
            )
        elif agent_name == "Workspace Agent":
            base.extend(
                [
                    "",
                    "你的任务：通过 Workspace MCP 访问当前用户工作区，直接回答这个文件/目录问题。",
                    "必要时先列出目录，再读取最相关文件。禁止说“我会查看”或“我先去看”，请直接执行工具并返回结果。",
                    "绝不能提及工程根目录、仓库目录或其他用户的空间。",
                ]
            )
        elif agent_name == "Execution Agent":
            base.extend(
                [
                    "",
                    "你的任务：如果请求需要运行代码、脚本或受控命令，请通过 execute_in_sandbox 真正执行，并仅基于执行结果回答。",
                    "禁止假装执行。禁止说“我会运行”，请直接执行或明确失败原因。",
                ]
            )
        elif agent_name == "External Agent Broker":
            base.extend(
                [
                    "",
                    "你的任务：在确实需要外部专业能力时，通过 discovery + A2A 获取最小必要的外部结果，并返回简洁摘要。",
                ]
            )
        elif agent_name == "Test Agent":
            base.extend(
                [
                    "",
                    "你的任务：基于已有证据输出测试建议、风险清单和验收点。",
                ]
            )
        return "\n".join(base).strip()

    def _tool_names_from_run_output(self, run_output: RunOutput | None) -> list[str]:
        names: list[str] = []
        if run_output is None:
            return names
        for tool in getattr(run_output, "tools", None) or []:
            tool_name = getattr(tool, "tool_name", None)
            if tool_name:
                names.append(str(tool_name))
        return names

    def _make_tool_execution(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        result: dict[str, Any] | str,
    ) -> ToolExecution:
        serialized_result = (
            result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        )
        return ToolExecution(
            tool_name=tool_name,
            tool_args=tool_args,
            result=serialized_result,
        )

    def _build_workspace_delegate_content(self, action: str, payload: dict[str, Any], ctx: RequestContext) -> str:
        if action == "write":
            return (
                "已通过 Workspace MCP 写入当前用户工作区。\n"
                f"- root: {payload.get('root', ctx.workspace_root)}\n"
                f"- path: {payload.get('path')}\n"
                f"- size: {payload.get('size')}"
            )
        if action == "read":
            return (
                "已通过 Workspace MCP 读取当前用户工作区文件。\n"
                f"- root: {payload.get('root', ctx.workspace_root)}\n"
                f"- path: {payload.get('path')}\n"
                f"- content:\n{payload.get('content', '')}"
            )
        files = payload.get("files") or []
        lines = [
            "已通过 Workspace MCP 列出当前用户工作区。",
            f"- root: {payload.get('root', ctx.workspace_root)}",
            "- files:",
        ]
        if not files:
            lines.append("  - 当前没有可见文件。")
        else:
            for item in files:
                lines.append(f"  - {item.get('path')}")
        return "\n".join(lines)

    def _run_workspace_delegate(
        self,
        ctx: RequestContext,
        prompt: str,
        *,
        healthy_aliases: set[str] | None,
    ) -> RunOutput:
        decision = self._classify_workspace_access(prompt, healthy_aliases=healthy_aliases)
        action = str(decision.get("action") or "list")
        if action == "write":
            path = str(decision.get("path") or "").strip()
            content = str(decision.get("content") or "").strip()
            if not path or not content:
                raise ValueError("当前无法安全解析写入路径或内容，请明确给出相对路径和内容。")
            tool_name = "workspace_save_text_file"
            tool_args = {"path": path, "content": content, "overwrite": True}
            payload = call_workspace_mcp_tool(self.settings, ctx, tool_name, tool_args)
        elif action == "read":
            path = str(decision.get("path") or "").strip()
            if not path:
                raise ValueError("当前无法安全解析要读取的相对路径，请明确指定文件名。")
            tool_name = "workspace_read_text_file"
            tool_args = {"path": path, "max_chars": 6000}
            payload = call_workspace_mcp_tool(self.settings, ctx, tool_name, tool_args)
        else:
            action = "list"
            tool_name = "workspace_list_files"
            tool_args = {"prefix": "", "limit": 50}
            payload = call_workspace_mcp_tool(self.settings, ctx, tool_name, tool_args)
        safe_payload = self._sanitize_workspace_guard_payload(action, payload, ctx)
        return RunOutput(
            agent_name="Workspace Agent",
            content=self._build_workspace_delegate_content(action, safe_payload, ctx),
            tools=[self._make_tool_execution(tool_name=tool_name, tool_args=tool_args, result=safe_payload)],
            metadata={
                "delegate_mode": "explicit_runtime_executor",
                "action": action,
                "safe_payload": safe_payload,
            },
        )

    def _run_knowledge_delegate(self, ctx: RequestContext, prompt: str) -> RunOutput:
        hits = self.database.search_knowledge(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            project_id=ctx.project_id,
            query=prompt,
            limit=4,
        )
        if hits:
            lines = []
            for hit in hits:
                lines.append(
                    f"- 标题: {hit['title']} | 作用域: {hit['scope_type']}:{hit['scope_id']} | 摘要: {hit['snippet']}"
                )
            content = "已通过 Knowledge Agent 检索到以下作用域内证据：\n" + "\n".join(lines)
        else:
            content = "已通过 Knowledge Agent 完成检索，但当前用户和项目作用域下未命中相关知识。"
        return RunOutput(
            agent_name="Knowledge Agent",
            content=content,
            tools=[
                self._make_tool_execution(
                    tool_name="search_project_knowledge",
                    tool_args={"query": prompt, "limit": 4},
                    result={"hits": hits},
                )
            ],
            metadata={
                "delegate_mode": "explicit_runtime_executor",
                "knowledge_hits": hits,
            },
        )

    def _run_execution_delegate(
        self,
        ctx: RequestContext,
        prompt: str,
        *,
        healthy_aliases: set[str] | None,
    ) -> RunOutput:
        decision = self._classify_execution_request(prompt, healthy_aliases=healthy_aliases)
        if not decision.get("requires_execution"):
            raise ValueError("当前无法安全解析要执行的 Python 入口、脚本内容或受控命令。")
        request = ExecutionRequest(
            project_id=ctx.project_id,
            session_id=ctx.session_id,
            language=str(decision.get("language") or "python"),
            command=decision.get("command"),
            entrypoint=decision.get("entrypoint"),
            files=list(decision.get("files") or []),
            timeout_seconds=int(decision.get("timeout_seconds") or self.settings.exec_default_timeout_seconds),
            writeback=False,
        )
        result = self.execution_manager.run(ctx, request)
        lines = [
            "已通过 Execution Agent 在独立 sandbox 中执行请求。",
            f"- job_id: {result.job.job_id}",
            f"- status: {result.job.status}",
            f"- sandbox_mode: {result.job.sandbox_mode}",
            f"- command: {result.job.command}",
            f"- workspace_root: {ctx.workspace_root}",
            f"- writeback: {result.job.writeback_enabled}",
            f"- network_enabled: {result.job.network_enabled}",
        ]
        if result.stdout:
            lines.append("- stdout:")
            lines.append(result.stdout)
        if result.stderr:
            lines.append("- stderr:")
            lines.append(result.stderr)
        if result.artifacts:
            lines.append("- artifacts:")
            for artifact in result.artifacts:
                lines.append(f"  - {artifact.relative_path} ({artifact.size_bytes} bytes)")
        return RunOutput(
            agent_name="Execution Agent",
            content="\n".join(lines),
            tools=[
                self._make_tool_execution(
                    tool_name="execute_in_sandbox",
                    tool_args={
                        "command": request.command,
                        "entrypoint": request.entrypoint,
                        "timeout_seconds": request.timeout_seconds,
                        "writeback": request.writeback,
                    },
                    result={
                        "job_id": result.job.job_id,
                        "status": result.job.status,
                        "sandbox_mode": result.job.sandbox_mode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "artifacts": [item.relative_path for item in result.artifacts],
                    },
                )
            ],
            metadata={"delegate_mode": "explicit_runtime_executor"},
        )

    def _run_external_delegate(self, ctx: RequestContext, prompt: str) -> RunOutput:
        result = self.external_agent_broker.invoke(
            ctx=ctx,
            message=prompt,
            metadata={"role": ctx.role, "display_name": ctx.display_name},
        )
        content = self.external_agent_broker.format_invocation_result(result)
        return RunOutput(
            agent_name="External Agent Broker",
            content=content,
            tools=[
                self._make_tool_execution(
                    tool_name="delegate_to_external_agent",
                    tool_args={"message": prompt, "agent_id": result.selected_agent.agent_id},
                    result=result.model_dump(),
                )
            ],
            metadata={"delegate_mode": "explicit_runtime_executor"},
        )

    def _run_explicit_delegate(
        self,
        agent_name: str,
        *,
        prompt: str,
        ctx: RequestContext,
        healthy_aliases: set[str] | None,
    ) -> RunOutput | None:
        if agent_name == "Workspace Agent":
            return self._run_workspace_delegate(ctx, prompt, healthy_aliases=healthy_aliases)
        if agent_name == "Knowledge Agent":
            return self._run_knowledge_delegate(ctx, prompt)
        if agent_name == "Execution Agent":
            return self._run_execution_delegate(ctx, prompt, healthy_aliases=healthy_aliases)
        if agent_name == "External Agent Broker":
            return self._run_external_delegate(ctx, prompt)
        return None

    def _agent_has_required_evidence(self, agent_name: str, run_output: RunOutput | None) -> bool:
        if run_output is None:
            return False
        content = str(getattr(run_output, "content", "") or "").strip()
        tool_names = self._tool_names_from_run_output(run_output)
        if agent_name == "Workspace Agent":
            return any(name.startswith("workspace") for name in tool_names)
        if agent_name == "Knowledge Agent":
            return "search_project_knowledge" in tool_names
        if agent_name == "Execution Agent":
            return "execute_in_sandbox" in tool_names
        if agent_name == "External Agent Broker":
            return any(name in {"list_external_agents", "delegate_to_external_agent"} for name in tool_names)
        return bool(content)

    def _build_gate_failure(
        self,
        agent_name: str,
        *,
        delegated_outputs: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        if agent_name == "Workspace Agent":
            text = (
                "当前问题需要先通过 Workspace Agent 访问当前用户工作区，但本轮没有拿到真实的 Workspace MCP 结果。\n"
                "系统不会基于猜测返回文件或目录内容，请稍后重试。"
            )
        elif agent_name == "Execution Agent":
            text = (
                "当前问题需要先通过 Execution Agent 在 sandbox 中执行，但本轮没有拿到真实的执行结果。\n"
                "系统不会伪造运行输出，请稍后重试。"
            )
        elif agent_name == "Knowledge Agent":
            text = (
                "当前问题需要先通过 Knowledge Agent 检索项目或个人知识，但本轮没有拿到有效检索结果。\n"
                "系统不会凭空补全文档内容，请稍后重试。"
            )
        else:
            text = (
                f"当前问题需要先拿到 {agent_name} 的结果，但本轮没有成功获取对应证据。\n"
                "系统不会在缺少证据时直接给最终答案，请稍后重试。"
            )
        outputs = list(delegated_outputs)
        outputs.append(
            {
                "name": "Enterprise Orchestrator",
                "phase": "gate_block",
                "content": text,
            }
        )
        return text, outputs

    def _build_synthesizer_prompt(
        self,
        *,
        prompt: str,
        ctx: RequestContext,
        delegated_outputs: list[dict[str, Any]],
    ) -> str:
        evidence_payload = [
            {
                "name": item["name"],
                "phase": item.get("phase", ""),
                "content": item["content"],
            }
            for item in delegated_outputs
            if item.get("phase") in {"prefetch", "delegate"}
        ]
        return (
            "请基于下面已经拿到的成员证据，给用户最终答复。\n"
            "你不能说“我会去查看”或“我先去处理”，因为这一步之前该执行的成员已经执行完了。\n"
            "你只能总结现有证据，不能新增不存在的文件、目录、执行结果或知识。\n\n"
            f"用户原始请求: {prompt}\n"
            f"上下文: tenant={ctx.tenant_id}, user={ctx.user_id}, project={ctx.project_id}\n"
            f"成员证据(JSON): {json.dumps(evidence_payload, ensure_ascii=False)}"
        )

    def _run_delegate_agent(
        self,
        agent: Agent,
        *,
        agent_name: str,
        prompt: str,
        ctx: RequestContext,
        evidence_blocks: list[dict[str, Any]],
        healthy_aliases: set[str] | None = None,
    ) -> RunOutput:
        explicit = self._run_explicit_delegate(
            agent_name,
            prompt=prompt,
            ctx=ctx,
            healthy_aliases=healthy_aliases,
        )
        if explicit is not None:
            return explicit
        task_prompt = self._build_agent_task_prompt(
            agent_name,
            prompt=prompt,
            ctx=ctx,
            evidence_blocks=evidence_blocks,
        )
        return agent.run(
            task_prompt,
            user_id=ctx.user_id,
            session_id=f"{ctx.session_id}:{agent_name.lower().replace(' ', '_')}",
        )

    def _detect_workspace_guard(self, prompt: str) -> dict | None:
        lowered = (prompt or "").lower()
        path_match = re.search(
            r"([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+|[A-Za-z0-9._-]+\.[A-Za-z0-9._-]+)",
            prompt,
        )
        guard_patterns = [
            r"(当前|我|我的).{0,4}(目录|工作区|workspace|空间).{0,8}(有哪些|有什么|列表|列出|查看|内容|list|show)",
            r"(当前|我|我的).{0,4}文件.{0,8}(有哪些|有什么|内容|list|show)",
            r"(目录|工作区|workspace|空间|current directory|my directory).{0,8}(有哪些|有什么|列表|列出|查看|内容|list|show)",
            r"(文件|目录|工作区|workspace|空间|folder|directory).{0,6}(有哪些|有什么|列表|列出|内容|list|show)",
            r"(读取|读|打开|查看|read|open).{0,8}(文件|目录|workspace|file|directory)",
            r"(写入|保存|写|save|write).{0,8}(文件|目录|工作区|workspace|file|directory)",
        ]
        direct_path_action = bool(
            path_match
            and any(
                keyword in lowered
                for keyword in ["读取", "读", "打开", "查看", "read", "open", "写入", "保存", "写", "save", "write"]
            )
        )
        if not direct_path_action and not any(
            re.search(pattern, lowered, re.IGNORECASE) for pattern in guard_patterns
        ):
            return None

        action = "list"
        if any(keyword in lowered for keyword in ["写入", "保存", "save", "write"]):
            action = "write"
        elif any(keyword in lowered for keyword in ["读取", "查看文件", "读文件", "read", "open"]):
            action = "read"
        elif any(keyword in lowered for keyword in ["目录", "文件", "list", "有哪些"]):
            action = "list"

        path = path_match.group(1) if path_match else None
        content = None
        content_match = re.search(r"(?:内容|content)\s*[:：]\s*(.+)$", prompt, re.IGNORECASE | re.DOTALL)
        if content_match:
            content = content_match.group(1).strip()
        return {"action": action, "path": path, "content": content}

    def _extract_code_block(self, prompt: str) -> tuple[str | None, str | None]:
        match = re.search(r"```(?P<lang>[a-zA-Z0-9_-]+)?\s*\n(?P<code>.*?)```", prompt, re.DOTALL)
        if not match:
            return None, None
        language = (match.group("lang") or "").strip().lower() or None
        code = (match.group("code") or "").strip()
        return language, code or None

    def _detect_execution_guard(self, prompt: str) -> dict | None:
        lowered = (prompt or "").lower()
        code_lang, code_block = self._extract_code_block(prompt)
        command_match = re.search(r"(?m)^\s*(python3?|pytest)\b[^\n]*$", prompt)
        path_match = re.search(r"([A-Za-z0-9._/-]+\.(?:py|sh|bash))", prompt)
        has_run_intent = any(
            phrase in lowered
            for phrase in [
                "运行",
                "执行",
                "跑一下",
                "测试一下",
                "验证一下",
                "run ",
                "execute",
                "pytest",
                "python ",
                "python3 ",
            ]
        )
        has_code_structure = bool(
            code_block
            or re.search(r"\b(print\(|import |def |class |assert |pytest\b|python3?\b)", prompt)
        )
        if not (command_match or (has_run_intent and (has_code_structure or path_match))):
            return None

        files: list[dict] = []
        language = "python"
        command = None
        entrypoint = None
        if command_match:
            command = command_match.group(0).strip()
        elif code_block:
            if code_lang in {"python", "py", None}:
                files = [{"path": "inline_task.py", "content": code_block}]
                command = "python inline_task.py"
            elif code_lang in {"bash", "sh", "shell"}:
                first_line = next((line.strip() for line in code_block.splitlines() if line.strip()), "")
                command = first_line or None
        elif path_match:
            entrypoint = path_match.group(1)
            if entrypoint.endswith(".py"):
                command = f"python {entrypoint}"

        if not command and not entrypoint and not files:
            return None
        return {
            "requires_execution": True,
            "language": language,
            "command": command,
            "entrypoint": entrypoint,
            "files": files,
            "timeout_seconds": self.settings.exec_default_timeout_seconds,
            "source": "heuristic",
            "reason": "matched_execution_intent",
            "confidence": 1.0 if files or command_match else 0.8,
        }

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

    def _classify_execution_request(
        self,
        prompt: str,
        *,
        healthy_aliases: set[str] | None,
    ) -> dict:
        heuristic = self._detect_execution_guard(prompt)
        if heuristic is not None:
            return heuristic
        workspace_signal = self._detect_workspace_guard(prompt)
        if workspace_signal is not None:
            return {
                "requires_execution": False,
                "language": None,
                "command": None,
                "entrypoint": None,
                "files": [],
                "timeout_seconds": None,
                "source": "workspace_guard_preempted",
                "reason": "workspace_access_detected_before_execution_classifier",
                "confidence": 0.0,
            }
        if not healthy_aliases:
            return {
                "requires_execution": False,
                "language": None,
                "command": None,
                "entrypoint": None,
                "files": [],
                "timeout_seconds": None,
                "source": "no_live_alias",
                "reason": "classifier_skipped_without_live_alias",
                "confidence": 0.0,
            }
        route = self.model_router.resolve(TASK_ORCHESTRATE, preferred_aliases=healthy_aliases)
        headers = {"Authorization": f"Bearer {self.settings.litellm_master_key}"}
        system_prompt = (
            "你是一个执行安全判定器。"
            "判断用户请求是否必须进入独立 sandbox 执行。"
            "只返回 JSON。"
            "JSON 结构必须是"
            '{"requires_execution": boolean, "language": "python|none", "command": string|null, '
            '"entrypoint": string|null, "reason": string, "confidence": number}.'
            "凡是运行代码、执行命令、pytest、验证脚本输出、执行文件，都应判为 true。"
            "如果需要执行但没有明确 command，可返回 entrypoint 或 command=null。"
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
                        "max_tokens": 220,
                    },
                )
                response.raise_for_status()
            text = self._extract_completion_text(response.json()).strip()
            parsed = json.loads(text)
            entrypoint = parsed.get("entrypoint")
            if entrypoint:
                entrypoint = str(entrypoint).strip()
            return {
                "requires_execution": bool(parsed.get("requires_execution")),
                "language": "python" if str(parsed.get("language") or "python").lower() != "none" else None,
                "command": parsed.get("command"),
                "entrypoint": entrypoint,
                "files": [],
                "timeout_seconds": self.settings.exec_default_timeout_seconds,
                "source": "classifier",
                "reason": str(parsed.get("reason") or "classifier_decision"),
                "confidence": float(parsed.get("confidence") or 0.0),
            }
        except Exception as exc:
            return {
                "requires_execution": False,
                "language": None,
                "command": None,
                "entrypoint": None,
                "files": [],
                "timeout_seconds": None,
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
            TASK_EXECUTION: self.model_router.resolve(TASK_EXECUTION).alias,
            TASK_EXTERNAL_BROKER: self.model_router.resolve(TASK_EXTERNAL_BROKER).alias,
        }

    def _sanitize_workspace_guard_payload(self, action: str, payload: dict, ctx: RequestContext) -> dict:
        root = str(payload.get("root") or ctx.workspace_root)
        if action == "write":
            return {
                "root": root,
                "path": str(payload.get("path") or ""),
                "size": int(payload.get("size") or 0),
            }
        if action == "read":
            return {
                "root": root,
                "path": str(payload.get("path") or ""),
                "content": str(payload.get("content") or ""),
                "truncated": bool(payload.get("truncated")),
            }
        files = []
        for item in payload.get("files") or []:
            path = str(item.get("path") or "").strip()
            if path:
                files.append({"path": path})
        return {
            "root": root,
            "files": files,
        }

    def _summarize_workspace_guard_data(self, action: str, payload: dict) -> str:
        root = payload.get("root")
        if action == "write":
            return (
                f"action=write | root={root} | path={payload.get('path')} | "
                f"size={payload.get('size')}"
            )
        if action == "read":
            preview = str(payload.get("content") or "").replace("\n", " ")[:120]
            return (
                f"action=read | root={root} | path={payload.get('path')} | "
                f"truncated={payload.get('truncated')} | preview={preview}"
            )
        files = [str(item.get("path") or "") for item in payload.get("files") or [] if item.get("path")]
        head = ", ".join(files[:6]) if files else "none"
        return f"action=list | root={root} | file_count={len(files)} | files={head}"

    def _build_workspace_guard_fallback_answer(self, action: str, payload: dict, ctx: RequestContext) -> str:
        if action == "write":
            return (
                "Workspace Agent 已通过 MCP 写入当前用户工作区。\n"
                f"- root: {payload.get('root', ctx.workspace_root)}\n"
                f"- path: {payload.get('path')}\n"
                f"- size: {payload.get('size')}"
            )
        if action == "read":
            return (
                "Workspace Agent 已通过 MCP 读取当前用户工作区文件。\n"
                f"- root: {ctx.workspace_root}\n"
                f"- path: {payload.get('path')}\n"
                f"- content:\n{payload.get('content', '')}"
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
            return "\n".join(lines)
        return (
            "Workspace Agent 已通过 MCP 检查当前用户工作区，但没有发现可见文件。\n"
            f"- root: {payload.get('root', ctx.workspace_root)}"
        )

    def _run_execution_guard(
        self,
        ctx: RequestContext,
        prompt: str,
        *,
        execution_decision: dict | None = None,
    ) -> RunResult | None:
        decision = execution_decision or {}
        if not decision.get("requires_execution"):
            return None

        effective_agents = [item.as_dict() for item in self._effective_agents(ctx)]
        selected_agents = ["Execution Agent"]
        notes = [
            "命中执行类高风险请求，已启用 execution guard。",
            "这次请求不会在普通 chat 环境执行，而是进入独立 sandbox。",
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
            request = ExecutionRequest(
                project_id=ctx.project_id,
                session_id=ctx.session_id,
                language=str(decision.get("language") or "python"),
                command=decision.get("command"),
                entrypoint=decision.get("entrypoint"),
                files=list(decision.get("files") or []),
                timeout_seconds=int(decision.get("timeout_seconds") or self.settings.exec_default_timeout_seconds),
                writeback=False,
            )
            result = self.execution_manager.run(ctx, request)
            lines = [
                "Execution Agent 已在独立 sandbox 中执行请求。",
                f"- job_id: {result.job.job_id}",
                f"- status: {result.job.status}",
                f"- sandbox_mode: {result.job.sandbox_mode}",
                f"- command: {result.job.command}",
                f"- workspace_root: {ctx.workspace_root}",
                f"- writeback: {result.job.writeback_enabled}",
                f"- network_enabled: {result.job.network_enabled}",
            ]
            if result.stdout:
                lines.append("- stdout:")
                lines.append(result.stdout)
            if result.stderr:
                lines.append("- stderr:")
                lines.append(result.stderr)
            if result.artifacts:
                lines.append("- artifacts:")
                for artifact in result.artifacts:
                    lines.append(f"  - {artifact.relative_path} ({artifact.size_bytes} bytes)")
            answer = "\n".join(lines)
            member_outputs = [
                {
                    "name": "Execution Agent",
                    "content": answer,
                    "phase": "execution_guard",
                }
            ]
            self._record_member_outputs(ctx, member_outputs)
            return RunResult(
                answer=answer,
                mode="execution_guard",
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
                "当前未能安全完成代码执行，请改为提供更明确的 Python 入口、脚本内容或受控命令。\n"
                "系统已阻止在普通 chat 环境中直接执行，以避免越过 sandbox。\n"
                f"- reason: {exc}"
            )
            member_outputs = [
                {
                    "name": "Execution Agent",
                    "content": failure_text,
                    "phase": "execution_guard",
                }
            ]
            self._record_member_outputs(ctx, member_outputs)
            return RunResult(
                answer=failure_text,
                mode="execution_guard",
                selected_agents=selected_agents,
                member_outputs=member_outputs,
                knowledge_hits=[],
                notes=[*notes, "Execution Agent 未能完成 sandbox 执行，已安全失败。"],
                model_routes=model_routes,
                prefetch_info=prefetch_info,
                effective_agents=effective_agents,
            )

    def _run_workspace_guard(
        self,
        ctx: RequestContext,
        prompt: str,
        *,
        access_decision: dict | None = None,
        healthy_aliases: set[str] | None = None,
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
            elif action == "read":
                if not decision.get("path"):
                    raise ValueError("当前无法安全解析要读取的相对路径，请明确指定文件名。")
                payload = call_workspace_mcp_tool(
                    self.settings,
                    ctx,
                    "workspace_read_text_file",
                    {"path": decision["path"], "max_chars": 6000},
                )
            else:
                payload = call_workspace_mcp_tool(
                    self.settings,
                    ctx,
                    "workspace_list_files",
                    {"prefix": "", "limit": 50},
                )
            safe_payload = self._sanitize_workspace_guard_payload(action, payload, ctx)
            self.database.record_workspace_guard_data_captured(
                ctx,
                payload={
                    "action": action,
                    "source": decision.get("source"),
                    "reason": decision.get("reason"),
                    "safe_payload": safe_payload,
                },
            )
            data_summary = self._summarize_workspace_guard_data(action, safe_payload)
            member_outputs.append(
                {
                    "name": "Workspace Agent",
                    "content": data_summary,
                    "phase": "workspace_guard_data",
                }
            )
            fallback_answer = self._build_workspace_guard_fallback_answer(action, safe_payload, ctx)
            self.database.record_workspace_guard_compose_started(
                ctx,
                payload={"action": action, "source": decision.get("source")},
            )
            compose_input = guard_response.WorkspaceGuardComposeInput(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                project_id=ctx.project_id,
                action=action,
                workspace_root=str(ctx.workspace_root),
                source=str(decision.get("source") or "unknown"),
                reason=str(decision.get("reason") or "workspace_guard"),
                payload=safe_payload,
            )
            try:
                answer = guard_response.compose_workspace_guard_answer(
                    self.settings,
                    self.model_router,
                    compose_input,
                    healthy_aliases=healthy_aliases,
                )
                member_outputs.append(
                    {
                        "name": "Workspace Guard Composer",
                        "content": answer,
                        "phase": "workspace_guard_compose",
                    }
                )
                self.database.record_workspace_guard_compose_succeeded(
                    ctx,
                    payload={"action": action, "answer_excerpt": answer[:240]},
                )
                notes.append("composer success")
            except Exception as exc:
                member_outputs.append(
                    {
                        "name": "Workspace Guard Composer",
                        "content": f"compose failed: {exc}",
                        "phase": "workspace_guard_compose_failed",
                    }
                )
                member_outputs.append(
                    {
                        "name": "Workspace Agent",
                        "content": fallback_answer,
                        "phase": "workspace_guard_fallback",
                    }
                )
                self.database.record_workspace_guard_compose_failed(
                    ctx,
                    payload={"action": action, "error": str(exc)},
                )
                notes.append("composer failed, fallback to template")
                answer = fallback_answer
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
        execution_route = self.model_router.resolve(
            TASK_EXECUTION,
            preferred_aliases=self._preferred_aliases(agent_map.get("execution_agent"), healthy_aliases),
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
            TASK_EXECUTION: execution_route.alias,
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

        def execute_in_sandbox(
            command: str = "",
            code: str = "",
            path: str = "",
            timeout_seconds: int = 30,
            writeback: bool = False,
        ) -> str:
            files = [{"path": "inline_task.py", "content": code}] if code.strip() else []
            result = self.execution_manager.run(
                ctx,
                ExecutionRequest(
                    project_id=ctx.project_id,
                    session_id=ctx.session_id,
                    language="python",
                    command=command or None,
                    entrypoint=path or None,
                    files=files,
                    timeout_seconds=timeout_seconds,
                    writeback=writeback,
                ),
            )
            lines = [
                f"job_id={result.job.job_id}",
                f"status={result.job.status}",
                f"sandbox_mode={result.job.sandbox_mode}",
                f"command={result.job.command}",
            ]
            if result.stdout:
                lines.append(f"stdout:\n{result.stdout}")
            if result.stderr:
                lines.append(f"stderr:\n{result.stderr}")
            if result.artifacts:
                lines.append("artifacts:")
                lines.extend(f"- {item.relative_path}" for item in result.artifacts)
            return "\n".join(lines)

        members: list[Agent] = []
        enabled_descriptions: list[str] = []

        knowledge_cfg = agent_map.get("knowledge_agent")
        if knowledge_cfg and knowledge_cfg.included_in_team:
            members.append(
                Agent(
                    name="Knowledge Agent",
                    role="基于项目知识库和个人知识为请求补充上下文",
                    model=build_agno_model(self.settings, knowledge_route.alias),
                    skills=self.knowledge_skills,
                    tools=[search_project_knowledge],
                    tool_choice="required",
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
                    skills=self.workspace_skills,
                    tools=[workspace_tools],
                    tool_choice="required",
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

        execution_cfg = agent_map.get("execution_agent")
        if execution_cfg and execution_cfg.included_in_team:
            members.append(
                Agent(
                    name="Execution Agent",
                    role="在独立 sandbox 中执行 Python 代码和受控命令，返回日志与产物摘要",
                    model=build_agno_model(self.settings, execution_route.alias),
                    skills=self.execution_skills,
                    tools=[execute_in_sandbox],
                    tool_choice="required",
                    markdown=True,
                    instructions=[
                        "只有在确实需要运行代码、验证脚本结果、执行 pytest 或受控命令时才调用工具。",
                        "绝不能在普通对话环境直接假装执行结果，必须通过 execute_in_sandbox。",
                        "执行环境来自独立 sandbox job root，不共享普通 chat cwd。",
                        "默认网络关闭、默认不写回 workspace；只有用户明确允许时才请求 writeback。",
                        f"当前为统一模型网关路由，task_type=execution，alias={execution_route.alias}。",
                    ],
                    db=self.agno_db,
                    telemetry=self.settings.telemetry_enabled,
                )
            )
            enabled_descriptions.append("凡是需要运行代码、执行命令、验证脚本结果时，必须委托 Execution Agent。")

        broker_cfg = agent_map.get("external_agent_broker")
        if broker_cfg and broker_cfg.included_in_team:
            members.append(
                Agent(
                    name="External Agent Broker",
                    role="通过 MCP discovery 查找外部专业智能体，并通过 A2A 发起最小必要委托",
                    model=build_agno_model(self.settings, external_broker_route.alias),
                    skills=self.external_broker_skills,
                    tools=[list_external_agents, delegate_to_external_agent],
                    tool_choice="required",
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
                        "Execution Agent": "execution_agent",
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
                "你自己不应假装拥有 Workspace MCP、知识检索或 sandbox 执行的真实结果。",
                "凡是问题依赖真实文件、目录、知识检索或执行日志时，必须先调用对应成员，再整合它们的证据。",
                "如果用户只是换了一种说法，但本质仍然是文件/知识/执行问题，也要识别并委托正确成员。",
                "凡是需要运行代码、执行命令、测试脚本、生成并验证程序结果时，必须委托 Execution Agent。",
                "当 External Agent Broker 返回结果后，要重新总结整合，不要把协议细节原样转发给用户。",
                "严禁跨项目、跨用户引用信息。",
                f"当前为统一模型网关路由，task_type=orchestrate，alias={orchestrate_route.alias}。",
            ],
            telemetry=self.settings.telemetry_enabled,
        )
        return team, captured_hits, model_routes, [item.as_dict() for item in effective_agents]

    def run(self, ctx: RequestContext, prompt: str, use_mock: bool | None = None) -> RunResult:
        if use_mock is True:
            if self.settings.execution_guard_enabled:
                execution_decision = self._classify_execution_request(prompt, healthy_aliases=None)
                execution_guarded = self._run_execution_guard(ctx, prompt, execution_decision=execution_decision)
                if execution_guarded is not None:
                    return execution_guarded
            if self.settings.workspace_guard_enabled:
                access_decision = self._classify_workspace_access(prompt, healthy_aliases=None)
                guarded = self._run_workspace_guard(
                    ctx,
                    prompt,
                    access_decision=access_decision,
                    healthy_aliases=None,
                )
                if guarded is not None:
                    return guarded
            return self.run_mock(ctx, prompt)

        health = self.health_checker.probe()
        healthy_aliases = health.healthy_aliases if health.live else None
        if self.settings.execution_guard_enabled:
            execution_decision = self._classify_execution_request(prompt, healthy_aliases=healthy_aliases)
            execution_guarded = self._run_execution_guard(ctx, prompt, execution_decision=execution_decision)
            if execution_guarded is not None:
                return execution_guarded
        if self.settings.workspace_guard_enabled:
            access_decision = self._classify_workspace_access(prompt, healthy_aliases=healthy_aliases)
            guarded = self._run_workspace_guard(
                ctx,
                prompt,
                access_decision=access_decision,
                healthy_aliases=healthy_aliases,
            )
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
        routing_plan = self._build_team_routing_plan(prompt, healthy_aliases=healthy_aliases)
        team, captured_hits, model_routes, effective_agent_payload = self.build_team(
            ctx, healthy_aliases=healthy_aliases
        )
        member_agents = {member.name: member for member in team.members if isinstance(member, Agent)}

        member_outputs: list[dict] = list(prefetched_member_outputs)
        member_outputs.append(
            {
                "name": "Enterprise Orchestrator",
                "phase": "plan",
                "content": self._summarize_plan(routing_plan),
            }
        )
        selected_agents: list[str] = ["Enterprise Orchestrator", *prefetched_agents]
        notes = list(routing_plan.notes)
        delegated_results: dict[str, RunOutput] = {}
        delegated_evidence_blocks: list[dict[str, Any]] = [
            {
                "name": item["name"],
                "phase": item.get("phase", ""),
                "content": item["content"],
            }
            for item in prefetched_member_outputs
        ]
        required_agents = self._ordered_required_agents(routing_plan.required_agents)
        for agent_name in required_agents:
            agent = member_agents.get(agent_name)
            if agent is None:
                continue
            try:
                run_output = self._run_delegate_agent(
                    agent,
                    agent_name=agent_name,
                    prompt=prompt,
                    ctx=ctx,
                    evidence_blocks=delegated_evidence_blocks,
                    healthy_aliases=healthy_aliases,
                )
            except Exception as exc:
                run_output = RunOutput(
                    agent_name=agent_name,
                    content=f"{agent_name} 执行失败: {exc}",
                )
            delegated_results[agent_name] = run_output
            tool_names = self._tool_names_from_run_output(run_output)
            tool_summary = ", ".join(tool_names) if tool_names else "none"
            content = str(getattr(run_output, "content", "") or "").strip() or "未返回内容。"
            member_outputs.append(
                {
                    "name": agent_name,
                    "phase": "delegate",
                    "content": f"tool_evidence={tool_summary}\n{content}",
                }
            )
            delegated_evidence_blocks.append(
                {
                    "name": agent_name,
                    "phase": "delegate",
                    "content": content,
                }
            )
            selected_agents.append(agent_name)
            if agent_name == "Knowledge Agent":
                knowledge_hits = list((getattr(run_output, "metadata", {}) or {}).get("knowledge_hits") or [])
                if knowledge_hits:
                    captured_hits[:] = knowledge_hits

        missing_required_agents = [
            agent_name
            for agent_name in required_agents
            if not self._agent_has_required_evidence(agent_name, delegated_results.get(agent_name))
        ]
        if missing_required_agents:
            notes.append(f"agent_gate_missing_evidence={','.join(missing_required_agents)}")
            failure_text, blocked_outputs = self._build_gate_failure(
                missing_required_agents[0],
                delegated_outputs=member_outputs,
            )
            selected_agents = list(dict.fromkeys(selected_agents))
            self._record_member_outputs(ctx, blocked_outputs)
            return RunResult(
                answer=failure_text,
                mode="agent_gate_blocked",
                selected_agents=selected_agents,
                member_outputs=blocked_outputs,
                knowledge_hits=captured_hits,
                notes=notes,
                model_routes=model_routes,
                prefetch_info=prefetch_info,
                effective_agents=effective_agent_payload,
            )

        synthesizer = Agent(
            name="Enterprise Orchestrator",
            role="整合已验证的成员证据并生成最终用户答复",
            model=team.model,
            skills=self.orchestrator_skills,
            markdown=True,
            instructions=[
                f"当前用户: {ctx.user_id} ({ctx.display_name})，角色: {ctx.role}。",
                f"当前租户: {ctx.tenant_id}，当前项目: {ctx.project_id}。",
                "你现在处于最终整合阶段。",
                "你只能基于已经拿到的成员证据回答，不能再说“我会去查看”或“我先去处理”。",
                "如果成员证据为空，就如实说明；不要补出不存在的文件、目录、知识或执行结果。",
                "严禁跨项目、跨用户引用信息。",
            ],
            additional_context=(
                self.orchestrator_skills.get_system_prompt_snippet()
                if self.orchestrator_skills
                else None
            ),
            db=self.agno_db,
            telemetry=self.settings.telemetry_enabled,
        )
        synthesize_prompt = self._build_synthesizer_prompt(
            prompt=prompt,
            ctx=ctx,
            delegated_outputs=member_outputs,
        )
        response = synthesizer.run(
            synthesize_prompt,
            user_id=ctx.user_id,
            session_id=f"{ctx.session_id}:orchestrator_synthesize",
        )
        final_answer = str(getattr(response, "content", "") or "").strip()
        if not final_answer:
            final_answer = "当前没有拿到足够的成员结果，暂时无法形成最终答复。"
            notes.append("synthesizer_empty_fallback")
        if (
            "Workspace Agent" not in required_agents
            and not self._agent_has_required_evidence("Workspace Agent", delegated_results.get("Workspace Agent"))
            and self._answer_looks_like_repo_listing(ctx, final_answer)
        ):
            failure_text = (
                "检测到当前响应可能包含工程根目录或仓库结构信息，但本轮没有拿到 Workspace Agent 的真实工作区证据。\n"
                "系统已拦截该结果，以避免权限越界。"
            )
            member_outputs.append(
                {
                    "name": "Enterprise Orchestrator",
                    "phase": "gate_block",
                    "content": failure_text,
                }
            )
            selected_agents = list(dict.fromkeys(selected_agents))
            notes.append("agent_gate_blocked_repo_listing_without_workspace_evidence")
            self._record_member_outputs(ctx, member_outputs)
            return RunResult(
                answer=failure_text,
                mode="agent_gate_blocked",
                selected_agents=selected_agents,
                member_outputs=member_outputs,
                knowledge_hits=captured_hits,
                notes=notes,
                model_routes=model_routes,
                prefetch_info=prefetch_info,
                effective_agents=effective_agent_payload,
            )
        member_outputs.append(
            {
                "name": "Enterprise Orchestrator",
                "phase": "synthesize",
                "content": final_answer,
            }
        )
        selected_agents = list(dict.fromkeys(selected_agents))
        self._record_member_outputs(ctx, member_outputs)
        return RunResult(
            answer=final_answer,
            mode="agno",
            selected_agents=selected_agents,
            member_outputs=member_outputs,
            knowledge_hits=captured_hits,
            notes=notes,
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
                "name": "Execution Agent",
                "content": "当前 mock 模式不会真正执行代码；真实执行请走 sandbox 执行链路。",
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
            TASK_EXECUTION: self.model_router.resolve(TASK_EXECUTION).alias,
            TASK_EXTERNAL_BROKER: self.model_router.resolve(TASK_EXTERNAL_BROKER).alias,
        }
        return RunResult(
            answer="\n".join(answer_parts),
            mode="mock",
            selected_agents=[
                "Knowledge Agent",
                "Workspace Agent",
                "Execution Agent",
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
