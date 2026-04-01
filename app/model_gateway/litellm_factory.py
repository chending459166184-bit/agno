from __future__ import annotations

from agno.models.litellm import LiteLLMOpenAI

from app.config import Settings


def build_agno_model(settings: Settings, alias: str) -> LiteLLMOpenAI:
    return LiteLLMOpenAI(
        id=alias,
        api_key=settings.litellm_master_key,
        base_url=settings.litellm_proxy_base_url,
        temperature=0.2,
    )
