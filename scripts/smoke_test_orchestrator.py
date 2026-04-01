from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 Gateway -> Agno -> LiteLLM 端到端链路")
    parser.add_argument("--base-url", default="http://127.0.0.1:7777")
    parser.add_argument("--user-id", default="alice")
    parser.add_argument("--project-id", default="alpha")
    parser.add_argument("--allow-mock", action="store_true")
    args = parser.parse_args()

    with httpx.Client(timeout=60.0) as client:
        runtime = client.get(f"{args.base_url}/gateway/runtime-status")
        runtime.raise_for_status()
        runtime_payload = runtime.json()
        if not runtime_payload.get("live") and not args.allow_mock:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "step": "runtime-status",
                        "detail": runtime_payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

        token_res = client.get(f"{args.base_url}/gateway/dev-token/{args.user_id}")
        token_res.raise_for_status()
        token = token_res.json()["token"]

        chat = client.post(
            f"{args.base_url}/gateway/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "message": "请说明这次请求的 model_routes，并给出一条多用户隔离测试建议。",
                "project_id": args.project_id,
                "use_mock": False,
            },
        )
        chat.raise_for_status()
        payload = chat.json()
        ok = payload.get("mode") == "agno" or args.allow_mock
        print(
            json.dumps(
                {
                    "ok": ok,
                    "runtime": runtime_payload,
                    "chat": payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
