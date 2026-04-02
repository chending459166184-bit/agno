from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.model_gateway.health import RuntimeHealthStatus
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
import app.runtime as runtime_module
from app.runtime import OrchestratorDecision, OrchestratorRuntime, RunResult, TeamRoutingPlan


class RecordingHealthChecker:
    def __init__(self, status: RuntimeHealthStatus) -> None:
        self.status = status
        self.calls: list[bool] = []

    def probe(self, *, force_refresh: bool = False) -> RuntimeHealthStatus:
        self.calls.append(force_refresh)
        return self.status


def test_runtime_run_reuses_cached_health_probe() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(
        allow_mock_fallback=True,
        execution_guard_enabled=False,
        workspace_guard_enabled=False,
    )
    runtime.health_checker = RecordingHealthChecker(
        RuntimeHealthStatus(
            live=True,
            proxy_reachable=True,
            proxy_base_url="http://127.0.0.1:4000",
            reason="ok",
            aliases=[],
        )
    )

    expected = RunResult(
        answer="live",
        mode="agno",
        selected_agents=["Enterprise Orchestrator"],
        member_outputs=[],
        knowledge_hits=[],
        notes=[],
        model_routes={"orchestrate": "coder-premium"},
    )

    runtime.run_agno = lambda ctx, prompt, healthy_aliases=None: expected
    runtime.run_mock = lambda ctx, prompt: (_ for _ in ()).throw(
        AssertionError("run_mock should not be used when live health is available")
    )

    result = runtime.run(ctx=object(), prompt="hello")

    assert result is expected
    assert runtime.health_checker.calls == [False]


def test_runtime_skips_pre_guards_when_disabled() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(
        allow_mock_fallback=True,
        execution_guard_enabled=False,
        workspace_guard_enabled=False,
    )
    runtime.health_checker = RecordingHealthChecker(
        RuntimeHealthStatus(
            live=True,
            proxy_reachable=True,
            proxy_base_url="http://127.0.0.1:4000",
            reason="ok",
            aliases=[],
        )
    )
    runtime.run_mock = lambda ctx, prompt: (_ for _ in ()).throw(
        AssertionError("run_mock should not be used when live health is available")
    )
    runtime._run_execution_guard = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("execution guard should not run when disabled")
    )
    runtime._run_workspace_guard = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("workspace guard should not run when disabled")
    )
    expected = RunResult(
        answer="live",
        mode="agno",
        selected_agents=["Enterprise Orchestrator"],
        member_outputs=[],
        knowledge_hits=[],
        notes=[],
        model_routes={"orchestrate": "coder-premium"},
    )
    runtime.run_agno = lambda ctx, prompt, healthy_aliases=None: expected

    result = runtime.run(ctx=object(), prompt="我空间有什么文件？")

    assert result is expected
    assert runtime.health_checker.calls == [False]


def test_build_team_routing_plan_prefers_workspace_and_execution_agents() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime._classify_workspace_access = lambda prompt, healthy_aliases=None: {
        "requires_workspace_access": True,
        "source": "classifier",
        "reason": "workspace",
    }
    runtime._classify_execution_request = lambda prompt, healthy_aliases=None: {
        "requires_execution": True,
        "source": "classifier",
        "reason": "execution",
    }

    plan = runtime._build_team_routing_plan(
        "请看看我空间里的文件，并运行一个脚本验证结果",
        healthy_aliases={"coder-premium"},
    )

    assert "Workspace Agent" in plan.required_agents
    assert "Execution Agent" in plan.required_agents
    assert any("Workspace Agent" in hint for hint in plan.hints)
    assert any("Execution Agent" in hint for hint in plan.hints)


class FakeAgent:
    def __init__(self, name=None, role=None, **kwargs) -> None:
        self.name = name or "Enterprise Orchestrator"
        self.role = role or ""
        self.kwargs = kwargs

    def run(self, prompt, **kwargs):
        return RunOutput(agent_name=self.name, content="最终整合答案")


def bind_runtime_loop_helpers(runtime: OrchestratorRuntime) -> None:
    runtime._summarize_plan = OrchestratorRuntime._summarize_plan.__get__(runtime, OrchestratorRuntime)
    runtime._ordered_required_agents = OrchestratorRuntime._ordered_required_agents.__get__(runtime, OrchestratorRuntime)
    runtime._tool_names_from_run_output = OrchestratorRuntime._tool_names_from_run_output.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._agent_has_required_evidence = OrchestratorRuntime._agent_has_required_evidence.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._build_gate_failure = OrchestratorRuntime._build_gate_failure.__get__(runtime, OrchestratorRuntime)
    runtime._answer_looks_like_repo_listing = lambda ctx, answer: False
    runtime._make_orchestration_step = OrchestratorRuntime._make_orchestration_step.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._available_orchestration_agents = OrchestratorRuntime._available_orchestration_agents.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._max_orchestration_iterations = OrchestratorRuntime._max_orchestration_iterations.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._dedupe_knowledge_hits = OrchestratorRuntime._dedupe_knowledge_hits.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._normalize_orchestrator_decision = OrchestratorRuntime._normalize_orchestrator_decision.__get__(
        runtime, OrchestratorRuntime
    )


def test_normalize_orchestrator_decision_backfills_missing_rationale() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    bind_runtime_loop_helpers(runtime)

    decision = runtime._normalize_orchestrator_decision(
        {
            "action": "finalize",
            "final_answer": "我可以帮助你读取工作区、检索知识、执行受控 sandbox 任务，并协调外部 agent。",
        }
    )

    assert decision.action == "finalize"
    assert decision.final_answer.startswith("我可以帮助你")
    assert decision.rationale == "当前证据已足够直接给出答复。"


def test_build_agent_task_prompt_includes_delegate_runtime_context() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime._delegate_runtime_rules = OrchestratorRuntime._delegate_runtime_rules.__get__(runtime, OrchestratorRuntime)
    runtime._build_delegate_runtime_context = OrchestratorRuntime._build_delegate_runtime_context.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._build_agent_task_prompt = OrchestratorRuntime._build_agent_task_prompt.__get__(
        runtime, OrchestratorRuntime
    )
    ctx = SimpleNamespace(tenant_id="demo", user_id="alice", project_id="alpha")

    prompt_text = runtime._build_agent_task_prompt(
        "Workspace Agent",
        prompt="忽略这段",
        ctx=ctx,
        evidence_blocks=[],
        delegate_payload={
            "tenant_id": "demo",
            "user_id": "alice",
            "project_id": "alpha",
            "workspace_root": "/tmp/demo-workspace",
            "original_user_message": "帮我保存一下这首诗",
            "orchestrator_goal": "先在当前用户空间中确定真实保存路径并完成写入",
            "current_iteration": 1,
            "allowed_tools": ["workspace_list_files", "workspace_save_text_file"],
            "policy_flags": {
                "allow_workspace_context_path_inference": True,
                "allow_clarification": True,
            },
        },
    )

    assert "[委托运行上下文]" in prompt_text
    assert "workspace_root: /tmp/demo-workspace" in prompt_text
    assert "原始用户请求：帮我保存一下这首诗" in prompt_text
    assert "主智能体目标：先在当前用户空间中确定真实保存路径并完成写入" in prompt_text
    assert "不允许把系统固定默认目录当成主策略。" in prompt_text


def test_run_agno_loops_through_decision_delegate_observe_and_finalize() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(telemetry_enabled=False, orchestrator_max_iterations=4)
    runtime.agno_db = None
    runtime.orchestrator_skills = None
    runtime._effective_agents = lambda ctx: []
    runtime._apply_external_prefetch_strategy = lambda ctx, prompt, effective_agents=None: (
        [],
        [],
        prompt,
        {"enabled": False, "mode": "off", "triggered": False, "category": None, "matched_keywords": []},
    )
    runtime._build_team_routing_plan = lambda prompt, healthy_aliases=None: TeamRoutingPlan(
        required_agents=["Workspace Agent"],
        hints=["use workspace"],
        notes=["routing_hint=Workspace Agent"],
    )
    bind_runtime_loop_helpers(runtime)
    recorded = []
    runtime._record_member_outputs = lambda ctx, outputs: recorded.extend(outputs)
    runtime._run_delegate_agent = lambda agent, agent_name, prompt, ctx, evidence_blocks, delegate_payload=None, healthy_aliases=None: RunOutput(
        agent_name=agent_name,
        content="notes/customer-risk.md",
        tools=[ToolExecution(tool_name="workspace_list_files", result="ok")],
    )
    decisions = iter(
        [
            OrchestratorDecision(
                action="delegate",
                rationale="先拿当前用户工作区的真实文件证据。",
                target_agent="Workspace Agent",
                delegate_instruction="请列出和当前请求最相关的文件。",
            ),
            OrchestratorDecision(
                action="finalize",
                rationale="已经拿到工作区证据，可以直接回答。",
                final_answer="我在你当前工作区里看到了 `notes/customer-risk.md`。",
                stop_reason="sufficient_evidence",
            ),
        ]
    )
    runtime._decide_orchestration_step = lambda **kwargs: next(decisions)
    runtime.build_team = lambda ctx, healthy_aliases=None: (
        SimpleNamespace(
            members=[FakeAgent(name="Workspace Agent", role="读取当前用户工作区真实文件")],
            model="fake-model",
        ),
        [],
        {"orchestrate": "coder-premium", "workspace": "coder-premium"},
        [
            {
                "display_name": "Workspace Agent",
                "description": "通过 Workspace MCP 读取当前用户工作区。",
                "priority": 70,
                "tool_summary": ["workspace MCPTools"],
            }
        ],
    )
    ctx = SimpleNamespace(
        tenant_id="demo",
        user_id="alice",
        display_name="Alice",
        role="manager",
        project_id="alpha",
        session_id="session1",
        workspace_root=Path("/tmp/demo-workspace"),
    )

    result = runtime.run_agno(ctx, "我空间有什么文件？", healthy_aliases={"coder-premium"})

    assert result.mode == "agno"
    assert "Workspace Agent" in result.selected_agents
    phases = [item["phase"] for item in result.member_outputs]
    assert "plan" in phases
    assert "delegate" in phases
    assert "observe" in phases
    assert "finalize" in phases
    assert "decision" in phases
    assert result.answer == "我在你当前工作区里看到了 `notes/customer-risk.md`。"
    assert result.iteration_count == 2
    assert result.stop_reason == "sufficient_evidence"
    assert result.orchestration_steps == result.member_outputs


def test_run_agno_blocks_when_required_workspace_evidence_is_missing() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(telemetry_enabled=False, orchestrator_max_iterations=1)
    runtime.agno_db = None
    runtime.orchestrator_skills = None
    runtime._effective_agents = lambda ctx: []
    runtime._apply_external_prefetch_strategy = lambda ctx, prompt, effective_agents=None: (
        [],
        [],
        prompt,
        {"enabled": False, "mode": "off", "triggered": False, "category": None, "matched_keywords": []},
    )
    runtime._build_team_routing_plan = lambda prompt, healthy_aliases=None: TeamRoutingPlan(
        required_agents=["Workspace Agent"],
        hints=["use workspace"],
        notes=["routing_hint=Workspace Agent"],
    )
    bind_runtime_loop_helpers(runtime)
    runtime._record_member_outputs = lambda ctx, outputs: None
    runtime._run_delegate_agent = lambda agent, agent_name, prompt, ctx, evidence_blocks, delegate_payload=None, healthy_aliases=None: RunOutput(
        agent_name=agent_name,
        content="我先帮你看一下。",
        tools=[],
    )
    runtime._decide_orchestration_step = lambda **kwargs: OrchestratorDecision(
        action="delegate",
        rationale="先调用 Workspace Agent。",
        target_agent="Workspace Agent",
        delegate_instruction="请列出当前工作区文件。",
    )
    runtime.build_team = lambda ctx, healthy_aliases=None: (
        SimpleNamespace(
            members=[FakeAgent(name="Workspace Agent", role="读取当前用户工作区真实文件")],
            model="fake-model",
        ),
        [],
        {"orchestrate": "coder-premium", "workspace": "coder-premium"},
        [
            {
                "display_name": "Workspace Agent",
                "description": "通过 Workspace MCP 读取当前用户工作区。",
                "priority": 70,
                "tool_summary": ["workspace MCPTools"],
            }
        ],
    )
    ctx = SimpleNamespace(
        tenant_id="demo",
        user_id="alice",
        display_name="Alice",
        role="manager",
        project_id="alpha",
        session_id="session1",
        workspace_root=Path("/tmp/demo-workspace"),
    )

    result = runtime.run_agno(ctx, "我空间有什么文件？", healthy_aliases={"coder-premium"})

    assert result.mode == "agent_gate_blocked"
    assert "Workspace Agent" in result.answer
    assert result.stop_reason == "required_agent_evidence_missing"
    phases = [item["phase"] for item in result.member_outputs]
    assert phases[-1] == "gate_block"


def test_run_workspace_delegate_uses_real_mcp_helper(monkeypatch) -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace()
    runtime.workspace_skills = None
    runtime.agno_db = None
    runtime.settings.telemetry_enabled = False
    runtime._sanitize_workspace_guard_payload = OrchestratorRuntime._sanitize_workspace_guard_payload.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._build_workspace_delegate_content = OrchestratorRuntime._build_workspace_delegate_content.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._build_workspace_agent_response_content = OrchestratorRuntime._build_workspace_agent_response_content.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._make_tool_execution = OrchestratorRuntime._make_tool_execution.__get__(runtime, OrchestratorRuntime)
    runtime._build_delegate_payload = OrchestratorRuntime._build_delegate_payload.__get__(runtime, OrchestratorRuntime)
    runtime._delegate_policy_flags = OrchestratorRuntime._delegate_policy_flags.__get__(runtime, OrchestratorRuntime)
    runtime._sanitize_delegate_payload_for_trace = OrchestratorRuntime._sanitize_delegate_payload_for_trace.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._heuristic_workspace_task_plan = OrchestratorRuntime._heuristic_workspace_task_plan.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._workspace_content_category = OrchestratorRuntime._workspace_content_category.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._slugify_filename_seed = OrchestratorRuntime._slugify_filename_seed.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._infer_workspace_write_path = OrchestratorRuntime._infer_workspace_write_path.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._plan_workspace_delegate = lambda agent, ctx, delegate_payload, healthy_aliases=None: runtime._heuristic_workspace_task_plan(delegate_payload)
    runtime._detect_workspace_guard = OrchestratorRuntime._detect_workspace_guard.__get__(runtime, OrchestratorRuntime)
    runtime._json_signature = OrchestratorRuntime._json_signature.__get__(runtime, OrchestratorRuntime)

    monkeypatch.setattr(
        runtime_module,
        "call_workspace_mcp_tool",
        lambda settings, ctx, tool_name, arguments: {
            "root": str(ctx.workspace_root),
            "files": [{"path": "notes/alpha-analysis.md"}],
        },
    )
    ctx = SimpleNamespace(workspace_root=Path("/tmp/demo-workspace"))

    result = runtime._run_workspace_delegate(
        None,
        ctx,
        "我空间有什么文件？",
        healthy_aliases={"coder-premium"},
    )

    assert result.tools is not None
    assert result.tools[0].tool_name == "workspace_list_files"
    assert "notes/alpha-analysis.md" in result.content


def test_detect_workspace_guard_matches_save_content_without_path() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    result = OrchestratorRuntime._detect_workspace_guard(runtime, "帮我保存一下下面的内容：\n春天花会开\n")

    assert result is not None
    assert result["action"] == "write"
    assert result["path"] is None
    assert result["content"] == "春天花会开"


def test_execution_classifier_does_not_upgrade_workspace_save_to_execution() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace()
    runtime._detect_execution_guard = OrchestratorRuntime._detect_execution_guard.__get__(runtime, OrchestratorRuntime)
    runtime._detect_workspace_guard = OrchestratorRuntime._detect_workspace_guard.__get__(runtime, OrchestratorRuntime)

    result = runtime._classify_execution_request(
        "帮我保存一下下面的内容：\n春天花会开\n",
        healthy_aliases={"coder-premium"},
    )

    assert result["requires_execution"] is False
    assert result["source"] == "workspace_guard_preempted"


def test_run_workspace_delegate_infers_workspace_path_when_filename_missing(monkeypatch) -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(telemetry_enabled=False)
    runtime.workspace_skills = None
    runtime.agno_db = None
    runtime._sanitize_workspace_guard_payload = OrchestratorRuntime._sanitize_workspace_guard_payload.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._build_workspace_delegate_content = OrchestratorRuntime._build_workspace_delegate_content.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._build_workspace_agent_response_content = OrchestratorRuntime._build_workspace_agent_response_content.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._make_tool_execution = OrchestratorRuntime._make_tool_execution.__get__(runtime, OrchestratorRuntime)
    runtime._build_delegate_payload = OrchestratorRuntime._build_delegate_payload.__get__(runtime, OrchestratorRuntime)
    runtime._delegate_policy_flags = OrchestratorRuntime._delegate_policy_flags.__get__(runtime, OrchestratorRuntime)
    runtime._sanitize_delegate_payload_for_trace = OrchestratorRuntime._sanitize_delegate_payload_for_trace.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._heuristic_workspace_task_plan = OrchestratorRuntime._heuristic_workspace_task_plan.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._workspace_content_category = OrchestratorRuntime._workspace_content_category.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._slugify_filename_seed = OrchestratorRuntime._slugify_filename_seed.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._infer_workspace_write_path = OrchestratorRuntime._infer_workspace_write_path.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._plan_workspace_delegate = lambda agent, ctx, delegate_payload, healthy_aliases=None: runtime._heuristic_workspace_task_plan(delegate_payload)
    runtime._detect_workspace_guard = OrchestratorRuntime._detect_workspace_guard.__get__(runtime, OrchestratorRuntime)
    runtime._json_signature = OrchestratorRuntime._json_signature.__get__(runtime, OrchestratorRuntime)

    captured_calls = []

    def _fake_call_workspace_mcp_tool(settings, ctx, tool_name, arguments):
        captured_calls.append((tool_name, dict(arguments)))
        if tool_name == "workspace_list_files":
            return {
                "root": str(ctx.workspace_root),
                "files": [{"path": "poems/existing-poem.txt"}],
            }
        return {"root": str(ctx.workspace_root), "path": arguments["path"], "size": len(arguments["content"])}

    monkeypatch.setattr(runtime_module, "call_workspace_mcp_tool", _fake_call_workspace_mcp_tool)
    ctx = SimpleNamespace(
        tenant_id="demo",
        user_id="alice",
        display_name="Alice",
        role="manager",
        project_id="alpha",
        workspace_root=Path("/tmp/demo-workspace"),
    )

    result = runtime._run_workspace_delegate(
        None,
        ctx,
        "帮我保存一下这首诗：\n春天花会开\n",
        healthy_aliases={"coder-premium"},
    )

    assert result.metadata["status"] == "success"
    assert result.metadata["reason_code"] == "write_with_inferred_workspace_path"
    assert result.metadata["detected_intent"] == "write_file"
    assert len(captured_calls) == 2
    assert captured_calls[0][0] == "workspace_list_files"
    assert captured_calls[1][0] == "workspace_save_text_file"
    assert captured_calls[1][1]["path"].startswith("poems/poem-")
    assert captured_calls[1][1]["content"] == "春天花会开"
    assert len(result.tools or []) == 2
    assert "已把内容保存到你当前工作区的 `poems/poem-" in result.content


def test_run_agno_opens_circuit_after_repeated_workspace_failure() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(telemetry_enabled=False, orchestrator_max_iterations=6)
    runtime.agno_db = None
    runtime.orchestrator_skills = None
    runtime._effective_agents = lambda ctx: []
    runtime._apply_external_prefetch_strategy = lambda ctx, prompt, effective_agents=None: (
        [],
        [],
        prompt,
        {"enabled": False, "mode": "off", "triggered": False, "category": None, "matched_keywords": []},
    )
    runtime._build_team_routing_plan = lambda prompt, healthy_aliases=None: TeamRoutingPlan(
        required_agents=["Workspace Agent"],
        hints=["use workspace"],
        notes=["routing_hint=Workspace Agent"],
    )
    bind_runtime_loop_helpers(runtime)
    runtime._record_member_outputs = lambda ctx, outputs: None
    runtime._decide_orchestration_step = lambda **kwargs: OrchestratorDecision(
        action="delegate",
        rationale="先调用 Workspace Agent。",
        target_agent="Workspace Agent",
        delegate_instruction="请处理当前工作区请求。",
    )
    runtime._run_delegate_agent = lambda agent, agent_name, prompt, ctx, evidence_blocks, delegate_payload=None, healthy_aliases=None: RunOutput(
        agent_name=agent_name,
        content="Workspace Agent 在调用 Workspace MCP 时失败。\n- reason_code: workspace_tool_error\n- error: timeout",
        tools=[],
        metadata={
            "status": "tool_error",
            "reason_code": "workspace_tool_error",
            "delegate_payload_signature": (delegate_payload or {}).get("payload_signature"),
        },
    )
    runtime.build_team = lambda ctx, healthy_aliases=None: (
        SimpleNamespace(
            members=[FakeAgent(name="Workspace Agent", role="读取当前用户工作区真实文件")],
            model="fake-model",
        ),
        [],
        {"orchestrate": "coder-premium", "workspace": "coder-premium"},
        [
            {
                "display_name": "Workspace Agent",
                "description": "通过 Workspace MCP 读取当前用户工作区。",
                "priority": 70,
                "tool_summary": ["workspace MCPTools"],
            }
        ],
    )
    ctx = SimpleNamespace(
        tenant_id="demo",
        user_id="alice",
        display_name="Alice",
        role="manager",
        project_id="alpha",
        session_id="session1",
        workspace_root=Path("/tmp/demo-workspace"),
    )

    result = runtime.run_agno(ctx, "我空间有什么文件？", healthy_aliases={"coder-premium"})

    assert result.mode == "agent_gate_blocked"
    assert result.stop_reason == "repeated_agent_failure"
    assert result.iteration_count == 2
    assert any(item["phase"] == "gate_block" for item in result.member_outputs)
    assert any("agent_failure_circuit_open=Workspace Agent:workspace_tool_error" in note for note in result.notes)
