from __future__ import annotations

import json
import sys
from datetime import timedelta
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.config import Settings
from app.context import RequestContext


def build_workspace_mcp_env(settings: Settings, ctx: RequestContext) -> dict[str, str]:
    return {
        "USER_WORKSPACE_ROOT": str(ctx.workspace_root),
        "MCP_ALLOW_WRITE": "true" if settings.mcp_allow_write else "false",
        "MCP_AUDIT_DB": str(settings.resolved_db_file),
        "MCP_AUDIT_TRACE_ID": ctx.trace_id,
        "MCP_AUDIT_REQUEST_ID": ctx.request_id,
        "MCP_AUDIT_SESSION_ID": ctx.session_id,
        "MCP_AUDIT_TENANT_ID": ctx.tenant_id,
        "MCP_AUDIT_USER_ID": ctx.user_id,
    }


def build_workspace_mcp_server(settings: Settings, ctx: RequestContext) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp.user_workspace_server"],
        env=build_workspace_mcp_env(settings, ctx),
        cwd=str(settings.project_root),
    )


def extract_mcp_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("MCP 未返回可解析的 structuredContent")


async def _call_tool_async(
    settings: Settings,
    ctx: RequestContext,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    server = build_workspace_mcp_server(settings, ctx)
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool(
                tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            )
    return extract_mcp_payload(result)


def call_workspace_mcp_tool(
    settings: Settings,
    ctx: RequestContext,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    return anyio.run(_call_tool_async, settings, ctx, tool_name, arguments, timeout_seconds)
