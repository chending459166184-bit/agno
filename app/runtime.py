from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.skills import LocalSkills, Skills
from agno.team import Team
from agno.team.mode import TeamMode
from agno.tools.mcp import MCPTools
from pydantic import BaseModel, ConfigDict, Field

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
    iteration_count: int = 0
    stop_reason: str | None = None
    orchestration_steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TeamRoutingPlan:
    required_agents: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class OrchestratorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["delegate", "finalize"] = Field(
        description="本轮是继续委托子智能体，还是直接给最终用户答复。"
    )
    rationale: str = Field(
        default="",
        description="对当前动作的简洁理由；如果模型未提供，runtime 会自动补默认值。",
    )
    target_agent: str | None = Field(
        default=None,
        description="当 action=delegate 时要委托的子智能体名称。",
    )
    delegate_instruction: str | None = Field(
        default=None,
        description="给子智能体的本轮任务说明。",
    )
    final_answer: str | None = Field(
        default=None,
        description="当 action=finalize 时给最终用户的答复。",
    )
    stop_reason: str | None = Field(
        default=None,
        description="当 action=finalize 时结束原因，例如 direct_response 或 sufficient_evidence。",
    )


class WorkspaceTaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detected_intent: Literal["list_files", "read_file", "write_file", "unknown"] = "unknown"
    action: Literal["list_files", "read_file", "write_file", "needs_clarification", "policy_blocked"] = (
        "needs_clarification"
    )
    rationale: str = ""
    reason_code: str = "unspecified"
    resolved_relative_path: str | None = None
    extracted_content: str | None = None
    directory_prefix: str | None = None
    clarification_question: str | None = None
    next_action_suggestion: str | None = None
    overwrite: bool = True
    used_default_path: bool = False

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

    def _max_orchestration_iterations(self, member_agents: dict[str, Agent]) -> int:
        configured = int(getattr(self.settings, "orchestrator_max_iterations", 6) or 6)
        dynamic_floor = max(3, len(member_agents) + 1)
        return max(configured, dynamic_floor)

    def _make_orchestration_step(
        self,
        *,
        name: str,
        phase: str,
        content: str,
        iteration: int = 0,
        target_agent: str | None = None,
        status: str = "completed",
        step_type: str | None = None,
        stop_reason: str | None = None,
        tool_evidence: list[str] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": name,
            "phase": phase,
            "content": content,
            "status": status,
            "step_type": step_type or phase,
        }
        if iteration > 0:
            item["iteration"] = iteration
        if target_agent:
            item["target_agent"] = target_agent
        if stop_reason:
            item["stop_reason"] = stop_reason
        if tool_evidence:
            item["tool_evidence"] = list(tool_evidence)
        for key, value in extra.items():
            if value is not None:
                item[key] = value
        return item

    def _dedupe_knowledge_hits(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in hits:
            marker = json.dumps(hit, ensure_ascii=False, sort_keys=True)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(hit)
        return deduped

    def _available_orchestration_agents(
        self,
        member_agents: dict[str, Agent],
        effective_agent_payload: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        metadata_by_name = {
            str(item.get("display_name") or ""): item
            for item in effective_agent_payload
            if item.get("display_name")
        }
        descriptors: list[dict[str, Any]] = []
        for name, agent in member_agents.items():
            metadata = metadata_by_name.get(name, {})
            descriptors.append(
                {
                    "name": name,
                    "role": str(getattr(agent, "role", "") or ""),
                    "description": str(metadata.get("description") or ""),
                    "priority": int(metadata.get("priority") or 0),
                    "tool_summary": list(metadata.get("tool_summary") or []),
                }
            )
        descriptors.sort(key=lambda item: (-item["priority"], item["name"]))
        return descriptors

    def _normalize_orchestrator_decision(self, value: Any) -> OrchestratorDecision:
        raw: Any = value
        if isinstance(value, str):
            raw = json.loads(value)
        elif hasattr(value, "model_dump"):
            raw = value.model_dump()

        decision = raw if isinstance(raw, OrchestratorDecision) else OrchestratorDecision.model_validate(raw)
        if not str(decision.rationale or "").strip():
            if decision.action == "delegate":
                target = str(decision.target_agent or "").strip() or "目标子智能体"
                decision.rationale = f"当前需要先委托 {target} 获取下一步证据。"
            else:
                decision.rationale = "当前证据已足够直接给出答复。"
        return decision

    def _build_orchestrator_decision_prompt(
        self,
        *,
        prompt: str,
        effective_prompt: str,
        ctx: RequestContext,
        routing_plan: TeamRoutingPlan,
        available_agents: list[dict[str, Any]],
        evidence_blocks: list[dict[str, Any]],
        iteration: int,
        max_iterations: int,
        pending_required_agents: list[str],
        retry_missing_agents: list[str] | None = None,
    ) -> str:
        visible_evidence = []
        for block in evidence_blocks[-8:]:
            visible_evidence.append(
                {
                    "name": block.get("name"),
                    "phase": block.get("phase"),
                    "content": str(block.get("content") or "")[:700],
                    **{
                        key: block.get(key)
                        for key in (
                            "status",
                            "reason_code",
                            "detected_intent",
                            "resolved_relative_path",
                            "next_action_suggestion",
                            "tool_calls",
                        )
                        if block.get(key) is not None
                    },
                }
            )
        guided_prompt = self._apply_team_routing_hints(
            effective_prompt,
            routing_plan,
            retry_missing_agents=retry_missing_agents,
        )
        return (
            "你是 Enterprise Orchestrator 的多轮编排决策内核。\n"
            "你的唯一职责是决定当前这一轮的下一步动作：委托哪个子智能体，或直接给最终用户答复。\n"
            "你没有 Workspace MCP、知识检索、sandbox 执行或 A2A 的直接访问权，不能伪造这些结果。\n"
            "只有 observe 阶段里已经拿到的证据才能当作真实事实使用。\n"
            "你可以重复调用同一个子智能体，也可以切换到其他子智能体。\n"
            "如果当前没有合适子智能体，或者已有证据已经足够，就选择 finalize。\n"
            "如果仍存在 pending_required_agents，就不能 finalize，必须先补齐这些真实证据。\n\n"
            f"轮次: {iteration}/{max_iterations}\n"
            f"用户上下文: tenant={ctx.tenant_id}, user={ctx.user_id}, project={ctx.project_id}, role={ctx.role}\n"
            f"用户原始请求: {prompt}\n"
            f"编排提示后的请求: {guided_prompt}\n"
            f"待补齐的强制证据 agent: {json.dumps(pending_required_agents, ensure_ascii=False)}\n"
            f"可用子智能体(JSON): {json.dumps(available_agents, ensure_ascii=False)}\n"
            f"已有证据(JSON): {json.dumps(visible_evidence, ensure_ascii=False)}\n"
        )

    def _json_signature(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def _delegate_policy_flags(self, agent_name: str) -> dict[str, Any]:
        if agent_name == "Workspace Agent":
            return {
                "allow_workspace_context_path_inference": True,
                "allow_generated_filename": True,
                "allow_clarification": True,
                "allow_overwrite": True,
                "prefer_existing_directories": True,
                "create_new_directory_by_default": False,
            }
        return {"allow_clarification": True}

    def _delegate_allowed_tools(self, agent_name: str, agent_descriptor: dict[str, Any] | None = None) -> list[str]:
        explicit_map = {
            "Knowledge Agent": ["search_project_knowledge"],
            "Workspace Agent": [
                "workspace_list_files",
                "workspace_read_text_file",
                "workspace_save_text_file",
            ],
            "Execution Agent": ["execute_in_sandbox"],
            "External Agent Broker": ["list_external_agents", "delegate_to_external_agent"],
            "Test Agent": [],
        }
        mapped = explicit_map.get(agent_name)
        if mapped is not None:
            return list(mapped)
        return list((agent_descriptor or {}).get("tool_summary") or [])

    def _default_delegate_instruction(
        self,
        agent_name: str | None,
        current_instruction: str | None,
    ) -> str:
        current = str(current_instruction or "").strip()
        if agent_name == "Workspace Agent":
            if not current or "获取下一步证据" in current or current == "请处理当前最关键的下一步。":
                return (
                    "请基于原始用户请求自主判断是列目录、读取文件还是保存文本。"
                    "如果用户要保存内容但没有给出相对路径，请先通过 Workspace MCP 感知当前用户空间结构，"
                    "再推断合适的相对路径并完成真实写入；只有在关键信息仍不足时才向用户确认。"
                )
        return current or "请处理当前最关键的下一步。"

    def _sanitize_delegate_payload_for_trace(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "original_user_message": str(payload.get("original_user_message") or "")[:500],
            "orchestrator_goal": str(payload.get("orchestrator_goal") or "")[:300],
            "current_iteration": payload.get("current_iteration"),
            "tenant_id": payload.get("tenant_id"),
            "user_id": payload.get("user_id"),
            "project_id": payload.get("project_id"),
            "workspace_root": payload.get("workspace_root"),
            "allowed_tools": list(payload.get("allowed_tools") or []),
            "agent_role": payload.get("agent_role"),
            "safety_boundary": payload.get("safety_boundary"),
            "policy_flags": dict(payload.get("policy_flags") or {}),
            "prior_attempt_count": len(payload.get("prior_attempts") or []),
            "known_evidence_count": len(payload.get("known_evidence") or []),
        }

    def _delegate_runtime_rules(
        self,
        agent_name: str,
        payload: dict[str, Any],
    ) -> list[str]:
        base_rules = [
            "只能在当前授权边界内行动。",
            "不能伪造 tool_evidence 或工具返回结果。",
            "最终结论必须基于真实工具结果，或明确的澄清/阻断状态。",
        ]
        policy_flags = dict(payload.get("policy_flags") or {})
        if agent_name == "Workspace Agent":
            base_rules.extend(
                [
                    "只能在 workspace_root 下读取、写入和整理文件。",
                    "不允许把系统固定默认目录当成主策略。",
                    "优先感知当前用户自己的工作区结构，再推断最合适的相对路径。",
                    "如果能推断出合理路径，就直接调用真实保存工具完成写入。",
                    "只有当路径无法从当前用户空间和请求中推出时，才返回 needs_clarification。",
                ]
            )
            if not policy_flags.get("create_new_directory_by_default", False):
                base_rules.append("没有充分证据时，不要默认创建新的目录层级。")
        elif agent_name == "Knowledge Agent":
            base_rules.extend(
                [
                    "只在当前 tenant/user/project 授权范围内检索知识。",
                    "没有命中时要明确说明，不要脑补文档内容。",
                ]
            )
        elif agent_name == "Execution Agent":
            base_rules.extend(
                [
                    "只有在确实需要运行代码、脚本或命令时才执行。",
                    "只能基于真实 sandbox 结果返回结论。",
                ]
            )
        elif agent_name == "External Agent Broker":
            base_rules.extend(
                [
                    "优先内部能力，只有确实需要外部能力时才做外部委托。",
                    "只向外部 agent 暴露最小必要上下文。",
                ]
            )
        return base_rules

    def _build_delegate_runtime_context(
        self,
        *,
        agent_name: str,
        payload: dict[str, Any],
    ) -> str:
        original_user_message = str(payload.get("original_user_message") or "").strip()
        orchestrator_goal = str(payload.get("orchestrator_goal") or "").strip()
        known_evidence = list(payload.get("known_evidence") or [])
        lines = [
            f"你是 {agent_name}，负责处理当前这轮委托任务。",
            "",
            "当前运行上下文：",
            f"- tenant_id: {payload.get('tenant_id') or 'unknown'}",
            f"- user_id: {payload.get('user_id') or 'unknown'}",
            f"- project_id: {payload.get('project_id') or 'unknown'}",
            f"- workspace_root: {payload.get('workspace_root') or 'n/a'}",
            f"- agent_role: {payload.get('agent_role') or 'n/a'}",
            "",
            "当前任务：",
            f"- 原始用户请求：{original_user_message or 'n/a'}",
            f"- 主智能体目标：{orchestrator_goal or 'n/a'}",
            f"- 当前轮次：{payload.get('current_iteration') or 0}",
        ]
        if known_evidence:
            lines.extend(
                [
                    "",
                    "当前已知证据：",
                    *[
                        (
                            f"- {item.get('name') or 'unknown'}"
                            f" | phase={item.get('phase') or 'unknown'}"
                            f" | status={item.get('status') or 'n/a'}"
                            f" | content={str(item.get('content') or '')[:180]}"
                        )
                        for item in known_evidence[-5:]
                    ],
                ]
            )
        allowed_tools = list(payload.get("allowed_tools") or [])
        if allowed_tools:
            lines.extend(
                [
                    "",
                    "允许使用的工具：",
                    f"- {', '.join(allowed_tools)}",
                ]
            )
        safety_boundary = str(payload.get("safety_boundary") or "").strip()
        if safety_boundary:
            lines.extend(
                [
                    "",
                    "安全边界：",
                    f"- {safety_boundary}",
                ]
            )
        lines.extend(
            [
                "",
                "你的工作规则：",
                *[f"{index}. {rule}" for index, rule in enumerate(self._delegate_runtime_rules(agent_name, payload), start=1)],
            ]
        )
        return "\n".join(lines).strip()

    def _build_delegate_payload(
        self,
        *,
        agent_name: str,
        ctx: RequestContext,
        original_user_message: str,
        delegate_instruction: str,
        evidence_blocks: list[dict[str, Any]],
        iteration: int,
        allowed_tools: list[str],
        agent_role: str,
        prior_attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        known_evidence = [
            {
                "name": block.get("name"),
                "phase": block.get("phase"),
                "content": str(block.get("content") or "")[:500],
                **{
                    key: block.get(key)
                    for key in ("status", "reason_code", "detected_intent", "resolved_relative_path")
                    if block.get(key) is not None
                },
            }
            for block in evidence_blocks[-10:]
        ]
        workspace_root = getattr(ctx, "workspace_root", None)
        payload = {
            "original_user_message": original_user_message,
            "conversation_context": {
                "latest_user_message": original_user_message,
                "recent_evidence": known_evidence,
            },
            "orchestrator_goal": delegate_instruction,
            "current_iteration": iteration,
            "known_evidence": known_evidence,
            "tenant_id": getattr(ctx, "tenant_id", ""),
            "user_id": getattr(ctx, "user_id", ""),
            "project_id": getattr(ctx, "project_id", ""),
            "workspace_root": str(workspace_root) if workspace_root is not None else "",
            "allowed_tools": allowed_tools,
            "agent_role": agent_role,
            "safety_boundary": (
                "只能访问当前用户工作区；不能伪造工具结果；不能越权读取其他用户或仓库根目录；"
                "最终结论必须基于真实工具结果或明确的澄清/阻断状态。"
            ),
            "policy_flags": self._delegate_policy_flags(agent_name),
            "prior_attempts": list(prior_attempts or []),
        }
        payload["payload_signature"] = self._json_signature(
            {
                "agent_name": agent_name,
                "original_user_message": original_user_message,
                "orchestrator_goal": delegate_instruction,
                "policy_flags": payload["policy_flags"],
                "allowed_tools": allowed_tools,
            }
        )
        return payload

    def _serialize_delegate_payload_for_prompt(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _recent_attempts_for_agent(
        self,
        observation_history: list[dict[str, Any]],
        agent_name: str,
        *,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        return [dict(item) for item in observation_history if item.get("agent_name") == agent_name][-limit:]

    def _repeat_failure_streak(
        self,
        observation_history: list[dict[str, Any]],
        *,
        agent_name: str,
        reason_code: str | None,
        payload_signature: str | None,
    ) -> int:
        if not reason_code or not payload_signature:
            return 0
        streak = 0
        for item in reversed(observation_history):
            if (
                item.get("agent_name") == agent_name
                and item.get("reason_code") == reason_code
                and item.get("payload_signature") == payload_signature
            ):
                streak += 1
                continue
            break
        return streak

    def _fallback_orchestrator_decision(
        self,
        *,
        available_agents: list[dict[str, Any]],
        pending_required_agents: list[str],
        notes: list[str],
        error: Exception | None = None,
    ) -> OrchestratorDecision:
        available_names = [item["name"] for item in available_agents]
        for agent_name in pending_required_agents:
            if agent_name in available_names:
                notes.append(f"orchestrator_decision_fallback={agent_name}")
                return OrchestratorDecision(
                    action="delegate",
                    rationale="结构化决策失败，按强制证据约束回退到必需 agent。",
                    target_agent=agent_name,
                    delegate_instruction="请优先补齐当前缺失的真实证据，并返回可验证结果。",
                )
        notes.append(f"orchestrator_decision_failed={error or 'unknown_error'}")
        return OrchestratorDecision(
            action="finalize",
            rationale="结构化决策失败，且当前没有可安全继续委托的必需 agent。",
            final_answer=(
                "当前主编排没有成功产出可验证的下一步决策，系统已停止，以避免在缺少证据时继续推断。"
            ),
            stop_reason="decision_error",
        )

    def _decide_orchestration_step(
        self,
        *,
        ctx: RequestContext,
        prompt: str,
        effective_prompt: str,
        routing_plan: TeamRoutingPlan,
        available_agents: list[dict[str, Any]],
        evidence_blocks: list[dict[str, Any]],
        iteration: int,
        max_iterations: int,
        model: Any,
        notes: list[str],
        pending_required_agents: list[str],
        retry_missing_agents: list[str] | None = None,
    ) -> OrchestratorDecision:
        decision_agent = Agent(
            name="Enterprise Orchestrator",
            role="负责多轮动态编排的主智能体，只产出结构化下一步决策",
            model=model,
            skills=self.orchestrator_skills,
            db=self.agno_db,
            telemetry=self.settings.telemetry_enabled,
            markdown=False,
            instructions=[
                f"当前用户: {ctx.user_id} ({ctx.display_name})，角色: {ctx.role}。",
                f"当前租户: {ctx.tenant_id}，当前项目: {ctx.project_id}。",
                "严格遵守最小权限和证据优先原则。",
                "Workspace、Knowledge、Execution、External Agent Broker 的真实结果只能来自已有 observe 证据。",
                "如果你选择 delegate，target_agent 必须来自可用子智能体列表。",
                "如果你选择 finalize，必须给出 final_answer 和 stop_reason。",
                "不要输出 Markdown 或解释文字，只返回结构化结果。",
            ],
            additional_context=(
                self.orchestrator_skills.get_system_prompt_snippet()
                if self.orchestrator_skills
                else None
            ),
            output_schema=OrchestratorDecision,
            structured_outputs=True,
            use_json_mode=True,
        )
        prompt_text = self._build_orchestrator_decision_prompt(
            prompt=prompt,
            effective_prompt=effective_prompt,
            ctx=ctx,
            routing_plan=routing_plan,
            available_agents=available_agents,
            evidence_blocks=evidence_blocks,
            iteration=iteration,
            max_iterations=max_iterations,
            pending_required_agents=pending_required_agents,
            retry_missing_agents=retry_missing_agents,
        )
        try:
            response = decision_agent.run(
                prompt_text,
                user_id=ctx.user_id,
                session_id=f"{ctx.session_id}:orchestrator_round_{iteration}",
            )
            return self._normalize_orchestrator_decision(getattr(response, "content", response))
        except Exception as exc:
            return self._fallback_orchestrator_decision(
                available_agents=available_agents,
                pending_required_agents=pending_required_agents,
                notes=notes,
                error=exc,
            )

    def _build_agent_task_prompt(
        self,
        agent_name: str,
        *,
        prompt: str,
        ctx: RequestContext,
        evidence_blocks: list[dict[str, Any]],
        delegate_payload: dict[str, Any] | None = None,
    ) -> str:
        evidence_lines: list[str] = []
        for block in evidence_blocks:
            evidence_lines.append(f"- {block['name']}: {block['content']}")
        evidence_text = "\n".join(evidence_lines) if evidence_lines else "- 当前还没有其他成员证据。"

        base = [
            f"用户原始请求: {str((delegate_payload or {}).get('original_user_message') or prompt)}",
            f"当前租户: {ctx.tenant_id}，当前用户: {ctx.user_id}，当前项目: {ctx.project_id}",
            "已有证据:",
            evidence_text,
        ]
        if delegate_payload:
            base.extend(
                [
                    "",
                    "[委托运行上下文]",
                    self._build_delegate_runtime_context(
                        agent_name=agent_name,
                        payload=delegate_payload,
                    ),
                ]
            )
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

    def _workspace_content_category(self, message: str, content: str | None = None) -> str:
        combined = f"{message}\n{content or ''}".lower()
        if any(keyword in combined for keyword in ["诗", "poem", "poetry", "lyric", "lyrics"]):
            return "poem"
        if any(keyword in combined for keyword in ["草稿", "draft"]):
            return "draft"
        if any(keyword in combined for keyword in ["文章", "essay", "article", "blog", "post"]):
            return "article"
        if any(keyword in combined for keyword in ["笔记", "note", "memo", "journal"]):
            return "note"
        return "text"

    def _slugify_filename_seed(self, value: str) -> str:
        candidate = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "")).strip("-_.").lower()
        candidate = re.sub(r"-{2,}", "-", candidate)
        return candidate[:40]

    def _infer_workspace_write_path(
        self,
        *,
        message: str,
        content: str,
        workspace_files: list[dict[str, Any]],
    ) -> tuple[str, str]:
        category = self._workspace_content_category(message, content)
        category_hints = {
            "poem": ["poem", "poems", "poetry", "lyric", "lyrics", "draft", "drafts", "writing"],
            "draft": ["draft", "drafts", "writing", "workspace"],
            "article": ["article", "articles", "essay", "essays", "blog", "blogs", "writing", "docs"],
            "note": ["note", "notes", "memo", "memos", "journal", "journals"],
            "text": ["note", "notes", "draft", "drafts", "docs", "documents"],
        }
        parent_directories: dict[str, int] = {}
        for item in workspace_files:
            path = str(item.get("path") or "").strip().strip("/")
            if "/" not in path:
                continue
            parent = path.rsplit("/", 1)[0]
            parent_directories[parent] = parent_directories.get(parent, 0) + 1

        best_directory = ""
        best_score = -1
        for directory, count in parent_directories.items():
            base = directory.rsplit("/", 1)[-1].lower()
            score = count
            hints = category_hints.get(category, [])
            if base in hints:
                score += 8
            elif any(hint in base for hint in hints):
                score += 4
            if directory.count("/") == 0:
                score += 1
            if score > best_score:
                best_directory = directory
                best_score = score

        seed = ""
        if content:
            first_content_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
            seed = self._slugify_filename_seed(first_content_line)
        if not seed:
            first_message_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
            seed = self._slugify_filename_seed(first_message_line)
        if not seed:
            seed = category
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{seed}-{timestamp}.txt"
        if best_directory:
            return f"{best_directory}/{filename}", "write_with_inferred_workspace_path"
        return filename, "write_with_generated_root_path"

    def _heuristic_workspace_task_plan(self, delegate_payload: dict[str, Any]) -> WorkspaceTaskPlan:
        message = str(delegate_payload.get("original_user_message") or "").strip()
        lowered = message.lower()
        signal = self._detect_workspace_guard(message) or {}
        action = str(signal.get("action") or "").strip().lower()
        path = str(signal.get("path") or "").strip() or None
        content = str(signal.get("content") or "").strip() or None
        policy_flags = dict(delegate_payload.get("policy_flags") or {})
        first_line, _, rest = message.partition("\n")

        if not action and any(keyword in lowered for keyword in ["保存", "写入", "save", "write"]):
            action = "write"
        if not content and action == "write":
            colon_match = re.search(r"(?:下面的内容|以下内容|内容|正文|text)\s*[:：]\s*(.+)$", message, re.IGNORECASE | re.DOTALL)
            if colon_match:
                content = colon_match.group(1).strip() or None
            elif rest.strip() and any(keyword in first_line.lower() for keyword in ["保存", "写入", "save", "write"]):
                content = rest.strip() or None

        if action == "write":
            if not content:
                return WorkspaceTaskPlan(
                    detected_intent="write_file",
                    action="needs_clarification",
                    rationale="用户表达了写入意图，但还没有给出要保存的正文内容。",
                    reason_code="missing_content",
                    clarification_question="你想保存什么内容？如果方便，也请一起告诉我目标相对路径。",
                    next_action_suggestion="ask_user_for_content_and_path",
                )
            if not path:
                if not policy_flags.get("allow_workspace_context_path_inference", True):
                    return WorkspaceTaskPlan(
                        detected_intent="write_file",
                        action="needs_clarification",
                        rationale="用户要求保存文本，但当前策略不允许在缺路径时自行推断相对路径。",
                        reason_code="missing_target_path",
                        extracted_content=content,
                        clarification_question="要把这段内容保存到哪个相对路径？",
                        next_action_suggestion="ask_user_for_filename",
                    )
                return WorkspaceTaskPlan(
                    detected_intent="write_file",
                    action="write_file",
                    rationale="用户要求保存文本，但没有给出路径；Workspace Agent 需要先结合当前用户工作区结构推断合适相对路径，再执行真实写入。",
                    reason_code="write_requires_path_inference",
                    extracted_content=content,
                    next_action_suggestion="inspect_workspace_then_write",
                )
            return WorkspaceTaskPlan(
                detected_intent="write_file",
                action="write_file",
                rationale="用户已经给出了写入意图和可执行的相对路径。",
                reason_code="write_with_explicit_path",
                resolved_relative_path=path,
                extracted_content=content,
                next_action_suggestion="write_file",
            )

        if action == "read":
            if not path:
                return WorkspaceTaskPlan(
                    detected_intent="read_file",
                    action="needs_clarification",
                    rationale="用户想读取文件，但没有给出明确相对路径。",
                    reason_code="missing_target_path",
                    clarification_question="你想读取哪个相对路径的文件？",
                    next_action_suggestion="ask_user_for_path",
                )
            return WorkspaceTaskPlan(
                detected_intent="read_file",
                action="read_file",
                rationale="用户明确要求读取某个文件。",
                reason_code="read_with_explicit_path",
                resolved_relative_path=path,
                next_action_suggestion="read_file",
            )

        if action == "list" or any(
            keyword in lowered
            for keyword in ["目录", "文件", "workspace", "工作区", "有哪些", "列出", "list", "show"]
        ):
            return WorkspaceTaskPlan(
                detected_intent="list_files",
                action="list_files",
                rationale="用户要求查看工作区中的文件或目录。",
                reason_code="list_workspace",
                directory_prefix=path or "",
                next_action_suggestion="list_files",
            )

        return WorkspaceTaskPlan(
            detected_intent="unknown",
            action="needs_clarification",
            rationale="当前请求与工作区相关，但尚不足以判断是读、写还是列目录。",
            reason_code="ambiguous_workspace_intent",
            clarification_question="你希望我在当前工作区做什么？例如列文件、读取某个文件，或把文本保存到某个相对路径。",
            next_action_suggestion="ask_user_for_workspace_intent",
        )

    def _plan_workspace_delegate(
        self,
        agent: Agent | None,
        *,
        ctx: RequestContext,
        delegate_payload: dict[str, Any],
        healthy_aliases: set[str] | None,
    ) -> WorkspaceTaskPlan:
        heuristic_plan = self._heuristic_workspace_task_plan(delegate_payload)
        model = getattr(agent, "model", None)
        if model is None:
            return heuristic_plan
        delegate_runtime_context = self._build_delegate_runtime_context(
            agent_name="Workspace Agent",
            payload=delegate_payload,
        )
        planner = Agent(
            name="Workspace Agent",
            role="负责工作区任务理解与安全执行决策的专业子智能体",
            model=model,
            skills=self.workspace_skills,
            db=self.agno_db,
            telemetry=self.settings.telemetry_enabled,
            markdown=False,
            instructions=[
                f"当前用户: {ctx.user_id} ({ctx.display_name})，角色: {ctx.role}。",
                f"当前租户: {ctx.tenant_id}，当前项目: {ctx.project_id}。",
                "你要先理解工作区任务，再决定是读、写、列目录、澄清还是阻断。",
                "如果缺少相对路径，优先结合当前用户工作区结构推断更自然的相对路径，不要退回统一模板目录。",
                "不要伪造工具结果；这里只负责输出下一步工作区计划。",
                "所有路径都必须是当前用户 workspace 内的相对路径。",
                delegate_runtime_context,
            ],
            output_schema=WorkspaceTaskPlan,
            structured_outputs=True,
            use_json_mode=True,
        )
        prompt_text = (
            "请基于下面的 delegate payload，为 Workspace Agent 输出本轮结构化执行计划。\n"
            f"delegate_runtime_context:\n{delegate_runtime_context}\n"
            f"heuristic_candidate(JSON): {json.dumps(heuristic_plan.model_dump(), ensure_ascii=False)}\n"
            f"delegate_payload(JSON): {self._serialize_delegate_payload_for_prompt(delegate_payload)}"
        )
        try:
            response = planner.run(
                prompt_text,
                user_id=ctx.user_id,
                session_id=f"{ctx.session_id}:workspace_agent_planner",
            )
            content = getattr(response, "content", response)
            plan = content if isinstance(content, WorkspaceTaskPlan) else WorkspaceTaskPlan.model_validate(content)
            if heuristic_plan.detected_intent != "unknown":
                if plan.detected_intent == "unknown":
                    plan.detected_intent = heuristic_plan.detected_intent
                if plan.action == "needs_clarification" and heuristic_plan.action != "needs_clarification":
                    plan.action = heuristic_plan.action
                if not plan.extracted_content and heuristic_plan.extracted_content:
                    plan.extracted_content = heuristic_plan.extracted_content
                if not plan.resolved_relative_path and heuristic_plan.resolved_relative_path:
                    plan.resolved_relative_path = heuristic_plan.resolved_relative_path
            for field_name in (
                "reason_code",
                "directory_prefix",
                "clarification_question",
                "next_action_suggestion",
            ):
                if not getattr(plan, field_name):
                    setattr(plan, field_name, getattr(heuristic_plan, field_name))
            if not plan.used_default_path:
                plan.used_default_path = heuristic_plan.used_default_path
            if not str(plan.rationale or "").strip():
                plan.rationale = heuristic_plan.rationale or "Workspace Agent 已根据当前任务上下文生成执行计划。"
            if not str(plan.reason_code or "").strip():
                plan.reason_code = heuristic_plan.reason_code
            return plan
        except Exception:
            return heuristic_plan

    def _build_workspace_agent_response_content(
        self,
        *,
        plan: WorkspaceTaskPlan,
        status: str,
        ctx: RequestContext,
        safe_payload: dict[str, Any] | None = None,
    ) -> str:
        if status == "success":
            if plan.action == "write_file":
                path = (safe_payload or {}).get("path") or plan.resolved_relative_path or ""
                return f"已把内容保存到你当前工作区的 `{path}`。"
            if plan.action == "read_file":
                path = (safe_payload or {}).get("path") or plan.resolved_relative_path or ""
                content = str((safe_payload or {}).get("content") or "")
                return f"已读取你当前工作区的 `{path}`：\n{content}"
            files = [str(item.get("path") or "") for item in (safe_payload or {}).get("files") or [] if item.get("path")]
            if not files:
                return "我查看了你当前工作区，但暂时没有发现可见文件。"
            return "我查看了你当前工作区，当前可见文件包括：\n" + "\n".join(f"- {path}" for path in files[:20])
        if status == "needs_clarification":
            if plan.clarification_question and plan.resolved_relative_path:
                return (
                    f"{plan.clarification_question}\n"
                    f"如果你愿意，我也可以直接保存到 `{plan.resolved_relative_path}`。"
                )
            if plan.clarification_question:
                return plan.clarification_question
            return "我可以继续处理这个工作区任务，但还需要你补充一点关键信息。"
        if status == "policy_blocked":
            return (
                "这次工作区操作被安全策略阻断了。"
                f"\n原因：{plan.rationale}"
            )
        return (
            "我在处理工作区任务时遇到了错误。"
            f"\n原因：{plan.rationale}"
        )

    def _run_workspace_delegate(
        self,
        agent: Agent | None,
        ctx: RequestContext,
        prompt: str,
        *,
        delegate_payload: dict[str, Any] | None = None,
        healthy_aliases: set[str] | None,
    ) -> RunOutput:
        payload = delegate_payload or self._build_delegate_payload(
            agent_name="Workspace Agent",
            ctx=ctx,
            original_user_message=prompt,
            delegate_instruction="请完成当前工作区相关任务。",
            evidence_blocks=[],
            iteration=1,
            allowed_tools=["workspace_list_files", "workspace_read_text_file", "workspace_save_text_file"],
            agent_role="通过 Workspace MCP 访问当前用户工作区",
            prior_attempts=[],
        )
        plan = self._plan_workspace_delegate(
            agent,
            ctx=ctx,
            delegate_payload=payload,
            healthy_aliases=healthy_aliases,
        )
        tool_calls: list[dict[str, Any]] = []
        tool_executions: list[ToolExecution] = []
        metadata: dict[str, Any] = {
            "delegate_mode": "autonomous_workspace_agent",
            "delegate_payload": self._sanitize_delegate_payload_for_trace(payload),
            "delegate_payload_signature": payload.get("payload_signature"),
            "detected_intent": plan.detected_intent,
            "reason_code": plan.reason_code,
            "resolved_relative_path": plan.resolved_relative_path,
            "extracted_content": plan.extracted_content,
            "next_action_suggestion": plan.next_action_suggestion,
            "used_default_path": plan.used_default_path,
            "rationale": plan.rationale,
        }
        if plan.action == "needs_clarification":
            metadata["status"] = "needs_clarification"
            return RunOutput(
                agent_name="Workspace Agent",
                content=self._build_workspace_agent_response_content(
                    plan=plan,
                    status="needs_clarification",
                    ctx=ctx,
                ),
                tools=[],
                metadata=metadata,
            )
        if plan.action == "policy_blocked":
            metadata["status"] = "policy_blocked"
            return RunOutput(
                agent_name="Workspace Agent",
                content=self._build_workspace_agent_response_content(
                    plan=plan,
                    status="policy_blocked",
                    ctx=ctx,
                ),
                tools=[],
                metadata=metadata,
            )

        if plan.action == "write_file" and not plan.resolved_relative_path:
            snapshot_args = {"prefix": "", "limit": 200}
            workspace_files: list[dict[str, Any]] = []
            try:
                snapshot_payload = call_workspace_mcp_tool(
                    self.settings,
                    ctx,
                    "workspace_list_files",
                    snapshot_args,
                )
                snapshot_safe_payload = self._sanitize_workspace_guard_payload("list", snapshot_payload, ctx)
                workspace_files = list(snapshot_safe_payload.get("files") or [])
                tool_calls.append(
                    {
                        "tool": "workspace_list_files",
                        "args": snapshot_args,
                        "result": snapshot_safe_payload,
                    }
                )
                tool_executions.append(
                    self._make_tool_execution(
                        tool_name="workspace_list_files",
                        tool_args=snapshot_args,
                        result=snapshot_safe_payload,
                    )
                )
            except Exception as exc:
                tool_calls.append(
                    {
                        "tool": "workspace_list_files",
                        "args": snapshot_args,
                        "error": str(exc),
                    }
                )
            inferred_path, reason_code = self._infer_workspace_write_path(
                message=str(payload.get("original_user_message") or prompt),
                content=str(plan.extracted_content or ""),
                workspace_files=workspace_files,
            )
            plan.resolved_relative_path = inferred_path
            plan.reason_code = reason_code
            plan.detected_intent = "write_file"
            plan.next_action_suggestion = "write_file"
            plan.used_default_path = False
            metadata["resolved_relative_path"] = inferred_path
            metadata["reason_code"] = reason_code
            metadata["detected_intent"] = "write_file"
            metadata["next_action_suggestion"] = "write_file"

        try:
            if plan.action == "write_file":
                tool_name = "workspace_save_text_file"
                tool_args = {
                    "path": str(plan.resolved_relative_path or "").strip().lstrip("/"),
                    "content": str(plan.extracted_content or ""),
                    "overwrite": bool(plan.overwrite),
                }
                payload_result = call_workspace_mcp_tool(self.settings, ctx, tool_name, tool_args)
                safe_payload = self._sanitize_workspace_guard_payload("write", payload_result, ctx)
            elif plan.action == "read_file":
                tool_name = "workspace_read_text_file"
                tool_args = {
                    "path": str(plan.resolved_relative_path or "").strip().lstrip("/"),
                    "max_chars": 6000,
                }
                payload_result = call_workspace_mcp_tool(self.settings, ctx, tool_name, tool_args)
                safe_payload = self._sanitize_workspace_guard_payload("read", payload_result, ctx)
            else:
                tool_name = "workspace_list_files"
                tool_args = {"prefix": str(plan.directory_prefix or ""), "limit": 50}
                payload_result = call_workspace_mcp_tool(self.settings, ctx, tool_name, tool_args)
                safe_payload = self._sanitize_workspace_guard_payload("list", payload_result, ctx)
        except Exception as exc:
            metadata["status"] = "tool_error"
            metadata["tool_evidence"] = []
            return RunOutput(
                agent_name="Workspace Agent",
                content=(
                    "Workspace Agent 在调用 Workspace MCP 时失败。\n"
                    f"- reason_code: workspace_tool_error\n"
                    f"- error: {exc}"
                ),
                tools=[],
                metadata={
                    **metadata,
                    "reason_code": "workspace_tool_error",
                    "error": str(exc),
                    "tool_calls": tool_calls,
                },
            )

        metadata["status"] = "success"
        metadata["next_action_suggestion"] = "finalize_with_tool_result"
        tool_calls.append({"tool": tool_name, "args": tool_args, "result": safe_payload})
        tool_executions.append(
            self._make_tool_execution(tool_name=tool_name, tool_args=tool_args, result=safe_payload)
        )
        metadata["tool_evidence"] = [
            {
                "tool": item["tool"],
                "path": item.get("result", {}).get("path") or item.get("result", {}).get("root"),
            }
            for item in tool_calls
            if isinstance(item.get("result"), dict)
        ]
        metadata["tool_calls"] = tool_calls
        return RunOutput(
            agent_name="Workspace Agent",
            content=self._build_workspace_agent_response_content(
                plan=plan,
                status="success",
                ctx=ctx,
                safe_payload=safe_payload,
            ),
            tools=tool_executions,
            metadata={**metadata, "safe_payload": safe_payload, "action": plan.action},
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
                "status": "success",
                "reason_code": "knowledge_hits_found" if hits else "knowledge_no_match",
                "knowledge_hits": hits,
                "tool_calls": [
                    {
                        "tool": "search_project_knowledge",
                        "args": {"query": prompt, "limit": 4},
                        "result_count": len(hits),
                    }
                ],
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
            metadata={
                "delegate_mode": "explicit_runtime_executor",
                "status": "success",
                "reason_code": "execution_completed",
                "tool_calls": [
                    {
                        "tool": "execute_in_sandbox",
                        "args": {
                            "command": request.command,
                            "entrypoint": request.entrypoint,
                            "timeout_seconds": request.timeout_seconds,
                            "writeback": request.writeback,
                        },
                        "result": {
                            "job_id": result.job.job_id,
                            "status": result.job.status,
                            "artifacts": [item.relative_path for item in result.artifacts],
                        },
                    }
                ],
            },
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
            metadata={
                "delegate_mode": "explicit_runtime_executor",
                "status": "success",
                "reason_code": "external_delegate_completed",
                "tool_calls": [
                    {
                        "tool": "delegate_to_external_agent",
                        "args": {"message": prompt, "agent_id": result.selected_agent.agent_id},
                        "result": {
                            "selected_agent": result.selected_agent.agent_id,
                            "status": getattr(result, "status", None),
                        },
                    }
                ],
            },
        )

    def _run_explicit_delegate(
        self,
        agent: Agent,
        agent_name: str,
        *,
        prompt: str,
        ctx: RequestContext,
        delegate_payload: dict[str, Any] | None = None,
        healthy_aliases: set[str] | None,
    ) -> RunOutput | None:
        source_prompt = str((delegate_payload or {}).get("original_user_message") or prompt)
        if agent_name == "Workspace Agent":
            return self._run_workspace_delegate(
                agent,
                ctx,
                source_prompt,
                delegate_payload=delegate_payload,
                healthy_aliases=healthy_aliases,
            )
        if agent_name == "Knowledge Agent":
            return self._run_knowledge_delegate(ctx, source_prompt)
        if agent_name == "Execution Agent":
            return self._run_execution_delegate(ctx, source_prompt, healthy_aliases=healthy_aliases)
        if agent_name == "External Agent Broker":
            return self._run_external_delegate(ctx, source_prompt)
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
        delegate_payload: dict[str, Any] | None = None,
        healthy_aliases: set[str] | None = None,
    ) -> RunOutput:
        explicit = self._run_explicit_delegate(
            agent,
            agent_name,
            prompt=prompt,
            ctx=ctx,
            delegate_payload=delegate_payload,
            healthy_aliases=healthy_aliases,
        )
        if explicit is not None:
            return explicit
        task_prompt = self._build_agent_task_prompt(
            agent_name,
            prompt=prompt,
            ctx=ctx,
            evidence_blocks=evidence_blocks,
            delegate_payload=delegate_payload,
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
            r"(写入|保存|存一下|save|write).{0,12}(下面的内容|以下内容|内容|正文|文本|text)",
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
        elif action == "write":
            first_line, _, rest = prompt.partition("\n")
            if rest.strip() and any(keyword in first_line.lower() for keyword in ["保存", "写入", "save", "write", "存一下"]):
                content = rest.strip()
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
            extra_payload = {
                key: value
                for key, value in item.items()
                if key not in {"name", "content", "phase"} and value is not None
            }
            self.database.record_member_output(
                ctx,
                member_name=item["name"],
                order=index,
                content=item["content"],
                phase=item.get("phase", "team"),
                metadata=extra_payload or None,
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
                    role="管理当前用户自己的工作区文件，读取、保存并整理与当前任务相关的本地内容",
                    model=build_agno_model(self.settings, workspace_route.alias),
                    skills=self.workspace_skills,
                    tools=[workspace_tools],
                    tool_choice="required",
                    markdown=True,
                    instructions=[
                        f"你只能访问当前用户 {ctx.user_id} 的工作区。",
                        "先理解当前任务是列目录、读取文件还是保存文本，再决定使用哪个 MCP 工具。",
                        "如用户要求保存内容但没给相对路径，先感知当前用户工作区结构，再推断最合适的相对路径并执行真实写入。",
                        "不要假设存在某个文件，先确认再读取。",
                        f"当前为统一模型网关路由，task_type=workspace，alias={workspace_route.alias}。",
                    ],
                    db=self.agno_db,
                    telemetry=self.settings.telemetry_enabled,
                )
            )
            enabled_descriptions.append("当请求需要用户文件、草稿、笔记或工作区写入时，先委托 Workspace Agent。")

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
        member_agents = {
            member.name: member
            for member in team.members
            if getattr(member, "name", None) and callable(getattr(member, "run", None))
        }
        working_prompt = enriched_prompt or prompt
        member_outputs: list[dict[str, Any]] = []
        selected_agents: list[str] = ["Enterprise Orchestrator", *prefetched_agents]
        notes = list(routing_plan.notes)
        delegated_results: dict[str, RunOutput] = {}
        evidence_blocks: list[dict[str, Any]] = []
        required_agents = self._ordered_required_agents(routing_plan.required_agents)
        available_agents = self._available_orchestration_agents(member_agents, effective_agent_payload)
        available_agent_map = {item["name"]: item for item in available_agents}
        max_iterations = self._max_orchestration_iterations(member_agents)
        iteration_count = 0
        stop_reason: str | None = None
        retry_missing_agents: list[str] | None = None
        final_answer = ""
        observation_history: list[dict[str, Any]] = []

        for item in prefetched_member_outputs:
            step = self._make_orchestration_step(
                name=item["name"],
                phase=item.get("phase", "prefetch"),
                content=item["content"],
                target_agent=item["name"] if item["name"] != "Enterprise Orchestrator" else None,
            )
            member_outputs.append(step)
            evidence_blocks.append(
                {
                    "name": item["name"],
                    "phase": item.get("phase", "prefetch"),
                    "content": item["content"],
                }
            )

        member_outputs.append(
            self._make_orchestration_step(
                name="Enterprise Orchestrator",
                phase="plan",
                content=self._summarize_plan(routing_plan),
            )
        )

        unavailable_required_agents = [
            agent_name for agent_name in required_agents if agent_name not in member_agents
        ]
        if unavailable_required_agents:
            notes.append(f"required_agent_unavailable={','.join(unavailable_required_agents)}")
            failure_text, blocked_outputs = self._build_gate_failure(
                unavailable_required_agents[0],
                delegated_outputs=member_outputs,
            )
            blocked_outputs[-1].update(
                {
                    "status": "blocked",
                    "step_type": "gate_block",
                    "stop_reason": "required_agent_unavailable",
                }
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
                iteration_count=0,
                stop_reason="required_agent_unavailable",
                orchestration_steps=[dict(item) for item in blocked_outputs],
            )

        for iteration in range(1, max_iterations + 1):
            iteration_count = iteration
            pending_required_agents = [
                agent_name
                for agent_name in required_agents
                if not self._agent_has_required_evidence(agent_name, delegated_results.get(agent_name))
            ]
            decision = self._decide_orchestration_step(
                ctx=ctx,
                prompt=prompt,
                effective_prompt=working_prompt,
                routing_plan=routing_plan,
                available_agents=available_agents,
                evidence_blocks=evidence_blocks,
                iteration=iteration,
                max_iterations=max_iterations,
                model=team.model,
                notes=notes,
                pending_required_agents=pending_required_agents,
                retry_missing_agents=retry_missing_agents,
            )
            target_agent = str(decision.target_agent or "").strip() or None
            delegate_instruction = str(decision.delegate_instruction or "").strip() or None
            decision_content_lines = [
                f"action={decision.action}",
                f"pending_required_agents={','.join(pending_required_agents) or 'none'}",
                f"rationale={decision.rationale}",
            ]
            if target_agent:
                decision_content_lines.append(f"target_agent={target_agent}")
            if delegate_instruction:
                decision_content_lines.append(f"delegate_instruction={delegate_instruction}")
            if decision.action == "finalize":
                decision_content_lines.append(
                    f"stop_reason={str(decision.stop_reason or '').strip() or 'none'}"
                )
            member_outputs.append(
                self._make_orchestration_step(
                    name="Enterprise Orchestrator",
                    phase="decision",
                    content="\n".join(decision_content_lines),
                    iteration=iteration,
                    target_agent=target_agent,
                    stop_reason=(
                        str(decision.stop_reason or "").strip() or None
                        if decision.action == "finalize"
                        else None
                    ),
                )
            )

            if decision.action == "finalize":
                if pending_required_agents:
                    retry_missing_agents = list(pending_required_agents)
                    notes.append(
                        "orchestrator_finalize_blocked_missing_required="
                        + ",".join(pending_required_agents)
                    )
                    continue
                final_answer = str(decision.final_answer or "").strip()
                if not final_answer:
                    final_answer = "当前没有拿到足够的成员结果，暂时无法形成最终答复。"
                    notes.append("orchestrator_empty_finalize_fallback")
                stop_reason = str(decision.stop_reason or "").strip() or (
                    "direct_response" if not evidence_blocks else "sufficient_evidence"
                )
                member_outputs.append(
                    self._make_orchestration_step(
                        name="Enterprise Orchestrator",
                        phase="finalize",
                        content=final_answer,
                        iteration=iteration,
                        stop_reason=stop_reason,
                    )
                )
                break

            if target_agent is None:
                retry_missing_agents = list(pending_required_agents)
                notes.append("orchestrator_delegate_missing_target")
                continue

            agent = member_agents.get(target_agent)
            if agent is None:
                retry_missing_agents = list(pending_required_agents)
                notes.append(f"orchestrator_delegate_unknown_agent={target_agent}")
                continue

            delegate_instruction = self._default_delegate_instruction(
                target_agent,
                delegate_instruction or str(decision.rationale or "").strip(),
            )
            agent_descriptor = available_agent_map.get(target_agent, {})
            delegate_payload = self._build_delegate_payload(
                agent_name=target_agent,
                ctx=ctx,
                original_user_message=prompt,
                delegate_instruction=delegate_instruction,
                evidence_blocks=evidence_blocks,
                iteration=iteration,
                allowed_tools=self._delegate_allowed_tools(target_agent, agent_descriptor),
                agent_role=str(agent_descriptor.get("role") or ""),
                prior_attempts=self._recent_attempts_for_agent(observation_history, target_agent),
            )
            member_outputs.append(
                self._make_orchestration_step(
                    name="Enterprise Orchestrator",
                    phase="delegate",
                    content=(
                        f"target_agent={target_agent}\n"
                        f"instruction={delegate_instruction}\n"
                        f"delegate_payload={json.dumps(self._sanitize_delegate_payload_for_trace(delegate_payload), ensure_ascii=False)}"
                    ),
                    iteration=iteration,
                    target_agent=target_agent,
                    status="started",
                    delegate_payload=self._sanitize_delegate_payload_for_trace(delegate_payload),
                    payload_signature=delegate_payload.get("payload_signature"),
                )
            )
            delegate_prompt = (
                f"{prompt}\n\n[Enterprise Orchestrator 本轮委托]\n{delegate_instruction}"
            ).strip()
            try:
                run_output = self._run_delegate_agent(
                    agent,
                    agent_name=target_agent,
                    prompt=delegate_prompt,
                    ctx=ctx,
                    evidence_blocks=evidence_blocks,
                    delegate_payload=delegate_payload,
                    healthy_aliases=healthy_aliases,
                )
            except Exception as exc:
                run_output = RunOutput(
                    agent_name=target_agent,
                    content=f"{target_agent} 执行失败: {exc}",
                )
            delegated_results[target_agent] = run_output
            tool_names = self._tool_names_from_run_output(run_output)
            tool_summary = ", ".join(tool_names) if tool_names else "none"
            content = str(getattr(run_output, "content", "") or "").strip() or "未返回内容。"
            run_metadata = dict(getattr(run_output, "metadata", {}) or {})
            observe_status = str(
                run_metadata.get("status")
                or (
                    "success"
                    if self._agent_has_required_evidence(target_agent, run_output) or bool(content.strip())
                    else "empty"
                )
            ).strip()
            reason_code = str(run_metadata.get("reason_code") or "").strip() or None
            payload_signature = str(
                run_metadata.get("delegate_payload_signature") or delegate_payload.get("payload_signature") or ""
            ).strip() or None
            observation_history.append(
                {
                    "agent_name": target_agent,
                    "status": observe_status,
                    "reason_code": reason_code,
                    "payload_signature": payload_signature,
                    "content": content,
                    "next_action_suggestion": run_metadata.get("next_action_suggestion"),
                }
            )
            observe_status = (
                "completed"
                if observe_status in {"success", "needs_clarification", "policy_blocked"}
                else observe_status
            )
            member_outputs.append(
                self._make_orchestration_step(
                    name=target_agent,
                    phase="observe",
                    content=f"tool_evidence={tool_summary}\n{content}",
                    iteration=iteration,
                    target_agent=target_agent,
                    status=observe_status,
                    tool_evidence=tool_names,
                    reason_code=reason_code,
                    detected_intent=run_metadata.get("detected_intent"),
                    resolved_relative_path=run_metadata.get("resolved_relative_path"),
                    next_action_suggestion=run_metadata.get("next_action_suggestion"),
                    delegate_payload=run_metadata.get("delegate_payload"),
                    payload_signature=payload_signature,
                    tool_calls=run_metadata.get("tool_calls"),
                    used_default_path=run_metadata.get("used_default_path"),
                )
            )
            evidence_blocks.append(
                {
                    "name": target_agent,
                    "phase": "observe",
                    "content": content,
                    "status": run_metadata.get("status"),
                    "reason_code": reason_code,
                    "detected_intent": run_metadata.get("detected_intent"),
                    "resolved_relative_path": run_metadata.get("resolved_relative_path"),
                    "next_action_suggestion": run_metadata.get("next_action_suggestion"),
                    "tool_calls": run_metadata.get("tool_calls"),
                }
            )
            selected_agents.append(target_agent)
            if target_agent == "Knowledge Agent":
                knowledge_hits = list((getattr(run_output, "metadata", {}) or {}).get("knowledge_hits") or [])
                if knowledge_hits:
                    captured_hits[:] = self._dedupe_knowledge_hits([*captured_hits, *knowledge_hits])
            repeated_failure_streak = self._repeat_failure_streak(
                observation_history,
                agent_name=target_agent,
                reason_code=reason_code,
                payload_signature=payload_signature,
            )
            if run_metadata.get("status") == "needs_clarification":
                final_answer = content
                stop_reason = "needs_clarification"
                member_outputs.append(
                    self._make_orchestration_step(
                        name="Enterprise Orchestrator",
                        phase="finalize",
                        content=final_answer,
                        iteration=iteration,
                        stop_reason=stop_reason,
                        status="completed",
                    )
                )
                break
            if run_metadata.get("status") == "policy_blocked":
                failure_text = content
                stop_reason = "policy_blocked"
                member_outputs.append(
                    self._make_orchestration_step(
                        name="Enterprise Orchestrator",
                        phase="gate_block",
                        content=failure_text,
                        iteration=iteration,
                        stop_reason=stop_reason,
                        status="blocked",
                    )
                )
                selected_agents = list(dict.fromkeys(selected_agents))
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
                    iteration_count=iteration,
                    stop_reason=stop_reason,
                    orchestration_steps=[dict(item) for item in member_outputs],
                )
            if repeated_failure_streak >= 2 and reason_code:
                final_answer = content
                stop_reason = (
                    "needs_clarification"
                    if reason_code.startswith("missing_") or run_metadata.get("status") == "needs_clarification"
                    else "repeated_agent_failure"
                )
                notes.append(f"agent_failure_circuit_open={target_agent}:{reason_code}")
                member_outputs.append(
                    self._make_orchestration_step(
                        name="Enterprise Orchestrator",
                        phase="finalize" if stop_reason == "needs_clarification" else "gate_block",
                        content=final_answer,
                        iteration=iteration,
                        stop_reason=stop_reason,
                        status="completed" if stop_reason == "needs_clarification" else "blocked",
                    )
                )
                if stop_reason != "needs_clarification":
                    selected_agents = list(dict.fromkeys(selected_agents))
                    self._record_member_outputs(ctx, member_outputs)
                    return RunResult(
                        answer=final_answer,
                        mode="agent_gate_blocked",
                        selected_agents=selected_agents,
                        member_outputs=member_outputs,
                        knowledge_hits=captured_hits,
                        notes=notes,
                        model_routes=model_routes,
                        prefetch_info=prefetch_info,
                        effective_agents=effective_agent_payload,
                        iteration_count=iteration,
                        stop_reason=stop_reason,
                        orchestration_steps=[dict(item) for item in member_outputs],
                    )
                break
            if target_agent in pending_required_agents and not self._agent_has_required_evidence(
                target_agent,
                run_output,
            ):
                retry_missing_agents = [target_agent]
                notes.append(f"required_agent_missing_evidence_after_observe={target_agent}")
            else:
                retry_missing_agents = None

        if not final_answer:
            pending_required_agents = [
                agent_name
                for agent_name in required_agents
                if not self._agent_has_required_evidence(agent_name, delegated_results.get(agent_name))
            ]
            if pending_required_agents:
                notes.append(f"agent_gate_missing_evidence={','.join(pending_required_agents)}")
                failure_text, blocked_outputs = self._build_gate_failure(
                    pending_required_agents[0],
                    delegated_outputs=member_outputs,
                )
                blocked_outputs[-1].update(
                    {
                        "iteration": iteration_count,
                        "status": "blocked",
                        "step_type": "gate_block",
                        "stop_reason": "required_agent_evidence_missing",
                    }
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
                    iteration_count=iteration_count,
                    stop_reason="required_agent_evidence_missing",
                    orchestration_steps=[dict(item) for item in blocked_outputs],
                )
            stop_reason = stop_reason or "max_iterations_reached"
            final_answer = (
                f"Enterprise Orchestrator 在 {iteration_count} 轮内仍未收敛到最终答复，"
                "系统已停止，以避免在边界不清时继续推断。"
            )
            member_outputs.append(
                self._make_orchestration_step(
                    name="Enterprise Orchestrator",
                    phase="finalize",
                    content=final_answer,
                    iteration=iteration_count,
                    status="failed",
                    stop_reason=stop_reason,
                )
            )

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
                self._make_orchestration_step(
                    name="Enterprise Orchestrator",
                    phase="gate_block",
                    content=failure_text,
                    iteration=iteration_count,
                    status="blocked",
                    stop_reason="repo_listing_without_workspace_evidence",
                )
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
                iteration_count=iteration_count,
                stop_reason="repo_listing_without_workspace_evidence",
                orchestration_steps=[dict(item) for item in member_outputs],
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
            iteration_count=iteration_count,
            stop_reason=stop_reason,
            orchestration_steps=[dict(item) for item in member_outputs],
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
