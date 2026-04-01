from __future__ import annotations

import shlex
import shutil
import sys
from dataclasses import dataclass

from app.config import Settings
from app.execution.schemas import ExecutionPolicy, ExecutionRequest
from app.workspace import normalize_rel_path


SAFE_EXECUTABLES = {"python", "python3", "pytest"}
DENIED_TOKENS = {
    "pip",
    "pip3",
    "poetry",
    "uv",
    "npm",
    "pnpm",
    "yarn",
    "brew",
    "apt",
    "apt-get",
    "curl",
    "wget",
    "ssh",
    "scp",
    "docker",
    "git",
}


@dataclass(slots=True)
class ResolvedCommand:
    argv: list[str]
    display_command: str
    entrypoint: str | None = None


def build_execution_policy(settings: Settings, request: ExecutionRequest) -> ExecutionPolicy:
    timeout_seconds = request.timeout_seconds or settings.exec_default_timeout_seconds
    timeout_seconds = max(1, min(timeout_seconds, settings.exec_max_timeout_seconds))
    requested_mode = (settings.exec_sandbox_mode or "docker").strip().lower()
    requested_mode = requested_mode if requested_mode in {"docker", "process"} else "docker"
    network_enabled = bool(settings.exec_allow_network if request.allow_network is None else request.allow_network)
    writeback_enabled = bool(request.writeback and settings.exec_allow_workspace_writeback)
    notes: list[str] = []
    if request.writeback and not settings.exec_allow_workspace_writeback:
        notes.append("当前环境未开启 workspace writeback，已自动降级为只读执行。")
    return ExecutionPolicy(
        sandbox_mode=requested_mode,
        requested_sandbox_mode=requested_mode,
        timeout_seconds=timeout_seconds,
        memory_mb=min(settings.exec_default_memory_mb, settings.exec_max_memory_mb),
        cpu_limit=settings.exec_default_cpu_limit,
        network_enabled=network_enabled,
        writeback_enabled=writeback_enabled,
        max_stdout_chars=settings.exec_max_stdout_chars,
        max_stderr_chars=settings.exec_max_stderr_chars,
        container_image=settings.exec_container_image,
        allow_dependency_install=False,
        command_allowlist=sorted(SAFE_EXECUTABLES),
        command_denylist=sorted(DENIED_TOKENS),
        notes=notes,
    )


def resolve_execution_command(request: ExecutionRequest) -> ResolvedCommand:
    language = (request.language or "python").strip().lower()
    if language != "python":
        raise ValueError("第一版执行沙箱只支持 Python 任务。")

    entrypoint = request.entrypoint
    if entrypoint:
        entrypoint = normalize_rel_path(entrypoint)

    if request.command:
        argv = shlex.split(request.command)
        if not argv:
            raise ValueError("command 不能为空")
        executable = argv[0]
        if executable not in SAFE_EXECUTABLES:
            raise ValueError(f"当前仅允许受控命令: {', '.join(sorted(SAFE_EXECUTABLES))}")
        lowered = {part.lower() for part in argv}
        denied = sorted(lowered & DENIED_TOKENS)
        if denied:
            raise ValueError(f"当前命令包含被禁止的依赖安装或宿主操作: {', '.join(denied)}")
        if executable in {"python", "python3"}:
            argv = [sys.executable, *argv[1:]]
        elif executable == "pytest":
            pytest_bin = shutil.which("pytest")
            if pytest_bin:
                argv = [pytest_bin, *argv[1:]]
            else:
                argv = [sys.executable, "-m", "pytest", *argv[1:]]
        if executable.startswith("python") and entrypoint:
            return ResolvedCommand(argv=argv, display_command=" ".join(argv), entrypoint=entrypoint)
        return ResolvedCommand(argv=argv, display_command=" ".join(argv), entrypoint=entrypoint)

    if entrypoint:
        return ResolvedCommand(
            argv=[sys.executable, entrypoint],
            display_command=f"python {entrypoint}",
            entrypoint=entrypoint,
        )

    python_files = [normalize_rel_path(item.path) for item in request.files if item.path.endswith(".py")]
    if python_files:
        target = python_files[0]
        return ResolvedCommand(
            argv=[sys.executable, target],
            display_command=f"python {target}",
            entrypoint=target,
        )

    raise ValueError("当前无法推断要执行的 Python 入口，请提供 command、entrypoint 或 .py 文件。")
