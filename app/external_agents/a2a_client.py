from __future__ import annotations

import os
from uuid import uuid4

import httpx

from app.config import Settings
from app.external_agents.schemas import (
    A2AInvocationResult,
    ExternalAgentCard,
    RemoteAgentCard,
)


class A2AClient:
    def __init__(self, settings: Settings, default_auth) -> None:
        self.settings = settings
        self.default_auth = default_auth

    def fetch_agent_card(
        self,
        agent: ExternalAgentCard,
        *,
        timeout_seconds: float | None = None,
    ) -> RemoteAgentCard:
        with httpx.Client(timeout=timeout_seconds or self.default_auth.timeout_seconds) as client:
            response = client.get(agent.card_url, headers=self._build_auth_headers(agent.auth_strategy))
            response.raise_for_status()
        return RemoteAgentCard.model_validate(response.json())

    def send_message(
        self,
        agent: ExternalAgentCard,
        *,
        message: str,
        trace_id: str,
        request_id: str,
        session_id: str,
        user_id: str,
        project_id: str,
        metadata: dict | None = None,
        timeout_seconds: float | None = None,
        agent_card: RemoteAgentCard | None = None,
    ) -> A2AInvocationResult:
        headers = self._build_auth_headers(agent.auth_strategy)
        headers.update(
            {
                "X-Trace-ID": trace_id,
                "X-Request-ID": request_id,
                "X-Session-ID": session_id,
                "X-User-ID": user_id,
                "X-Project-ID": project_id,
            }
        )
        payload = {
            "jsonrpc": "2.0",
            "id": f"a2a_{uuid4().hex[:12]}",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": f"msg_{uuid4().hex[:12]}",
                    "role": "user",
                    "parts": [{"kind": "text", "text": message}],
                    "contextId": session_id,
                    "metadata": {
                        "traceId": trace_id,
                        "requestId": request_id,
                        "userId": user_id,
                        "projectId": project_id,
                        **(metadata or {}),
                    },
                },
                "configuration": {"blocking": True},
            },
        }
        with httpx.Client(timeout=timeout_seconds or self.default_auth.timeout_seconds) as client:
            response = client.post(agent.message_url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        task = body.get("result", {}).get("task", {})
        return A2AInvocationResult(
            agent_id=agent.agent_id,
            task_id=task.get("id"),
            context_id=task.get("context_id") or task.get("contextId"),
            state=self._extract_state(task),
            text=self._extract_text(task),
            raw=body,
            agent_card=agent_card,
        )

    def _build_auth_headers(self, auth_strategy: str) -> dict[str, str]:
        strategy = auth_strategy or self.default_auth.auth_strategy
        if strategy == "none":
            return {}
        token = ""
        if self.default_auth.auth_token_env:
            token = os.getenv(self.default_auth.auth_token_env, "")
        if not token:
            return {}
        if strategy == "bearer":
            return {
                self.default_auth.auth_header_name: (
                    f"{self.default_auth.bearer_prefix} {token}".strip()
                )
            }
        if strategy == "header":
            return {self.default_auth.auth_header_name: token}
        return {}

    def _extract_state(self, task: dict) -> str:
        status = task.get("status") or {}
        if isinstance(status, dict):
            return str(status.get("state") or "completed")
        return str(status or "completed")

    def _extract_text(self, task: dict) -> str:
        history = task.get("history") or []
        for message in reversed(history):
            if str(message.get("role") or "").lower() != "agent":
                continue
            parts = message.get("parts") or []
            texts: list[str] = []
            for part in parts:
                if isinstance(part, dict) and part.get("kind") == "text":
                    texts.append(str(part.get("text") or ""))
            if texts:
                return "\n".join(texts).strip()
        return ""

