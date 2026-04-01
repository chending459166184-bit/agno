from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.model_gateway import ModelRegistry, ModelRouter


def test_router_prefers_configured_aliases_when_keys_exist() -> None:
    os.environ["OPENAI_API_KEY"] = "test-openai-key"
    os.environ["OPENAI_CODER_MODEL"] = "openai/test-coder"
    os.environ["MINIMAX_API_KEY"] = ""
    os.environ["MINIMAX_API_BASE"] = ""
    os.environ["MINIMAX_MODEL_ID"] = ""
    get_settings.cache_clear()

    settings = get_settings()
    registry = ModelRegistry(settings)
    router = ModelRouter(registry)

    route = router.resolve("general")
    assert route.alias == "coder-api"


def test_router_can_fallback_to_coder_premium_without_tool_support() -> None:
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["OPENAI_CODER_MODEL"] = "openai/gpt-5.3-codex"
    os.environ["MINIMAX_API_KEY"] = ""
    os.environ["MINIMAX_API_BASE"] = ""
    os.environ["MINIMAX_MODEL_ID"] = ""
    os.environ["CODER_PREMIUM_ADAPTER_KEY"] = "local-coder-premium-key"
    get_settings.cache_clear()

    settings = get_settings()
    registry = ModelRegistry(settings)
    router = ModelRouter(registry)

    route = router.resolve("workspace", preferred_aliases={"coder-premium"})
    assert route.alias == "coder-premium"
    assert route.reason == "matched_healthy_alias_without_tool_support"
