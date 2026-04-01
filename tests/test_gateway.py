from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from importlib import reload
from pathlib import Path

from fastapi.testclient import TestClient
import jwt

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def create_test_client(tmp_path: Path) -> TestClient:
    os.environ["DB_FILE"] = str(tmp_path / "app.db")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path / "workspaces")
    os.environ["EXTERNAL_AGENT_CATALOG_FILE"] = str(tmp_path / "external-agents" / "catalog.json")
    os.environ["EXTERNAL_AGENT_BASE_URL"] = "http://testserver"
    os.environ["ALLOW_MOCK_FALLBACK"] = "true"
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["OPENAI_API_BASE"] = ""
    os.environ["OPENAI_CODER_MODEL"] = "openai/gpt-5.3-codex"
    os.environ["MINIMAX_API_BASE"] = ""
    os.environ["MINIMAX_API_KEY"] = ""
    os.environ["MINIMAX_MODEL_ID"] = ""
    os.environ["ZAI_API_BASE"] = ""
    os.environ["ZAI_API_KEY"] = ""
    os.environ["ZAI_MODEL_ID"] = ""
    os.environ["LITELLM_PROXY_BASE_URL"] = "http://127.0.0.1:9"
    os.environ["LITELLM_REQUEST_TIMEOUT_SECONDS"] = "0.2"

    import app.config
    import app.main

    app.config.get_settings.cache_clear()
    reload(app.main)
    return TestClient(app.main.create_app())


def write_codex_auth_file(tmp_path: Path) -> Path:
    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    id_token = jwt.encode(
        {
            "sub": "codex-user-123",
            "email": "bridge@example.com",
            "name": "Bridge User",
            "exp": exp,
            "iat": exp - 3600,
            "iss": "https://auth.example.com/",
            "aud": "codex",
        },
        "dummy-secret-for-tests-2026-bridge",
        algorithm="HS256",
    )
    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_text(
        (
            '{'
            '"auth_mode":"chatgpt",'
            '"OPENAI_API_KEY":null,'
            '"tokens":{'
            f'"id_token":"{id_token}",'
            '"access_token":"access",'
            '"refresh_token":"refresh",'
            '"account_id":"acct_123"'
            '},'
            '"last_refresh":"2026-03-31T00:00:00Z"'
            '}'
        ),
        encoding="utf-8",
    )
    return auth_file


def test_alice_only_sees_alpha_scope(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "message": "请结合我的知识库和个人空间文件，给我测试建议。",
            "project_id": "alpha",
            "use_mock": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "mock"
    assert body["user_id"] == "alice"
    assert body["model_routes"]["orchestrate"] == "coder-premium"
    assert body["model_routes"]["external_broker"] == "coder-premium"
    assert all(hit["scope_id"] != "beta" for hit in body["knowledge_hits"])
    assert "alice" in body["workspace_root"]


def test_bob_cannot_access_alpha_project(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/bob").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "看看 Alpha 项目的内容", "project_id": "alpha", "use_mock": True},
    )
    assert response.status_code == 403


def test_workspace_endpoint_is_user_isolated(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    alice_token = client.get("/gateway/dev-token/alice").json()["token"]
    bob_token = client.get("/gateway/dev-token/bob").json()["token"]

    alice_files = client.get(
        "/gateway/workspace", headers={"Authorization": f"Bearer {alice_token}"}
    ).json()["files"]
    bob_files = client.get(
        "/gateway/workspace", headers={"Authorization": f"Bearer {bob_token}"}
    ).json()["files"]

    alice_paths = {item["path"] for item in alice_files}
    bob_paths = {item["path"] for item in bob_files}
    assert "notes/customer-risk.md" in alice_paths
    assert "notes/beta-todo.md" in bob_paths
    assert "notes/beta-todo.md" not in alice_paths


def test_codex_bridge_login_creates_internal_user(tmp_path: Path) -> None:
    auth_file = write_codex_auth_file(tmp_path)
    os.environ["CODEX_AUTH_FILE"] = str(auth_file)

    client = create_test_client(tmp_path)

    status = client.get("/gateway/codex-status")
    assert status.status_code == 200
    assert status.json()["available"] is True

    login = client.post("/gateway/codex-login")
    assert login.status_code == 200
    body = login.json()
    assert body["bridge_mode"] == "codex_auth_json"
    assert body["user"]["user_id"] == "codex-bridge"

    token = body["token"]
    workspace = client.get(
        "/gateway/workspace",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert workspace.status_code == 200
    paths = {item["path"] for item in workspace.json()["files"]}
    assert "notes/codex-bridge.md" in paths


def test_runtime_status_is_based_on_litellm_proxy_health(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    response = client.get("/gateway/runtime-status")
    assert response.status_code == 200
    body = response.json()
    assert body["live"] is False
    assert body["live_model_configured"] is False
    assert body["proxy_reachable"] is False
    assert body["router_defaults"]["orchestrate"] == "coder-premium"
    assert body["aliases"]
    assert body["external_agents"]["count"] >= 1


def test_external_agents_endpoint_returns_discovered_agents(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    response = client.get(
        "/gateway/external-agents",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    agent_ids = {item["agent_id"] for item in body["agents"]}
    assert "compliance-reviewer" in agent_ids
