from __future__ import annotations

import io
from importlib import reload
from pathlib import Path
import sys

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.codex_app_server_client import (
    CodexAppServerClient,
    CodexDynamicTool,
    CodexToolCall,
    CodexTurnResult,
)
from app.config import get_settings


def load_adapter_module(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CODER_PREMIUM_ADAPTER_KEY", "test-adapter-key")
    monkeypatch.setenv("CODEX_AUTH_FILE", str(tmp_path / "codex-auth.json"))

    import app.config
    import app.adapters.codex_subscription_adapter as adapter_module

    app.config.get_settings.cache_clear()
    return reload(adapter_module)


def test_adapter_returns_plain_text_completion(monkeypatch, tmp_path: Path) -> None:
    adapter_module = load_adapter_module(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, settings) -> None:
            captured["settings"] = settings

        def complete(self, prompt: str, **kwargs) -> CodexTurnResult:
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return CodexTurnResult(
                text="plain answer",
                thread_id="thread_plain",
                turn_id="turn_plain",
                model="gpt-5.4",
                provider="openai",
            )

    monkeypatch.setattr(adapter_module, "CodexAppServerClient", FakeClient)
    client = TestClient(adapter_module.app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-adapter-key"},
        json={
            "model": "coder-premium",
            "messages": [{"role": "user", "content": "你好"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "plain answer"
    assert body["_adapter"]["result_kind"] == "final"
    assert captured["kwargs"] == {
        "model": adapter_module.settings.coder_premium_model,
        "dynamic_tools": None,
        "stop_on_tool_call": False,
    }
    assert "USER: 你好" in str(captured["prompt"])


def test_adapter_returns_openai_tool_calls(monkeypatch, tmp_path: Path) -> None:
    adapter_module = load_adapter_module(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, settings) -> None:
            captured["settings"] = settings

        def complete(self, prompt: str, **kwargs) -> CodexTurnResult:
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return CodexTurnResult(
                text="准备调用工具",
                thread_id="thread_tool",
                turn_id="turn_tool",
                model="gpt-5.4",
                provider="openai",
                tool_call=CodexToolCall(
                    call_id="call_123",
                    tool="workspace_list_files",
                    arguments={"path": "notes"},
                ),
            )

    monkeypatch.setattr(adapter_module, "CodexAppServerClient", FakeClient)
    client = TestClient(adapter_module.app)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "workspace_list_files",
                "description": "列出工作区文件",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-adapter-key"},
        json={
            "model": "coder-premium",
            "messages": [{"role": "user", "content": "帮我看文件"}],
            "tools": tools,
        },
    )

    assert response.status_code == 200
    body = response.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["id"] == "call_123"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "workspace_list_files"
    assert choice["message"]["tool_calls"][0]["function"]["arguments"] == '{"path": "notes"}'
    assert body["_adapter"]["result_kind"] == "tool_call"
    dynamic_tools = captured["kwargs"]["dynamic_tools"]
    assert isinstance(dynamic_tools, list)
    assert dynamic_tools[0].name == "workspace_list_files"
    assert captured["kwargs"]["stop_on_tool_call"] is True
    assert "dynamic tools" in str(captured["prompt"])
    assert "只返回纯文本" not in str(captured["prompt"])


def test_adapter_serializes_tool_results_into_followup_prompt(monkeypatch, tmp_path: Path) -> None:
    adapter_module = load_adapter_module(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, settings) -> None:
            captured["settings"] = settings

        def complete(self, prompt: str, **kwargs) -> CodexTurnResult:
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return CodexTurnResult(
                text="工作区里有 1 个文件。",
                thread_id="thread_followup",
                turn_id="turn_followup",
                model="gpt-5.4",
                provider="openai",
            )

    monkeypatch.setattr(adapter_module, "CodexAppServerClient", FakeClient)
    client = TestClient(adapter_module.app)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-adapter-key"},
        json={
            "model": "coder-premium",
            "messages": [
                {"role": "system", "content": "你是一个企业智能体。"},
                {"role": "user", "content": "我空间里有什么文件？"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "workspace_list_files",
                                "arguments": '{"path": "notes"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_abc",
                    "content": '["notes/alpha-analysis.md"]',
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "workspace_list_files",
                        "description": "列出工作区文件",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    prompt = str(captured["prompt"])
    assert "ASSISTANT_TOOL_CALL[call_abc]: workspace_list_files {\"path\": \"notes\"}" in prompt
    assert "TOOL_RESULT[call_abc] workspace_list_files: [\"notes/alpha-analysis.md\"]" in prompt
    assert "SYSTEM: 你是一个企业智能体。" in prompt


def test_codex_client_can_capture_dynamic_tool_call(monkeypatch, tmp_path: Path) -> None:
    class FakeStdin(io.StringIO):
        def flush(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = object()
            self.stderr = object()
            self._poll = None

        def poll(self):
            return self._poll

        def terminate(self) -> None:
            self._poll = 0

        def wait(self, timeout=None) -> None:
            self._poll = 0

        def kill(self) -> None:
            self._poll = 0

    settings = get_settings()
    fake_process = FakeProcess()
    event_stream = iter(
        [
            ("stdout", {"id": 1, "result": {"ok": True}}),
            ("stdout", {"id": 2, "result": {"thread": {"id": "thread_1", "modelProvider": "openai"}}}),
            ("stdout", {"id": 3, "result": {"turn": {"id": "turn_1"}}}),
            ("stdout", {"method": "item/agentMessage/delta", "params": {"delta": "准备调用工具"}}),
            (
                "stdout",
                {
                    "method": "item/tool/call",
                    "params": {
                        "callId": "call_1",
                        "tool": "workspace_list_files",
                        "arguments": {"path": "notes"},
                    },
                },
            ),
        ]
    )

    def fake_popen(*args, **kwargs):
        return fake_process

    def fake_read_message(self, process, *, timeout):
        return next(event_stream, None)

    monkeypatch.setattr("app.adapters.codex_app_server_client.subprocess.Popen", fake_popen)
    monkeypatch.setattr(CodexAppServerClient, "_read_message", fake_read_message)

    client = CodexAppServerClient(settings)
    result = client.complete(
        "列出文件",
        dynamic_tools=[CodexDynamicTool(name="workspace_list_files", description="", input_schema={"type": "object"})],
        stop_on_tool_call=True,
    )

    assert result.tool_call is not None
    assert result.tool_call.call_id == "call_1"
    assert result.tool_call.tool == "workspace_list_files"
    assert result.tool_call.arguments == {"path": "notes"}
    assert result.text == "准备调用工具"
