from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ExecutionInputFile(BaseModel):
    path: str
    content: str


class ExecutionRequest(BaseModel):
    project_id: str | None = None
    session_id: str | None = None
    language: str = "python"
    command: str | None = None
    entrypoint: str | None = None
    files: list[ExecutionInputFile] = Field(default_factory=list)
    workspace_paths: list[str] = Field(default_factory=list)
    timeout_seconds: int | None = None
    writeback: bool = False
    allow_network: bool | None = None


class ExecutionPolicy(BaseModel):
    sandbox_mode: str
    requested_sandbox_mode: str
    timeout_seconds: int
    memory_mb: int
    cpu_limit: float
    network_enabled: bool
    writeback_enabled: bool
    max_stdout_chars: int
    max_stderr_chars: int
    container_image: str
    allow_dependency_install: bool = False
    command_allowlist: list[str] = Field(default_factory=list)
    command_denylist: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ExecutionArtifact(BaseModel):
    artifact_id: str | None = None
    job_id: str | None = None
    relative_path: str
    size_bytes: int
    mime_type: str
    created_at: datetime | None = None


class ExecutionJob(BaseModel):
    job_id: str
    trace_id: str
    request_id: str
    session_id: str | None = None
    tenant_id: str
    user_id: str
    project_id: str
    status: str
    language: str
    command: str
    entrypoint: str | None = None
    sandbox_mode: str
    sandbox_id: str | None = None
    workspace_root: str
    job_root: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    artifact_count: int = 0
    network_enabled: bool = False
    writeback_enabled: bool = False
    resource_json: dict = Field(default_factory=dict)


class SandboxRunSummary(BaseModel):
    status: str
    sandbox_mode: str
    requested_sandbox_mode: str
    sandbox_id: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    stdout_chars: int = 0
    stderr_chars: int = 0
    timed_out: bool = False
    notes: list[str] = Field(default_factory=list)
    error: str | None = None


class ExecutionResult(BaseModel):
    trace_id: str
    request_id: str
    session_id: str | None
    tenant_id: str
    user_id: str
    project_id: str
    job: ExecutionJob
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    artifacts: list[ExecutionArtifact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    sandbox_summary: SandboxRunSummary
