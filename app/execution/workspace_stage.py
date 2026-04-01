from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.context import RequestContext
from app.execution.artifacts import FileSnapshot, snapshot_files
from app.execution.schemas import ExecutionRequest
from app.workspace import ensure_workspace, normalize_rel_path, save_text_file


@dataclass(slots=True)
class StagedWorkspace:
    job_id: str
    job_root: Path
    workspace_dir: Path
    logs_dir: Path
    runtime_dir: Path
    initial_snapshot: dict[str, FileSnapshot]


def _copy_workspace(source_root: Path, target_root: Path) -> None:
    ensure_workspace(target_root)
    if not source_root.exists():
        return
    for source in sorted(source_root.rglob("*")):
        rel = source.relative_to(source_root)
        destination = target_root / rel
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def stage_workspace_for_job(
    *,
    ctx: RequestContext,
    request: ExecutionRequest,
    jobs_root: Path,
    job_id: str,
) -> StagedWorkspace:
    job_root = (jobs_root / job_id).resolve()
    workspace_dir = ensure_workspace(job_root / "workspace")
    logs_dir = ensure_workspace(job_root / "logs")
    runtime_dir = ensure_workspace(job_root / "runtime_support")
    _copy_workspace(ctx.workspace_root, workspace_dir)

    for path in request.workspace_paths:
        normalize_rel_path(path)

    for item in request.files:
        save_text_file(workspace_dir, item.path, item.content, overwrite=True)

    initial_snapshot = snapshot_files(workspace_dir)
    return StagedWorkspace(
        job_id=job_id,
        job_root=job_root,
        workspace_dir=workspace_dir,
        logs_dir=logs_dir,
        runtime_dir=runtime_dir,
        initial_snapshot=initial_snapshot,
    )


def apply_workspace_writeback(stage: StagedWorkspace, destination_root: Path, artifacts: list[str]) -> list[str]:
    destination_root = ensure_workspace(destination_root)
    written: list[str] = []
    for rel_path in artifacts:
        source = stage.workspace_dir / rel_path
        if not source.exists() or not source.is_file():
            continue
        destination = destination_root / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        written.append(rel_path)
    return written
