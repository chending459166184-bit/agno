from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AuthenticatedUser:
    tenant_id: str
    user_id: str
    display_name: str
    role: str
    project_ids: list[str]
    default_project_id: str


@dataclass(slots=True)
class RequestContext:
    trace_id: str
    request_id: str
    session_id: str
    tenant_id: str
    user_id: str
    display_name: str
    role: str
    project_id: str
    workspace_root: Path
    knowledge_hits: list[dict] = field(default_factory=list)

