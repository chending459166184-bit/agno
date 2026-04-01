from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.model_gateway import LiteLLMHealthChecker, ModelRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="逐个 alias 冒烟 LiteLLM Proxy")
    parser.add_argument("--require-all", action="store_true", help="要求所有已配置 alias 都通过")
    args = parser.parse_args()

    settings = get_settings()
    registry = ModelRegistry(settings)
    checker = LiteLLMHealthChecker(settings, registry)
    status = checker.probe(force_refresh=True)

    headers = {"Authorization": f"Bearer {settings.litellm_master_key}"}
    detailed_results: list[dict] = []
    success_count = 0
    with httpx.Client(timeout=settings.litellm_request_timeout_seconds) as client:
        for alias in registry.list_aliases():
            item = {
                "alias": alias.name,
                "configured": alias.configured(),
                "missing_env": alias.missing_env(),
            }
            if alias.configured() and status.proxy_reachable:
                try:
                    response = client.post(
                        f"{settings.litellm_proxy_base_url}/v1/chat/completions",
                        headers=headers,
                        json={
                            "model": alias.name,
                            "messages": [{"role": "user", "content": f"Reply with {alias.name}."}],
                            "max_tokens": 24,
                        },
                    )
                    response.raise_for_status()
                    body = response.json()
                    item["ok"] = True
                    item["text"] = (
                        body.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content")
                    )
                    success_count += 1
                except Exception as exc:
                    item["ok"] = False
                    item["error"] = str(exc)
            else:
                item["ok"] = False
                item["error"] = "alias 未配置或 LiteLLM Proxy 不可达"
            detailed_results.append(item)

    configured_count = len([alias for alias in registry.list_aliases() if alias.configured()])
    ok = success_count > 0 and (not args.require_all or success_count == configured_count)
    print(
        json.dumps(
            {
                "ok": ok,
                "proxy_status": status.as_dict(),
                "results": detailed_results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
