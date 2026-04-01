from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.context import RequestContext
from app.db import Database
from app.execution.artifacts import detect_artifacts, read_artifact_payload
from app.execution.policy import build_execution_policy, resolve_execution_command
from app.execution.runner import run_sandbox_command
from app.execution.schemas import ExecutionArtifact, ExecutionJob, ExecutionRequest, ExecutionResult, SandboxRunSummary
from app.execution.workspace_stage import apply_workspace_writeback, stage_workspace_for_job


class ExecutionManager:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.jobs_root = settings.resolved_exec_jobs_root
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def run(self, ctx: RequestContext, request: ExecutionRequest) -> ExecutionResult:
        if not self.settings.exec_sandbox_enabled:
            raise RuntimeError("当前环境未启用执行沙箱。")
        policy = build_execution_policy(self.settings, request)
        command = resolve_execution_command(request)
        job_id = self.database.create_execution_job(
            ctx=ctx,
            language=request.language,
            command=command.display_command,
            entrypoint=command.entrypoint,
            sandbox_mode=policy.sandbox_mode,
            workspace_root=str(ctx.workspace_root),
            job_root=str((self.jobs_root / "pending").resolve()),
            network_enabled=policy.network_enabled,
            writeback_enabled=policy.writeback_enabled,
            resource_json={
                "timeout_seconds": policy.timeout_seconds,
                "memory_mb": policy.memory_mb,
                "cpu_limit": policy.cpu_limit,
                "requested_sandbox_mode": policy.requested_sandbox_mode,
                "notes": policy.notes,
            },
        )
        self.database.record_sandbox_job_created(
            ctx,
            job_id=job_id,
            payload={
                "language": request.language,
                "command": command.display_command,
                "entrypoint": command.entrypoint,
                "sandbox_mode": policy.sandbox_mode,
                "writeback_enabled": policy.writeback_enabled,
                "network_enabled": policy.network_enabled,
            },
        )
        stage = stage_workspace_for_job(
            ctx=ctx,
            request=request,
            jobs_root=self.jobs_root,
            job_id=job_id,
        )
        self.database.update_execution_job_paths(
            job_id,
            job_root=str(stage.job_root),
        )
        self.database.record_sandbox_stage_prepared(
            ctx,
            job_id=job_id,
            payload={
                "job_root": str(stage.job_root),
                "workspace_dir": str(stage.workspace_dir),
                "seed_file_count": len(stage.initial_snapshot),
            },
        )
        self.database.mark_execution_job_running(job_id)
        self.database.record_sandbox_started(
            ctx,
            job_id=job_id,
            payload={"sandbox_mode": policy.sandbox_mode, "command": command.display_command},
        )
        try:
            outcome = run_sandbox_command(stage, policy, command)
        except Exception as exc:
            self.database.complete_execution_job(
                job_id=job_id,
                status="failed",
                sandbox_mode=policy.sandbox_mode,
                sandbox_id=None,
                duration_ms=None,
                exit_code=None,
                stdout_path=None,
                stderr_path=None,
                artifact_count=0,
            resource_json={
                "timeout_seconds": policy.timeout_seconds,
                "memory_mb": policy.memory_mb,
                "cpu_limit": policy.cpu_limit,
                "requested_sandbox_mode": policy.requested_sandbox_mode,
                "notes": policy.notes,
            },
        )
            self.database.record_sandbox_failed(
                ctx,
                job_id=job_id,
                payload={"sandbox_mode": policy.sandbox_mode, "error": str(exc)},
            )
            raise
        artifacts = detect_artifacts(stage.initial_snapshot, stage.workspace_dir)
        persisted_artifacts: list[ExecutionArtifact] = []
        for artifact in artifacts:
            persisted = self.database.add_execution_artifact(
                job_id=job_id,
                relative_path=artifact.relative_path,
                size_bytes=artifact.size_bytes,
                mime_type=artifact.mime_type,
            )
            persisted_artifacts.append(ExecutionArtifact(**persisted))
            self.database.record_sandbox_artifact_recorded(
                ctx,
                job_id=job_id,
                payload={
                    "relative_path": artifact.relative_path,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.mime_type,
                },
            )

        writeback_paths: list[str] = []
        if policy.writeback_enabled and persisted_artifacts and outcome.status == "success":
            writeback_paths = apply_workspace_writeback(
                stage,
                ctx.workspace_root,
                [artifact.relative_path for artifact in persisted_artifacts],
            )
            self.database.record_sandbox_writeback_applied(
                ctx,
                job_id=job_id,
                payload={"written_paths": writeback_paths, "count": len(writeback_paths)},
            )
        else:
            self.database.record_sandbox_writeback_skipped(
                ctx,
                job_id=job_id,
                payload={
                    "requested": request.writeback,
                    "enabled": policy.writeback_enabled,
                    "status": outcome.status,
                },
            )

        self.database.complete_execution_job(
            job_id=job_id,
            status=outcome.status,
            sandbox_mode=outcome.sandbox_mode,
            sandbox_id=outcome.sandbox_id,
            duration_ms=outcome.duration_ms,
            exit_code=outcome.exit_code,
            stdout_path=str(outcome.stdout_path),
            stderr_path=str(outcome.stderr_path),
            artifact_count=len(persisted_artifacts),
            resource_json={
                "timeout_seconds": policy.timeout_seconds,
                "memory_mb": policy.memory_mb,
                "cpu_limit": policy.cpu_limit,
                "requested_sandbox_mode": policy.requested_sandbox_mode,
                "actual_sandbox_mode": outcome.sandbox_mode,
                "notes": [*policy.notes, *outcome.notes],
            },
        )

        event_payload = {
            "sandbox_mode": outcome.sandbox_mode,
            "sandbox_id": outcome.sandbox_id,
            "duration_ms": outcome.duration_ms,
            "exit_code": outcome.exit_code,
            "artifact_count": len(persisted_artifacts),
        }
        if outcome.status == "success":
            self.database.record_sandbox_completed(ctx, job_id=job_id, payload=event_payload)
        elif outcome.status == "timeout":
            self.database.record_sandbox_timeout(ctx, job_id=job_id, payload=event_payload)
        else:
            self.database.record_sandbox_failed(
                ctx,
                job_id=job_id,
                payload={**event_payload, "error": outcome.error},
            )
        return self.get_result_for_job(job_id)

    def _truncate_log(self, text: str, max_chars: int) -> tuple[str, bool]:
        if len(text) <= max_chars:
            return text, False
        return text[:max_chars], True

    def get_job(self, job_id: str) -> ExecutionJob:
        row = self.database.get_execution_job(job_id)
        if row is None:
            raise FileNotFoundError(job_id)
        return ExecutionJob(**row)

    def get_logs(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        stdout_text = Path(job.stdout_path).read_text(encoding="utf-8", errors="replace") if job.stdout_path else ""
        stderr_text = Path(job.stderr_path).read_text(encoding="utf-8", errors="replace") if job.stderr_path else ""
        stdout_excerpt, stdout_truncated = self._truncate_log(stdout_text, self.settings.exec_max_stdout_chars)
        stderr_excerpt, stderr_truncated = self._truncate_log(stderr_text, self.settings.exec_max_stderr_chars)
        return {
            "job_id": job_id,
            "trace_id": job.trace_id,
            "status": job.status,
            "stdout": stdout_excerpt,
            "stderr": stderr_excerpt,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }

    def list_artifacts(self, job_id: str) -> list[ExecutionArtifact]:
        return [ExecutionArtifact(**row) for row in self.database.list_execution_artifacts(job_id)]

    def read_artifact(self, job_id: str, rel_path: str) -> dict:
        job = self.get_job(job_id)
        return {
            "job_id": job_id,
            "trace_id": job.trace_id,
            **read_artifact_payload(Path(job.job_root) / "workspace", rel_path),
        }

    def get_result_for_job(self, job_id: str) -> ExecutionResult:
        job = self.get_job(job_id)
        logs = self.get_logs(job_id)
        artifacts = self.list_artifacts(job_id)
        sandbox_summary = SandboxRunSummary(
            status=job.status,
            sandbox_mode=job.sandbox_mode,
            requested_sandbox_mode=str(job.resource_json.get("requested_sandbox_mode", job.sandbox_mode)),
            sandbox_id=job.sandbox_id,
            exit_code=job.exit_code,
            duration_ms=job.duration_ms,
            stdout_path=job.stdout_path,
            stderr_path=job.stderr_path,
            stdout_chars=len(logs["stdout"]),
            stderr_chars=len(logs["stderr"]),
            timed_out=job.status == "timeout",
            notes=list(job.resource_json.get("notes", [])),
        )
        return ExecutionResult(
            trace_id=job.trace_id,
            request_id=job.request_id,
            session_id=job.session_id,
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            project_id=job.project_id,
            job=job,
            stdout=logs["stdout"],
            stderr=logs["stderr"],
            stdout_truncated=logs["stdout_truncated"],
            stderr_truncated=logs["stderr_truncated"],
            artifacts=artifacts,
            notes=list(job.resource_json.get("notes", [])),
            sandbox_summary=sandbox_summary,
        )
