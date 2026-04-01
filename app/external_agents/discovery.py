from __future__ import annotations

import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import yaml
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.config import Settings
from app.external_agents.schemas import (
    AgentDiscoveryConfig,
    DiscoverySnapshot,
    DiscoverySourceConfig,
    DiscoverySourceRunResult,
    ExternalAgentCard,
)


class ExternalAgentDiscovery:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = self._load_config(settings.resolved_agent_discovery_config)

    def _load_config(self, path: Path) -> AgentDiscoveryConfig:
        if not path.exists():
            raise FileNotFoundError(f"未找到 external discovery 配置: {path}")
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"external discovery 配置格式无效: {path}")
        return AgentDiscoveryConfig.model_validate(payload)

    def discover(self) -> DiscoverySnapshot:
        source_runs: list[DiscoverySourceRunResult] = []
        collected: list[ExternalAgentCard] = []
        for source in self.config.sources:
            try:
                payload = anyio.run(self._discover_from_source, source)
                discovered = [
                    ExternalAgentCard.model_validate(item)
                    for item in payload.get("agents", [])
                ]
                collected.extend(discovered)
                source_runs.append(
                    DiscoverySourceRunResult(
                        source_id=source.source_id,
                        transport=source.transport,
                        discovered_count=len(discovered),
                    )
                )
            except Exception as exc:
                source_runs.append(
                    DiscoverySourceRunResult(
                        source_id=source.source_id,
                        transport=source.transport,
                        error=str(exc),
                    )
                )
        agents = self._apply_filters(self._dedupe(collected))
        now = time.time()
        return DiscoverySnapshot(
            agents=agents,
            sources=source_runs,
            fetched_at=now,
            expires_at=now + self.config.refresh_ttl_seconds,
            from_cache=False,
        )

    async def _discover_from_source(self, source: DiscoverySourceConfig) -> dict[str, Any]:
        server = self._build_server_parameters(source)
        async with stdio_client(server) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool(
                    source.tool_name,
                    arguments={},
                    read_timeout_seconds=timedelta(seconds=source.timeout_seconds),
                )
        return self._extract_payload(result)

    def _build_server_parameters(self, source: DiscoverySourceConfig) -> StdioServerParameters:
        env = {
            key: self._resolve_env_value(value)
            for key, value in source.env.items()
        }
        cwd = source.cwd or str(self.settings.project_root)
        if source.command:
            command = source.command
            args = list(source.args)
        elif source.server_module:
            command = sys.executable
            args = ["-m", source.server_module, *source.args]
        else:
            raise ValueError(f"discovery source={source.source_id} 缺少 command 或 server_module")
        return StdioServerParameters(command=command, args=args, env=env, cwd=cwd)

    def _resolve_env_value(self, raw: str) -> str:
        if raw.startswith("os.environ/"):
            env_name = raw.split("/", 1)[1]
            if env_name in os.environ:
                return os.environ[env_name]
            attr_name = env_name.lower()
            if hasattr(self.settings, attr_name):
                value = getattr(self.settings, attr_name)
                if value is None:
                    return ""
                if isinstance(value, Path):
                    return str(value)
                return str(value)
            return ""
        return raw

    def _extract_payload(self, result: Any) -> dict[str, Any]:
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        content = getattr(result, "content", None) or []
        for item in content:
            if hasattr(item, "text"):
                try:
                    parsed = json.loads(item.text)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise ValueError("discovery MCP 未返回可解析的 structuredContent")

    def _dedupe(self, agents: list[ExternalAgentCard]) -> list[ExternalAgentCard]:
        deduped: dict[str, ExternalAgentCard] = {}
        for agent in agents:
            deduped.setdefault(agent.agent_id, agent)
        return list(deduped.values())

    def _apply_filters(self, agents: list[ExternalAgentCard]) -> list[ExternalAgentCard]:
        filters = self.config.filters
        filtered: list[ExternalAgentCard] = []
        for agent in agents:
            if filters.include_categories and agent.category not in filters.include_categories:
                continue
            if filters.exclude_categories and agent.category in filters.exclude_categories:
                continue
            if filters.include_tags and not (set(agent.tags) & set(filters.include_tags)):
                continue
            if filters.exclude_tags and (set(agent.tags) & set(filters.exclude_tags)):
                continue
            filtered.append(agent)
        filtered.sort(key=lambda item: (item.category, item.name.lower(), item.agent_id))
        return filtered

