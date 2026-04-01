from __future__ import annotations

import json
import select
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings


@dataclass(slots=True)
class CodexDynamicTool:
    name: str
    description: str
    input_schema: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass(slots=True)
class CodexToolCall:
    call_id: str
    tool: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class CodexTurnResult:
    text: str
    thread_id: str
    turn_id: str
    model: str
    provider: str
    tool_call: CodexToolCall | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)


class CodexAppServerClient:
    def __init__(self, settings: Settings, *, cwd: Path | None = None) -> None:
        self.settings = settings
        safe_root = settings.resolved_codex_safe_cwd_root
        safe_root.mkdir(parents=True, exist_ok=True)
        self.cwd = (cwd or safe_root / "general").resolve()
        self.cwd.mkdir(parents=True, exist_ok=True)

    def complete(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        dynamic_tools: list[CodexDynamicTool] | None = None,
        stop_on_tool_call: bool = False,
    ) -> CodexTurnResult:
        active_cwd = str((cwd or self.cwd).resolve())
        provider_model = model or self.settings.coder_premium_model
        timeout = timeout_seconds or self.settings.litellm_request_timeout_seconds
        process = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if not process.stdin or not process.stdout or not process.stderr:
            raise RuntimeError("无法启动 codex app-server")

        next_id = 1
        stderr_lines: list[str] = []
        events: list[dict[str, Any]] = []
        deltas: list[str] = []
        final_text: str | None = None
        thread_id: str | None = None
        turn_id: str | None = None
        provider = "openai"
        tool_call: CodexToolCall | None = None
        try:
            self._send(
                process,
                {
                    "id": next_id,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "agno-coder-premium-adapter",
                            "title": "Agno Coder Premium Adapter",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                },
            )
            self._expect_response(process, next_id, timeout, stderr_lines)
            next_id += 1

            self._send(process, {"method": "initialized", "params": {}})
            thread_start_params: dict[str, Any] = {}
            if dynamic_tools:
                thread_start_params["dynamicTools"] = [
                    tool.as_payload() for tool in dynamic_tools
                ]
            self._send(process, {"id": next_id, "method": "thread/start", "params": thread_start_params})
            thread_response = self._expect_response(process, next_id, timeout, stderr_lines)
            next_id += 1

            thread_payload = thread_response.get("result", {})
            thread = thread_payload.get("thread") or thread_payload
            thread_id = thread.get("id")
            provider = str(thread_payload.get("modelProvider") or thread.get("modelProvider") or provider)
            if not thread_id:
                raise RuntimeError("Codex app-server 未返回 thread_id")

            self._send(
                process,
                {
                    "id": next_id,
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                        "cwd": active_cwd,
                        "approvalPolicy": "never",
                        "sandboxPolicy": {
                            "type": "workspaceWrite",
                            "writableRoots": [active_cwd],
                            "networkAccess": True,
                        },
                        "model": provider_model,
                        "effort": self.settings.coder_premium_reasoning_effort,
                        "summary": self.settings.coder_premium_summary_mode,
                        "personality": self.settings.coder_premium_personality,
                    },
                },
            )
            turn_response = self._expect_response(process, next_id, timeout, stderr_lines)
            turn_id = (
                turn_response.get("result", {})
                .get("turn", {})
                .get("id")
            )
            if not turn_id:
                raise RuntimeError("Codex app-server 未返回 turn_id")

            deadline = time.time() + timeout
            saw_idle = False
            while time.time() < deadline:
                message = self._read_message(process, timeout=0.5)
                if not message:
                    if saw_idle and final_text:
                        break
                    continue

                channel, payload = message
                if channel == "stderr":
                    stderr_lines.append(payload)
                    continue

                events.append(payload)
                if payload.get("error"):
                    raise RuntimeError(payload["error"].get("message") or "Codex turn failed")

                method = payload.get("method")
                params = payload.get("params") or {}
                if method == "item/agentMessage/delta":
                    delta = str(params.get("delta") or "")
                    if delta:
                        deltas.append(delta)
                elif method == "item/tool/call":
                    tool_call = CodexToolCall(
                        call_id=str(params.get("callId") or f"call_{int(time.time() * 1000)}"),
                        tool=str(params.get("tool") or ""),
                        arguments=params.get("arguments") or {},
                    )
                    if stop_on_tool_call:
                        break
                elif method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        final_text = str(item.get("text") or final_text or "")
                elif method == "turn/completed":
                    turn_data = params.get("turn") or {}
                    error = turn_data.get("error")
                    if error:
                        raise RuntimeError(error.get("message") or "Codex turn completed with error")
                    break
                elif method == "thread/status/changed":
                    status_type = (params.get("status") or {}).get("type")
                    if status_type == "idle":
                        saw_idle = True
                        if final_text:
                            break

            text = (final_text or "".join(deltas)).strip()
            if not text and tool_call is None:
                diagnostic = stderr_lines[-1] if stderr_lines else "未收到模型文本输出"
                raise RuntimeError(f"Codex app-server 没有返回可用文本: {diagnostic}")
            return CodexTurnResult(
                text=text,
                thread_id=thread_id,
                turn_id=turn_id,
                model=provider_model,
                provider=provider,
                tool_call=tool_call,
                events=events,
                stderr_lines=stderr_lines,
            )
        finally:
            self._terminate(process)

    def _send(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def _expect_response(
        self,
        process: subprocess.Popen[str],
        request_id: int,
        timeout: float,
        stderr_lines: list[str],
    ) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self._read_message(process, timeout=0.5)
            if not message:
                continue
            channel, payload = message
            if channel == "stderr":
                stderr_lines.append(payload)
                continue
            if payload.get("id") != request_id:
                continue
            if payload.get("error"):
                raise RuntimeError(payload["error"].get("message") or "Codex request failed")
            return payload
        raise TimeoutError(f"等待 Codex app-server 响应超时，请求 id={request_id}")

    def _read_message(
        self,
        process: subprocess.Popen[str],
        *,
        timeout: float,
    ) -> tuple[str, dict[str, Any] | str] | None:
        readable, _, _ = select.select([process.stdout, process.stderr], [], [], timeout)
        if not readable:
            return None

        for pipe in readable:
            line = pipe.readline()
            if not line:
                continue
            if pipe is process.stderr:
                return ("stderr", line.rstrip())
            try:
                return ("stdout", json.loads(line))
            except json.JSONDecodeError:
                return ("stderr", f"非 JSON 输出: {line.rstrip()}")
        return None

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
