from __future__ import annotations

import time
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.context import RequestContext
from app.db import Database
from app.external_agents.broker import ExternalAgentBroker
from app.external_agents.registry import ExternalAgentRegistry
from app.external_agents.schemas import (
    A2AInvocationResult,
    AgentDiscoveryConfig,
    DiscoverySnapshot,
    DiscoverySourceRunResult,
    ExternalAgentCard,
    RemoteAgentCard,
)


class FakeDiscovery:
    def __init__(self, agents: list[ExternalAgentCard]) -> None:
        self.config = AgentDiscoveryConfig(refresh_ttl_seconds=60)
        self.agents = agents
        self.calls = 0

    def discover(self) -> DiscoverySnapshot:
        self.calls += 1
        now = time.time()
        return DiscoverySnapshot(
            agents=self.agents,
            sources=[
                DiscoverySourceRunResult(
                    source_id="fake-source",
                    transport="stdio",
                    discovered_count=len(self.agents),
                )
            ],
            fetched_at=now,
            expires_at=now + 60,
            from_cache=False,
        )


class FakeA2AClient:
    def fetch_agent_card(self, agent: ExternalAgentCard, **kwargs) -> RemoteAgentCard:
        return RemoteAgentCard(
            name=agent.name,
            description=agent.description,
            url=agent.message_url,
            version="1.0.0",
            skills=[{"id": capability, "name": capability} for capability in agent.capabilities],
        )

    def send_message(self, agent: ExternalAgentCard, **kwargs) -> A2AInvocationResult:
        return A2AInvocationResult(
            agent_id=agent.agent_id,
            task_id="task_demo",
            context_id=kwargs["session_id"],
            state="completed",
            text=f"{agent.name} handled the task.",
            raw={"ok": True},
            agent_card=self.fetch_agent_card(agent),
        )


def build_agent(agent_id: str, *, category: str, capabilities: list[str]) -> ExternalAgentCard:
    return ExternalAgentCard(
        agent_id=agent_id,
        source_id="fake-source",
        name=agent_id.replace("-", " ").title(),
        description=f"{category} specialist",
        category=category,
        capabilities=capabilities,
        tags=[category],
        card_url=f"http://example.test/{agent_id}/card",
        message_url=f"http://example.test/{agent_id}/send",
    )


def test_external_registry_caches_and_filters_candidates() -> None:
    discovery = FakeDiscovery(
        [
            build_agent("security-architect", category="security", capabilities=["audit-hardening"]),
            build_agent("analytics-copilot", category="analytics", capabilities=["metric-analysis"]),
        ]
    )
    registry = ExternalAgentRegistry(discovery)

    first = registry.refresh(force_refresh=False)
    second = registry.refresh(force_refresh=False)
    candidates = registry.find_candidates(category="security")

    assert first.from_cache is False
    assert second.from_cache is True
    assert discovery.calls == 1
    assert len(candidates) == 1
    assert candidates[0].agent_id == "security-architect"


def test_external_broker_records_selection_and_a2a_audit(tmp_path: Path) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    db = Database(tmp_path / "app.db")
    db.init_schema()
    discovery = FakeDiscovery(
        [
            build_agent("security-architect", category="security", capabilities=["audit-hardening"]),
            build_agent("analytics-copilot", category="analytics", capabilities=["metric-analysis"]),
        ]
    )
    registry = ExternalAgentRegistry(discovery)
    broker = ExternalAgentBroker(settings, db, registry, FakeA2AClient())
    ctx = RequestContext(
        trace_id="trace_external_test",
        request_id="req_external_test",
        session_id="session_external_test",
        tenant_id="demo",
        user_id="alice",
        display_name="Alice Chen",
        role="manager",
        project_id="alpha",
        workspace_root=Path(tmp_path / "workspace"),
    )

    result = broker.invoke(
        ctx=ctx,
        message="请从安全架构角度审查外部 A2A 引入后的隔离与审计风险。",
        category="security",
    )

    assert result.selected_agent.agent_id == "security-architect"
    assert result.response.state == "completed"
    events = db.list_audit_events(ctx.trace_id)
    event_types = [item["event_type"] for item in events]
    assert "external_agent_discovery" in event_types
    assert "external_agent_selected" in event_types
    assert "a2a_request_sent" in event_types
    assert "a2a_response_received" in event_types
