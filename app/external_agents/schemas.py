from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryFilterPolicy(BaseModel):
    include_categories: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)


class DiscoverySourceConfig(BaseModel):
    source_id: str
    transport: Literal["stdio"] = "stdio"
    server_module: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    tool_name: str = "external_agent_catalog_list"
    get_tool_name: str = "external_agent_catalog_get"
    timeout_seconds: float = 15.0


class A2ADefaultConfig(BaseModel):
    timeout_seconds: float = 20.0
    auth_strategy: Literal["none", "bearer", "header"] = "none"
    auth_token_env: str | None = None
    auth_header_name: str = "Authorization"
    bearer_prefix: str = "Bearer"


class AgentDiscoveryConfig(BaseModel):
    refresh_ttl_seconds: int = 60
    filters: DiscoveryFilterPolicy = Field(default_factory=DiscoveryFilterPolicy)
    default_a2a: A2ADefaultConfig = Field(default_factory=A2ADefaultConfig)
    sources: list[DiscoverySourceConfig] = Field(default_factory=list)


class ExternalAgentCard(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str
    source_id: str
    name: str
    description: str = ""
    category: str = "general"
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    version: str = "1.0.0"
    provider: str = "external"
    card_url: str
    message_url: str
    auth_strategy: str = "none"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiscoverySourceRunResult(BaseModel):
    source_id: str
    transport: str
    discovered_count: int = 0
    error: str | None = None


class DiscoverySnapshot(BaseModel):
    agents: list[ExternalAgentCard] = Field(default_factory=list)
    sources: list[DiscoverySourceRunResult] = Field(default_factory=list)
    fetched_at: float
    expires_at: float
    from_cache: bool = False


class RemoteAgentCard(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = ""
    description: str = ""
    url: str = ""
    version: str = "1.0.0"
    default_input_modes: list[str] = Field(default_factory=list)
    default_output_modes: list[str] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)


class A2AInvocationResult(BaseModel):
    agent_id: str
    task_id: str | None = None
    context_id: str | None = None
    state: str = "completed"
    text: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    agent_card: RemoteAgentCard | None = None


class BrokerSelection(BaseModel):
    agent_id: str
    score: int
    reason: str
    candidate_agent_ids: list[str] = Field(default_factory=list)
    matched_capabilities: list[str] = Field(default_factory=list)


class BrokerInvocationResult(BaseModel):
    selected_agent: ExternalAgentCard
    selection: BrokerSelection
    response: A2AInvocationResult
    candidates: list[ExternalAgentCard] = Field(default_factory=list)

