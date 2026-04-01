from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


APP_NAME = "external-agent-catalog-mcp"
CATALOG_FILE = Path(
    os.environ.get("EXTERNAL_AGENT_CATALOG_FILE", "data/external_agents/catalog.json")
).expanduser()
BASE_URL = os.environ.get("EXTERNAL_AGENT_BASE_URL", "http://127.0.0.1:7777").rstrip("/")

mcp = FastMCP(APP_NAME)


def _load_agents() -> list[dict[str, Any]]:
    payload = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    items = payload.get("agents") or []
    agents: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        agent = dict(item)
        agent["source_id"] = str(agent.get("source_id") or "local-demo-catalog")
        agent["card_url"] = _to_url(str(agent.get("card_path") or agent.get("card_url") or ""))
        agent["message_url"] = _to_url(str(agent.get("message_path") or agent.get("message_url") or ""))
        agent.setdefault("metadata", {})
        agents.append(agent)
    return agents


def _to_url(value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/"):
        return f"{BASE_URL}{value}"
    return f"{BASE_URL}/{value.lstrip('/')}"


@mcp.tool()
def external_agent_catalog_list(
    category: str = "",
    capability: str = "",
    name_query: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    category = category.strip().lower()
    capability = capability.strip().lower()
    name_query = name_query.strip().lower()
    agents = _load_agents()
    filtered: list[dict[str, Any]] = []
    for agent in agents:
        if category and str(agent.get("category") or "").lower() != category:
            continue
        if capability:
            capabilities = {str(item).lower() for item in agent.get("capabilities") or []}
            if capability not in capabilities:
                continue
        if name_query:
            haystack = " ".join(
                [
                    str(agent.get("agent_id") or ""),
                    str(agent.get("name") or ""),
                    str(agent.get("description") or ""),
                    " ".join(str(item) for item in agent.get("tags") or []),
                    " ".join(str(item) for item in agent.get("capabilities") or []),
                ]
            ).lower()
            if name_query not in haystack:
                continue
        filtered.append(agent)
    return {
        "ok": True,
        "agent_count": len(filtered[:limit]),
        "agents": filtered[:limit],
    }


@mcp.tool()
def external_agent_catalog_get(agent_id: str) -> dict[str, Any]:
    for agent in _load_agents():
        if str(agent.get("agent_id")) == agent_id:
            return {"ok": True, "agent": agent}
    raise ValueError(f"未找到 external agent: {agent_id}")


if __name__ == "__main__":
    mcp.run()
