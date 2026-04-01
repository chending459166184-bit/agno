from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.db import write_mcp_audit_log
from app.workspace import ensure_workspace, list_files, read_text_file, save_text_file


APP_NAME = "workspace-mcp"
WORKSPACE_ROOT = ensure_workspace(Path(os.environ["USER_WORKSPACE_ROOT"]).expanduser().resolve())
MCP_ALLOW_WRITE = os.environ.get("MCP_ALLOW_WRITE", "false").lower() == "true"
AUDIT_DB = os.environ.get("MCP_AUDIT_DB")
AUDIT_TRACE_ID = os.environ.get("MCP_AUDIT_TRACE_ID", "")
AUDIT_REQUEST_ID = os.environ.get("MCP_AUDIT_REQUEST_ID", "")
AUDIT_SESSION_ID = os.environ.get("MCP_AUDIT_SESSION_ID", "")
AUDIT_TENANT_ID = os.environ.get("MCP_AUDIT_TENANT_ID", "")
AUDIT_USER_ID = os.environ.get("MCP_AUDIT_USER_ID", "")

mcp = FastMCP(APP_NAME)


def audit(tool_name: str, payload: dict[str, Any]) -> None:
    if not AUDIT_DB:
        return
    write_mcp_audit_log(
        db_file=Path(AUDIT_DB),
        trace_id=AUDIT_TRACE_ID,
        request_id=AUDIT_REQUEST_ID,
        session_id=AUDIT_SESSION_ID,
        tenant_id=AUDIT_TENANT_ID,
        user_id=AUDIT_USER_ID,
        event_type="mcp_tool_call",
        payload={"tool_name": tool_name, **payload},
    )


@mcp.tool()
def workspace_list_files(prefix: str = "", limit: int = 50) -> dict[str, Any]:
    files = list_files(WORKSPACE_ROOT, prefix=prefix, limit=limit)
    audit("workspace_list_files", {"prefix": prefix, "count": len(files)})
    return {"ok": True, "root": str(WORKSPACE_ROOT), "files": files, "allow_write": MCP_ALLOW_WRITE}


@mcp.tool()
def workspace_read_text_file(path: str, max_chars: int = 6000) -> dict[str, Any]:
    data = read_text_file(WORKSPACE_ROOT, path, max_chars=max_chars)
    audit("workspace_read_text_file", {"path": path, "truncated": data["truncated"]})
    return {"ok": True, "allow_write": MCP_ALLOW_WRITE, **data}


@mcp.tool()
def workspace_save_text_file(path: str, content: str, overwrite: bool = True) -> dict[str, Any]:
    if not MCP_ALLOW_WRITE:
        audit("workspace_save_text_file", {"path": path, "blocked": True})
        raise ValueError("当前 PoC 默认关闭写入，如需启用请设置 MCP_ALLOW_WRITE=true")
    data = save_text_file(WORKSPACE_ROOT, path, content, overwrite=overwrite)
    audit("workspace_save_text_file", {"path": path, "size": data["size"]})
    return {"ok": True, "allow_write": MCP_ALLOW_WRITE, **data}


if __name__ == "__main__":
    mcp.run()
