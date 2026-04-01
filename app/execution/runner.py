from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.execution.policy import ResolvedCommand
from app.execution.schemas import ExecutionPolicy
from app.execution.workspace_stage import StagedWorkspace


@dataclass(slots=True)
class RunnerOutcome:
    status: str
    sandbox_mode: str
    requested_sandbox_mode: str
    sandbox_id: str | None
    exit_code: int | None
    duration_ms: int
    stdout_path: Path
    stderr_path: Path
    stdout_chars: int
    stderr_chars: int
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def _write_python_sitecustomize(runtime_dir: Path) -> None:
    sitecustomize = runtime_dir / "sitecustomize.py"
    sitecustomize.write_text(
        textwrap.dedent(
            """
            import socket

            _original_connect = socket.socket.connect
            _original_connect_ex = socket.socket.connect_ex

            def _blocked(*args, **kwargs):
                raise OSError("Network access is disabled in this sandbox.")

            socket.create_connection = _blocked
            socket.socket.connect = _blocked
            socket.socket.connect_ex = _blocked
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _build_env(stage: StagedWorkspace, policy: ExecutionPolicy) -> dict[str, str]:
    home_dir = stage.job_root / "home"
    tmp_dir = stage.job_root / "tmp"
    home_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(home_dir),
        "TMPDIR": str(tmp_dir),
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "*",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "PYTHONNOUSERSITE": "1",
    }
    if not policy.network_enabled:
        _write_python_sitecustomize(stage.runtime_dir)
        env["PYTHONPATH"] = str(stage.runtime_dir)
    return env


def _read_log_excerpt(path: Path) -> tuple[str, int]:
    if not path.exists():
        return "", 0
    text = path.read_text(encoding="utf-8", errors="replace")
    return text, len(text)


def _process_preexec(policy: ExecutionPolicy):  # pragma: no cover - exercised on POSIX systems
    def _apply_limits() -> None:
        import resource

        os.setsid()
        try:
            memory_bytes = policy.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except Exception:
            pass
        try:
            cpu_seconds = max(1, math.ceil(policy.timeout_seconds * max(policy.cpu_limit, 0.25)))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        except Exception:
            pass

    return _apply_limits


def _run_process(
    stage: StagedWorkspace,
    policy: ExecutionPolicy,
    command: ResolvedCommand,
) -> RunnerOutcome:
    started_at = time.perf_counter()
    stdout_path = stage.logs_dir / "stdout.txt"
    stderr_path = stage.logs_dir / "stderr.txt"
    env = _build_env(stage, policy)
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            command.argv,
            cwd=str(stage.workspace_dir),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            preexec_fn=_process_preexec(policy) if os.name != "nt" else None,
            text=True,
        )
        sandbox_id = f"proc_{process.pid}"
        try:
            exit_code = process.wait(timeout=policy.timeout_seconds)
            status = "success" if exit_code == 0 else "failed"
            error = None
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover
                process.kill()
            process.wait(timeout=5)
            exit_code = None
            status = "timeout"
            error = "sandbox execution timed out"
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    stdout_text, stdout_chars = _read_log_excerpt(stdout_path)
    stderr_text, stderr_chars = _read_log_excerpt(stderr_path)
    notes: list[str] = []
    if not policy.network_enabled:
        notes.append("process 模式通过 Python sitecustomize 禁用网络，隔离弱于 Docker network none。")
    return RunnerOutcome(
        status=status,
        sandbox_mode="process",
        requested_sandbox_mode=policy.requested_sandbox_mode,
        sandbox_id=sandbox_id,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_chars=stdout_chars,
        stderr_chars=stderr_chars,
        notes=notes,
        error=error,
    )


def _run_docker(
    stage: StagedWorkspace,
    policy: ExecutionPolicy,
    command: ResolvedCommand,
) -> RunnerOutcome:
    started_at = time.perf_counter()
    stdout_path = stage.logs_dir / "stdout.txt"
    stderr_path = stage.logs_dir / "stderr.txt"
    env_args = [
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "PYTHONNOUSERSITE=1",
    ]
    if not policy.network_enabled:
        _write_python_sitecustomize(stage.runtime_dir)
        env_args.extend(["-e", "PYTHONPATH=/sandbox_support"])
    stage.workspace_dir.chmod(0o777)
    stage.logs_dir.chmod(0o777)
    stage.runtime_dir.chmod(0o755)
    sandbox_id = f"sbox_{stage.job_id[:12]}"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        sandbox_id,
        "--workdir",
        "/workspace",
        "--memory",
        f"{policy.memory_mb}m",
        "--cpus",
        str(policy.cpu_limit),
        "--pids-limit",
        "64",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/var/tmp",
        "-v",
        f"{stage.workspace_dir}:/workspace",
        "-v",
        f"{stage.runtime_dir}:/sandbox_support:ro",
        "-u",
        "65532:65532",
        *env_args,
    ]
    if not policy.network_enabled:
        docker_cmd.extend(["--network", "none"])
    docker_cmd.extend([policy.container_image, *command.argv])
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            docker_cmd,
            cwd=str(stage.workspace_dir),
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        try:
            exit_code = process.wait(timeout=policy.timeout_seconds)
            status = "success" if exit_code == 0 else "failed"
            error = None
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            exit_code = None
            status = "timeout"
            error = "sandbox execution timed out"
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    _, stdout_chars = _read_log_excerpt(stdout_path)
    _, stderr_chars = _read_log_excerpt(stderr_path)
    return RunnerOutcome(
        status=status,
        sandbox_mode="docker",
        requested_sandbox_mode=policy.requested_sandbox_mode,
        sandbox_id=sandbox_id,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_chars=stdout_chars,
        stderr_chars=stderr_chars,
        notes=[],
        error=error,
    )


def run_sandbox_command(
    stage: StagedWorkspace,
    policy: ExecutionPolicy,
    command: ResolvedCommand,
) -> RunnerOutcome:
    if policy.sandbox_mode == "process":
        return _run_process(stage, policy, command)
    try:
        return _run_docker(stage, policy, command)
    except FileNotFoundError:
        fallback_policy = policy.model_copy(
            update={
                "sandbox_mode": "process",
                "notes": [
                    *policy.notes,
                    "本地未检测到 Docker，已回退到 process 模式，隔离边界弱于容器模式。",
                ],
            }
        )
        outcome = _run_process(stage, fallback_policy, command)
        outcome.notes = [*fallback_policy.notes, *outcome.notes]
        return outcome
    except Exception as exc:
        if shutil.which("docker") is None:
            fallback_policy = policy.model_copy(
                update={
                    "sandbox_mode": "process",
                    "notes": [
                        *policy.notes,
                        f"Docker 不可用，已回退到 process 模式: {exc}",
                    ],
                }
            )
            outcome = _run_process(stage, fallback_policy, command)
            outcome.notes = [*fallback_policy.notes, *outcome.notes]
            return outcome
        raise
