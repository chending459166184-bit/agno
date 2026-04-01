from __future__ import annotations

from types import SimpleNamespace

from app.model_gateway.health import RuntimeHealthStatus
from app.runtime import OrchestratorRuntime, RunResult


class RecordingHealthChecker:
    def __init__(self, status: RuntimeHealthStatus) -> None:
        self.status = status
        self.calls: list[bool] = []

    def probe(self, *, force_refresh: bool = False) -> RuntimeHealthStatus:
        self.calls.append(force_refresh)
        return self.status


def test_runtime_run_reuses_cached_health_probe() -> None:
    runtime = object.__new__(OrchestratorRuntime)
    runtime.settings = SimpleNamespace(allow_mock_fallback=True)
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
