from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request

from app.adapters.codex_app_server_client import CodexAppServerClient
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
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return ""


def _build_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> str:
    lines: list[str] = []
    if tools:
        tool_names = [
            str((tool.get("function") or {}).get("name") or tool.get("name") or "tool")
            for tool in tools
        ]
        lines.append(
            "你当前经由一个只返回纯文本的兼容适配层运行。可用工具名称仅供参考，请直接根据已有上下文作答："
            + ", ".join(tool_names)
        )
        lines.append("")
    for message in messages:
        role = str(message.get("role") or "user").upper()
        text = _extract_text(message.get("content"))
        if not text and role != "ASSISTANT":
            continue
        lines.append(f"{role}: {text}".rstrip())
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

    prompt = _build_prompt(messages, payload.get("tools"))
    client = CodexAppServerClient(settings)
    result = client.complete(prompt, model=settings.coder_premium_model)
    now = int(time.time())
    response_id = f"chatcmpl_{uuid4().hex}"
    requested_model = str(payload.get("model") or "coder-premium")
    assistant_text = result.text
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": now,
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": assistant_text,
                },
                "finish_reason": "stop",
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
        },
    }
