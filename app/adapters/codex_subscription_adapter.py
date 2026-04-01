from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request

from app.adapters.codex_app_server_client import CodexAppServerClient, CodexDynamicTool
from app.auth import read_codex_bridge_user
from app.config import get_settings

settings = get_settings()
app = FastAPI(
    title="Codex Subscription Adapter",
    version="0.1.0",
    description="把本地 Codex app-server 适配成 OpenAI-compatible /v1/chat/completions",
)


def _check_auth(authorization: str | None) -> None:
    expected = settings.coder_premium_adapter_key
    if not expected:
        return
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="无效的 coder-premium adapter key")


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"text", "input_text", "output_text", "inputText"}:
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return ""


def _normalize_dynamic_tools(
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
) -> tuple[list[CodexDynamicTool], str | None]:
    if not tools or tool_choice == "none":
        return [], None

    selected_name: str | None = None
    if isinstance(tool_choice, dict):
        if str(tool_choice.get("type") or "") == "function":
            selected_name = str(((tool_choice.get("function") or {}).get("name")) or "").strip() or None

    normalized: list[CodexDynamicTool] = []
    for tool in tools:
        function = tool.get("function") or {}
        name = str(function.get("name") or tool.get("name") or "").strip()
        if not name:
            continue
        if selected_name and name != selected_name:
            continue
        normalized.append(
            CodexDynamicTool(
                name=name,
                description=str(function.get("description") or tool.get("description") or ""),
                input_schema=(function.get("parameters") or tool.get("inputSchema") or {"type": "object"}),
            )
        )

    instruction: str | None = None
    if tool_choice == "required":
        instruction = "本轮必须先调用一个可用 dynamic tool，再继续回答。"
    elif selected_name:
        instruction = f"如需调用工具，本轮只能调用 dynamic tool `{selected_name}`。"
    return normalized, instruction


def _build_prompt(
    messages: list[dict[str, Any]],
    dynamic_tools: list[CodexDynamicTool] | None = None,
    tool_instruction: str | None = None,
) -> str:
    lines: list[str] = []
    tool_call_names: dict[str, str] = {}
    has_tool_results = any(str(message.get("role") or "").lower() == "tool" for message in messages)
    last_role = next(
        (str(message.get("role") or "").lower() for message in reversed(messages) if message.get("role")),
        "",
    )
    if dynamic_tools:
        tool_names = [tool.name for tool in dynamic_tools]
        lines.append(
            "你当前经由一个支持 dynamic tools 的协议桥运行。若需要外部能力，请直接发起 dynamic tool call，不要只在文字里描述计划："
            + ", ".join(tool_names)
        )
        if has_tool_results:
            lines.append(
                "你会在下面看到已经完成的 ASSISTANT_TOOL_CALL 和对应 TOOL_RESULT。请把这些 TOOL_RESULT 视为已执行成功的真实结果，优先直接基于它们续答。"
            )
            if last_role == "tool":
                lines.append(
                    "当前正处于工具结果续答阶段。除非必须获取新的外部信息，否则不要再次调用 dynamic tool，更不要重复已经有结果的调用。"
                )
        if tool_instruction:
            lines.append(tool_instruction)
        lines.append("")
    for message in messages:
        role = str(message.get("role") or "user").lower()
        text = _extract_text(message.get("content"))
        if role == "assistant":
            if text:
                lines.append(f"ASSISTANT: {text}".rstrip())
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                call_id = str(tool_call.get("id") or "")
                name = str(function.get("name") or "")
                arguments = str(function.get("arguments") or "{}")
                if call_id and name:
                    tool_call_names[call_id] = name
                lines.append(f"ASSISTANT_TOOL_CALL[{call_id or 'pending'}]: {name} {arguments}".rstrip())
            continue
        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "")
            tool_name = str(message.get("name") or tool_call_names.get(tool_call_id) or "tool")
            lines.append(f"TOOL_RESULT[{tool_call_id or 'unknown'}] {tool_name}: {text}".rstrip())
            continue
        role_label = role.upper()
        if not text:
            continue
        lines.append(f"{role_label}: {text}".rstrip())
    lines.append("ASSISTANT:")
    return "\n".join(lines).strip()


@app.get("/health")
def health() -> dict:
    try:
        user, identity = read_codex_bridge_user(settings)
        return {
            "ok": True,
            "logged_in": True,
            "auth_file": str(settings.resolved_codex_auth_file),
            "user_id": user.user_id,
            "email": identity.get("email"),
            "token_freshness": identity.get("token_freshness"),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/v1/models")
def list_models(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return {
        "object": "list",
        "data": [
            {
                "id": "coder-premium",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "local-codex-subscription-adapter",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    _check_auth(authorization)
    payload = await request.json()
    if payload.get("stream"):
        raise HTTPException(status_code=400, detail="当前 adapter 暂不支持 stream=true")

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail="缺少 messages")

    dynamic_tools, tool_instruction = _normalize_dynamic_tools(
        payload.get("tools"),
        payload.get("tool_choice"),
    )
    prompt = _build_prompt(messages, dynamic_tools, tool_instruction)
    client = CodexAppServerClient(settings)
    result = client.complete(
        prompt,
        model=settings.coder_premium_model,
        dynamic_tools=dynamic_tools or None,
        stop_on_tool_call=bool(dynamic_tools),
    )
    now = int(time.time())
    response_id = f"chatcmpl_{uuid4().hex}"
    requested_model = str(payload.get("model") or "coder-premium")
    finish_reason = "stop"
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": result.text or None,
    }
    if result.tool_call is not None:
        finish_reason = "tool_calls"
        assistant_message["tool_calls"] = [
            {
                "id": result.tool_call.call_id,
                "type": "function",
                "function": {
                    "name": result.tool_call.tool,
                    "arguments": json.dumps(result.tool_call.arguments, ensure_ascii=False),
                },
            }
        ]

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": now,
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": assistant_message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "_adapter": {
            "provider_model": result.model,
            "provider": result.provider,
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "result_kind": "tool_call" if result.tool_call is not None else "final",
        },
    }
