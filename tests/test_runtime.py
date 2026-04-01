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
from app.runtime import OrchestratorRuntime, RunResult, TeamRoutingPlan


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
    def __init__(self, name=None, **kwargs) -> None:
        self.name = name or "Enterprise Orchestrator"
        self.kwargs = kwargs

    def run(self, prompt, **kwargs):
        return RunOutput(agent_name=self.name, content="最终整合答案")


def test_run_agno_explicitly_delegates_required_workspace_agent(monkeypatch) -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(telemetry_enabled=False)
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
    runtime._summarize_plan = OrchestratorRuntime._summarize_plan.__get__(runtime, OrchestratorRuntime)
    runtime._ordered_required_agents = OrchestratorRuntime._ordered_required_agents.__get__(runtime, OrchestratorRuntime)
    runtime._tool_names_from_run_output = OrchestratorRuntime._tool_names_from_run_output.__get__(runtime, OrchestratorRuntime)
    runtime._agent_has_required_evidence = OrchestratorRuntime._agent_has_required_evidence.__get__(runtime, OrchestratorRuntime)
    runtime._build_gate_failure = OrchestratorRuntime._build_gate_failure.__get__(runtime, OrchestratorRuntime)
    runtime._build_synthesizer_prompt = OrchestratorRuntime._build_synthesizer_prompt.__get__(runtime, OrchestratorRuntime)
    runtime._answer_looks_like_repo_listing = lambda ctx, answer: False
    recorded = []
    runtime._record_member_outputs = lambda ctx, outputs: recorded.extend(outputs)
    runtime._run_delegate_agent = lambda agent, agent_name, prompt, ctx, evidence_blocks, healthy_aliases=None: RunOutput(
        agent_name=agent_name,
        content="notes/customer-risk.md",
        tools=[ToolExecution(tool_name="workspace_list_files", result="ok")],
    )
    runtime.build_team = lambda ctx, healthy_aliases=None: (
        SimpleNamespace(members=[FakeAgent(name="Workspace Agent")], model="fake-model"),
        [],
        {"orchestrate": "coder-premium", "workspace": "coder-premium"},
        [],
    )
    ctx = SimpleNamespace(
        tenant_id="demo",
        user_id="alice",
        display_name="Alice",
        role="manager",
        project_id="alpha",
        session_id="session1",
    )

    original_agent = runtime_module.Agent
    monkeypatch.setattr(runtime_module, "Agent", FakeAgent)
    try:
        result = runtime.run_agno(ctx, "我空间有什么文件？", healthy_aliases={"coder-premium"})
    finally:
        monkeypatch.setattr(runtime_module, "Agent", original_agent)

    assert result.mode == "agno"
    assert "Workspace Agent" in result.selected_agents
    phases = [item["phase"] for item in result.member_outputs]
    assert "plan" in phases
    assert "delegate" in phases
    assert "synthesize" in phases
    assert result.answer == "最终整合答案"


def test_run_agno_blocks_when_required_workspace_evidence_is_missing(monkeypatch) -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(telemetry_enabled=False)
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
    runtime._summarize_plan = OrchestratorRuntime._summarize_plan.__get__(runtime, OrchestratorRuntime)
    runtime._ordered_required_agents = OrchestratorRuntime._ordered_required_agents.__get__(runtime, OrchestratorRuntime)
    runtime._tool_names_from_run_output = OrchestratorRuntime._tool_names_from_run_output.__get__(runtime, OrchestratorRuntime)
    runtime._agent_has_required_evidence = OrchestratorRuntime._agent_has_required_evidence.__get__(runtime, OrchestratorRuntime)
    runtime._build_gate_failure = OrchestratorRuntime._build_gate_failure.__get__(runtime, OrchestratorRuntime)
    runtime._build_synthesizer_prompt = OrchestratorRuntime._build_synthesizer_prompt.__get__(runtime, OrchestratorRuntime)
    runtime._answer_looks_like_repo_listing = lambda ctx, answer: False
    runtime._record_member_outputs = lambda ctx, outputs: None
    runtime._run_delegate_agent = lambda agent, agent_name, prompt, ctx, evidence_blocks, healthy_aliases=None: RunOutput(
        agent_name=agent_name,
        content="我先帮你看一下。",
        tools=[],
    )
    runtime.build_team = lambda ctx, healthy_aliases=None: (
        SimpleNamespace(members=[FakeAgent(name="Workspace Agent")], model="fake-model"),
        [],
        {"orchestrate": "coder-premium", "workspace": "coder-premium"},
        [],
    )
    ctx = SimpleNamespace(
        tenant_id="demo",
        user_id="alice",
        display_name="Alice",
        role="manager",
        project_id="alpha",
        session_id="session1",
    )

    original_agent = runtime_module.Agent
    monkeypatch.setattr(runtime_module, "Agent", FakeAgent)
    try:
        result = runtime.run_agno(ctx, "我空间有什么文件？", healthy_aliases={"coder-premium"})
    finally:
        monkeypatch.setattr(runtime_module, "Agent", original_agent)

    assert result.mode == "agent_gate_blocked"
    assert "Workspace Agent" in result.answer


def test_run_workspace_delegate_uses_real_mcp_helper(monkeypatch) -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace()
    runtime._classify_workspace_access = lambda prompt, healthy_aliases=None: {
        "requires_workspace_access": True,
        "action": "list",
        "path": None,
        "content": None,
        "source": "heuristic",
        "reason": "matched_explicit_workspace_pattern",
    }
    runtime._sanitize_workspace_guard_payload = OrchestratorRuntime._sanitize_workspace_guard_payload.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._build_workspace_delegate_content = OrchestratorRuntime._build_workspace_delegate_content.__get__(
        runtime, OrchestratorRuntime
    )
    runtime._make_tool_execution = OrchestratorRuntime._make_tool_execution.__get__(runtime, OrchestratorRuntime)

    monkeypatch.setattr(
        runtime_module,
        "call_workspace_mcp_tool",
        lambda settings, ctx, tool_name, arguments: {
            "root": str(ctx.workspace_root),
            "files": [{"path": "notes/alpha-analysis.md"}],
        },
    )
    ctx = SimpleNamespace(workspace_root=Path("/tmp/demo-workspace"))

    result = runtime._run_workspace_delegate(ctx, "我空间有什么文件？", healthy_aliases={"coder-premium"})

    assert result.tools is not None
    assert result.tools[0].tool_name == "workspace_list_files"
    assert "notes/alpha-analysis.md" in result.content
