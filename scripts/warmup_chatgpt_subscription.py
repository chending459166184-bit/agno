from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.codex_app_server_client import CodexAppServerClient
from app.auth import read_codex_bridge_user
from app.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="预热 coder-premium / ChatGPT Subscription 链路")
    parser.add_argument(
        "--device-auth",
        action="store_true",
        help="若当前本机未登录 Codex，则触发 codex login --device-auth",
    )
    parser.add_argument(
        "--prompt",
        default="Reply exactly with OK.",
        help="发给 coder-premium 的最小探测提示词",
    )
    args = parser.parse_args()

    settings = get_settings()
    try:
        user, identity = read_codex_bridge_user(settings)
    except Exception as exc:
        if not args.device_auth:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "step": "read_codex_auth",
                        "error": str(exc),
                        "hint": "可执行: python scripts/warmup_chatgpt_subscription.py --device-auth",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        subprocess.run(["codex", "login", "--device-auth"], check=True)
        user, identity = read_codex_bridge_user(settings)

    client = CodexAppServerClient(settings)
    result = client.complete(args.prompt)
    print(
        json.dumps(
            {
                "ok": True,
                "user_id": user.user_id,
                "email": identity.get("email"),
                "token_freshness": identity.get("token_freshness"),
                "provider_model": result.model,
                "provider": result.provider,
                "thread_id": result.thread_id,
                "turn_id": result.turn_id,
                "text": result.text,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
