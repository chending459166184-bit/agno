from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    app_host: str = "0.0.0.0"
    app_port: int = 7777

    db_file: Path = Field(default=Path("data/app.db"))
    workspace_root: Path = Field(default=Path("data/workspaces"))
    seed_docs_dir: Path = Field(default=Path("docs/seed"))

    jwt_secret: str = "dev-secret-change-me-local-2026-x"
    jwt_issuer: str = "agno-enterprise-local"
    jwt_audience: str = "agno-enterprise-users"

    default_tenant_id: str = "demo"
    default_project_id: str = "alpha"

    litellm_proxy_base_url: str = "http://127.0.0.1:4000"
    litellm_master_key: str = "local-litellm-master-key"
    litellm_proxy_config: Path = Field(default=Path("configs/litellm_proxy.yaml"))
    model_router_config: Path = Field(default=Path("configs/model_router.yaml"))
    agent_discovery_config: Path = Field(default=Path("configs/agent_discovery.yaml"))
    litellm_request_timeout_seconds: float = 45.0
    litellm_health_ttl_seconds: int = 15
    litellm_probe_prompt: str = "Reply with OK."

    openai_api_key: str | None = None
    openai_api_base: str | None = None
    openai_coder_model: str = "openai/gpt-5.3-codex"
    minimax_api_base: str | None = None
    minimax_api_key: str | None = None
    minimax_model_id: str | None = None
    zai_api_base: str | None = None
    zai_api_key: str | None = None
    zai_model_id: str | None = None

    coder_premium_adapter_base_url: str = "http://127.0.0.1:4101/v1"
    coder_premium_adapter_key: str = "local-coder-premium-key"
    coder_premium_model: str = "gpt-5.4"
    coder_premium_reasoning_effort: str = "medium"
    coder_premium_summary_mode: str = "concise"
    coder_premium_personality: str = "friendly"

    external_agent_catalog_file: Path = Field(default=Path("data/external_agents/catalog.json"))
    external_agent_base_url: str = "http://127.0.0.1:7777"
    skills_root: Path = Field(default=Path("skills"))
    codex_safe_cwd_root: Path = Field(default=Path("data/codex_sandbox"))
    exec_sandbox_enabled: bool = True
    exec_sandbox_mode: str = "docker"
    exec_jobs_root: Path = Field(default=Path("data/exec_jobs"))
    exec_default_timeout_seconds: int = 30
    exec_max_timeout_seconds: int = 120
    exec_default_memory_mb: int = 512
    exec_max_memory_mb: int = 1024
    exec_default_cpu_limit: float = 1.0
    exec_allow_network: bool = False
    exec_allow_workspace_writeback: bool = False
    exec_max_stdout_chars: int = 20000
    exec_max_stderr_chars: int = 20000
    exec_container_image: str = "python:3.11-slim"
    external_prefetch_enabled: bool = True
    external_prefetch_mode: str = "prefetch"
    mcp_allow_write: bool = True
    workspace_guard_enabled: bool = False
    execution_guard_enabled: bool = False

    allow_mock_fallback: bool = True
    telemetry_enabled: bool = False
    agno_tracing_enabled: bool = False
    codex_bridge_enabled: bool = True
    codex_auth_file: Path = Field(default=Path("~/.codex/auth.json"))
    codex_bridge_default_role: str = "manager"
    codex_bridge_project_ids: str = "alpha"
    codex_bridge_default_project_id: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def resolved_db_file(self) -> Path:
        return (self.project_root / self.db_file).resolve()

    @property
    def resolved_workspace_root(self) -> Path:
        return (self.project_root / self.workspace_root).resolve()

    @property
    def resolved_seed_docs_dir(self) -> Path:
        return (self.project_root / self.seed_docs_dir).resolve()

    @property
    def resolved_litellm_proxy_config(self) -> Path:
        return (self.project_root / self.litellm_proxy_config).resolve()

    @property
    def resolved_model_router_config(self) -> Path:
        return (self.project_root / self.model_router_config).resolve()

    @property
    def resolved_agent_discovery_config(self) -> Path:
        return (self.project_root / self.agent_discovery_config).resolve()

    @property
    def resolved_codex_auth_file(self) -> Path:
        return self.codex_auth_file.expanduser().resolve()

    @property
    def resolved_external_agent_catalog_file(self) -> Path:
        return (self.project_root / self.external_agent_catalog_file).resolve()

    @property
    def resolved_skills_root(self) -> Path:
        return (self.project_root / self.skills_root).resolve()

    @property
    def resolved_codex_safe_cwd_root(self) -> Path:
        return (self.project_root / self.codex_safe_cwd_root).resolve()

    @property
    def resolved_exec_jobs_root(self) -> Path:
        return (self.project_root / self.exec_jobs_root).resolve()

    @property
    def codex_bridge_project_ids_list(self) -> list[str]:
        parts = [part.strip() for part in self.codex_bridge_project_ids.split(",")]
        return [part for part in parts if part]

    @property
    def effective_codex_default_project_id(self) -> str:
        if self.codex_bridge_default_project_id:
            return self.codex_bridge_default_project_id
        if self.codex_bridge_project_ids_list:
            return self.codex_bridge_project_ids_list[0]
        return self.default_project_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
