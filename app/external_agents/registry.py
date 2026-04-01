from __future__ import annotations

import time

from app.external_agents.discovery import ExternalAgentDiscovery
from app.external_agents.schemas import DiscoverySnapshot, ExternalAgentCard


class ExternalAgentRegistry:
    def __init__(self, discovery: ExternalAgentDiscovery) -> None:
        self.discovery = discovery
        self._snapshot: DiscoverySnapshot | None = None

    @property
    def config(self):
        return self.discovery.config

    def refresh(self, *, force_refresh: bool = False) -> DiscoverySnapshot:
        now = time.time()
        if (
            not force_refresh
            and self._snapshot is not None
            and now < self._snapshot.expires_at
        ):
            return self._snapshot.model_copy(update={"from_cache": True})
        snapshot = self.discovery.discover()
        self._snapshot = snapshot
        return snapshot

    def list_agents(
        self,
        *,
        force_refresh: bool = False,
        category: str | None = None,
        capability: str | None = None,
        name_query: str | None = None,
    ) -> DiscoverySnapshot:
        snapshot = self.refresh(force_refresh=force_refresh)
        agents = self._filter_agents(
            snapshot.agents,
            category=category,
            capability=capability,
            name_query=name_query,
        )
        return snapshot.model_copy(update={"agents": agents})

    def get_agent(self, agent_id: str, *, force_refresh: bool = False) -> ExternalAgentCard | None:
        snapshot = self.refresh(force_refresh=force_refresh)
        for agent in snapshot.agents:
            if agent.agent_id == agent_id:
                return agent
        return None

    def find_candidates(
        self,
        *,
        force_refresh: bool = False,
        category: str | None = None,
        capability: str | None = None,
        name_query: str | None = None,
        limit: int = 8,
    ) -> list[ExternalAgentCard]:
        snapshot = self.list_agents(
            force_refresh=force_refresh,
            category=category,
            capability=capability,
            name_query=name_query,
        )
        return snapshot.agents[:limit]

    def status(self) -> dict:
        snapshot = self.refresh(force_refresh=False)
        return {
            "cached": snapshot.from_cache,
            "fetched_at": snapshot.fetched_at,
            "expires_at": snapshot.expires_at,
            "count": len(snapshot.agents),
            "source_ids": [item.source_id for item in snapshot.sources],
            "errors": [
                {"source_id": item.source_id, "error": item.error}
                for item in snapshot.sources
                if item.error
            ],
        }

    def _filter_agents(
        self,
        agents: list[ExternalAgentCard],
        *,
        category: str | None,
        capability: str | None,
        name_query: str | None,
    ) -> list[ExternalAgentCard]:
        category = (category or "").strip().lower()
        capability = (capability or "").strip().lower()
        name_query = (name_query or "").strip().lower()
        filtered: list[ExternalAgentCard] = []
        for agent in agents:
            if category and agent.category.lower() != category:
                continue
            if capability and capability not in {item.lower() for item in agent.capabilities}:
                continue
            if name_query:
                haystack = " ".join(
                    [agent.agent_id, agent.name, agent.description, *agent.tags, *agent.capabilities]
                ).lower()
                if name_query not in haystack:
                    continue
            filtered.append(agent)
        return filtered

