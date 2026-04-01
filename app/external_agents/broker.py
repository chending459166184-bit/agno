from __future__ import annotations

from typing import Iterable

from app.config import Settings
from app.context import RequestContext
from app.db import Database
from app.db import tokenize
from app.external_agents.a2a_client import A2AClient
from app.external_agents.registry import ExternalAgentRegistry
from app.external_agents.schemas import (
    BrokerInvocationResult,
    BrokerSelection,
    DiscoverySnapshot,
    ExternalAgentCard,
)


class ExternalAgentBroker:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        registry: ExternalAgentRegistry,
        a2a_client: A2AClient,
    ) -> None:
        self.settings = settings
        self.database = database
        self.registry = registry
        self.a2a_client = a2a_client

    def refresh_agents(self, ctx: RequestContext | None = None) -> DiscoverySnapshot:
        snapshot = self.registry.refresh(force_refresh=True)
        if ctx is not None:
            self.database.record_external_agent_discovery(
                ctx,
                agent_count=len(snapshot.agents),
                from_cache=snapshot.from_cache,
                source_results=[item.model_dump() for item in snapshot.sources],
            )
        return snapshot

    def list_agents(
        self,
        *,
        ctx: RequestContext | None = None,
        force_refresh: bool = False,
        category: str | None = None,
        capability: str | None = None,
        name_query: str | None = None,
    ) -> DiscoverySnapshot:
        snapshot = self.registry.list_agents(
            force_refresh=force_refresh,
            category=category,
            capability=capability,
            name_query=name_query,
        )
        if ctx is not None:
            self.database.record_external_agent_discovery(
                ctx,
                agent_count=len(snapshot.agents),
                from_cache=snapshot.from_cache,
                source_results=[item.model_dump() for item in snapshot.sources],
            )
        return snapshot

    def invoke(
        self,
        *,
        ctx: RequestContext,
        message: str,
        agent_id: str | None = None,
        category: str | None = None,
        capability: str | None = None,
        preferred_name: str | None = None,
        force_refresh: bool = False,
        metadata: dict | None = None,
    ) -> BrokerInvocationResult:
        snapshot = self.list_agents(
            ctx=ctx,
            force_refresh=force_refresh,
            category=category,
            capability=capability,
            name_query=preferred_name,
        )
        candidates = snapshot.agents
        if agent_id:
            selected = next((item for item in snapshot.agents if item.agent_id == agent_id), None)
            if selected is None:
                selected = self.registry.get_agent(agent_id, force_refresh=force_refresh)
                if selected is not None:
                    candidates = [selected, *candidates]
            if selected is None:
                raise ValueError(f"未找到 external agent: {agent_id}")
        else:
            if not candidates:
                raise ValueError("当前没有可用 external agents")
            selected = self._choose_agent(
                candidates,
                category=category,
                capability=capability,
                preferred_name=preferred_name,
                message=message,
            )
        selection = BrokerSelection(
            agent_id=selected.agent_id,
            score=self._score_agent(
                selected,
                category=category,
                capability=capability,
                preferred_name=preferred_name,
                message=message,
            ),
            reason=self._selection_reason(
                selected,
                category=category,
                capability=capability,
                preferred_name=preferred_name,
            ),
            candidate_agent_ids=[item.agent_id for item in candidates[:8]],
            matched_capabilities=[
                item for item in selected.capabilities if capability and item.lower() == capability.lower()
            ],
        )
        self.database.record_external_agent_selected(
            ctx,
            selected_agent_id=selected.agent_id,
            selection=selection.model_dump(),
        )
        try:
            agent_card = self.a2a_client.fetch_agent_card(selected)
            self.database.record_a2a_request_sent(
                ctx,
                agent_id=selected.agent_id,
                payload={
                    "message_excerpt": message[:240],
                    "category": category,
                    "capability": capability,
                    "preferred_name": preferred_name,
                    "message_url": selected.message_url,
                    "card_url": selected.card_url,
                },
            )
            response = self.a2a_client.send_message(
                selected,
                message=message,
                trace_id=ctx.trace_id,
                request_id=ctx.request_id,
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                project_id=ctx.project_id,
                metadata=metadata,
                agent_card=agent_card,
            )
            self.database.record_a2a_response_received(
                ctx,
                agent_id=selected.agent_id,
                payload={
                    "task_id": response.task_id,
                    "context_id": response.context_id,
                    "state": response.state,
                    "text_excerpt": response.text[:240],
                },
            )
            return BrokerInvocationResult(
                selected_agent=selected,
                selection=selection,
                response=response,
                candidates=candidates,
            )
        except Exception as exc:
            self.database.record_a2a_error(
                ctx,
                agent_id=selected.agent_id,
                payload={"error": str(exc), "message_excerpt": message[:240]},
            )
            raise

    def format_agents_summary(self, snapshot: DiscoverySnapshot) -> str:
        if not snapshot.agents:
            return "当前没有发现可用 external agents。"
        lines = []
        for agent in snapshot.agents:
            capabilities = ", ".join(agent.capabilities) or "none"
            lines.append(
                f"- {agent.agent_id} | {agent.name} | category={agent.category} | capabilities={capabilities}"
            )
        return "\n".join(lines)

    def format_invocation_result(self, result: BrokerInvocationResult) -> str:
        agent = result.selected_agent
        return (
            f"已委托 external agent `{agent.agent_id}` ({agent.name})。\n"
            f"- category: {agent.category}\n"
            f"- selection_reason: {result.selection.reason}\n"
            f"- response:\n{result.response.text}"
        )

    def _choose_agent(
        self,
        candidates: Iterable[ExternalAgentCard],
        *,
        category: str | None,
        capability: str | None,
        preferred_name: str | None,
        message: str,
    ) -> ExternalAgentCard:
        ranked = sorted(
            candidates,
            key=lambda item: (
                -self._score_agent(
                    item,
                    category=category,
                    capability=capability,
                    preferred_name=preferred_name,
                    message=message,
                ),
                item.name.lower(),
            ),
        )
        return ranked[0]

    def _score_agent(
        self,
        agent: ExternalAgentCard,
        *,
        category: str | None,
        capability: str | None,
        preferred_name: str | None,
        message: str,
    ) -> int:
        score = 0
        if category and agent.category.lower() == category.lower():
            score += 100
        if capability:
            capabilities = {item.lower() for item in agent.capabilities}
            if capability.lower() in capabilities:
                score += 80
        if preferred_name:
            needle = preferred_name.lower()
            haystack = f"{agent.agent_id} {agent.name} {agent.description}".lower()
            if needle in haystack:
                score += 60
        message_tokens = tokenize(message)
        agent_tokens = tokenize(
            " ".join([agent.name, agent.description, agent.category, *agent.capabilities, *agent.tags])
        )
        score += len(message_tokens & agent_tokens)
        return score

    def _selection_reason(
        self,
        agent: ExternalAgentCard,
        *,
        category: str | None,
        capability: str | None,
        preferred_name: str | None,
    ) -> str:
        reasons: list[str] = []
        if category and agent.category.lower() == category.lower():
            reasons.append("matched_category")
        if capability and capability.lower() in {item.lower() for item in agent.capabilities}:
            reasons.append("matched_capability")
        if preferred_name:
            haystack = f"{agent.agent_id} {agent.name}".lower()
            if preferred_name.lower() in haystack:
                reasons.append("matched_name")
        return ",".join(reasons) if reasons else "best_scored_candidate"

