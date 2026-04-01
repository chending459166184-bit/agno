from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

import httpx

from app.config import Settings
from app.model_gateway import ModelRouter
from app.model_gateway.task_types import TASK_WORKSPACE


FORBIDDEN_GUARD_MARKERS = [
    ".git",
    ".env",
    "app/",
    "configs/",
    "tests/",
]


@dataclass(slots=True)
class WorkspaceGuardComposeInput:
    tenant_id: str
    user_id: str
    project_id: str
    action: str
    workspace_root: str
    source: str
    reason: str
    payload: dict


def _extract_completion_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        return "".join(texts)
    return ""


def _validate_workspace_guard_output(compose_input: WorkspaceGuardComposeInput, text: str) -> None:
    lowered = (text or "").lower()
    if not text.strip():
        raise ValueError("workspace guard composer returned empty content")
    if any(marker.lower() in lowered for marker in FORBIDDEN_GUARD_MARKERS):
        raise ValueError("workspace guard composer mentioned forbidden repo markers")
    allowed_paths = set()
    if compose_input.action == "list":
        allowed_paths = {
            str(item.get("path") or "")
            for item in compose_input.payload.get("files") or []
            if item.get("path")
        }
    elif compose_input.action in {"read", "write"}:
        target = str(compose_input.payload.get("path") or "")
        if target:
            allowed_paths.add(target)
    if not allowed_paths:
        return
    path_like_tokens = set(
        re.findall(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+(?:\.[A-Za-z0-9._-]+)?", text)
    )
    for token in path_like_tokens:
        if token in allowed_paths:
            continue
        if token == compose_input.workspace_root or token == compose_input.payload.get("root"):
            continue
        raise ValueError(f"workspace guard composer introduced unexpected path: {token}")


def compose_workspace_guard_answer(
    settings: Settings,
    model_router: ModelRouter,
    compose_input: WorkspaceGuardComposeInput,
    healthy_aliases: set[str] | None = None,
) -> str:
    if not healthy_aliases:
        raise RuntimeError("workspace guard composer requires a live model alias")
    route = model_router.resolve(TASK_WORKSPACE, preferred_aliases=healthy_aliases)
    system_prompt = (
        "你是一个工作区结果整理器。"
        "你的任务是把已经确认安全的 Workspace MCP 结果，整理成自然、简洁、面向最终用户的中文回答。"
        "你不能假设额外文件存在。"
        "你不能新增任何未出现在输入结果里的目录、文件、路径或内容。"
        "你不能提及工程根目录、仓库目录、系统目录。"
        "你不能调用工具，也不能要求额外访问文件系统。"
        "如果结果为空，就如实说明为空。"
        "如果是读取文件，就只总结输入中已有内容。"
        "如果是写入结果，就只确认写入路径和结果。"
        "输出应自然、简洁、可信。"
    )
    with httpx.Client(timeout=min(settings.litellm_request_timeout_seconds, 18.0)) as client:
        response = client.post(
            f"{settings.litellm_proxy_base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            json={
                "model": route.alias,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(asdict(compose_input), ensure_ascii=False, indent=2),
                    },
                ],
                "temperature": 0,
                "max_tokens": 220,
            },
        )
        response.raise_for_status()
    answer = _extract_completion_text(response.json()).strip()
    _validate_workspace_guard_output(compose_input, answer)
    return answer
