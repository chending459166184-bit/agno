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
    os.environ["MCP_ALLOW_WRITE"] = "true"
    os.environ["EXTERNAL_PREFETCH_ENABLED"] = "true"
    os.environ["EXTERNAL_PREFETCH_MODE"] = "prefetch"
    os.environ["EXEC_SANDBOX_ENABLED"] = "true"
    os.environ["EXEC_SANDBOX_MODE"] = "process"
    os.environ["EXEC_JOBS_ROOT"] = str(tmp_path / "exec-jobs")
    os.environ["EXEC_DEFAULT_TIMEOUT_SECONDS"] = "5"
    os.environ["EXEC_MAX_TIMEOUT_SECONDS"] = "20"
    os.environ["EXEC_DEFAULT_MEMORY_MB"] = "256"
    os.environ["EXEC_MAX_MEMORY_MB"] = "512"
    os.environ["EXEC_DEFAULT_CPU_LIMIT"] = "1.0"
    os.environ["EXEC_ALLOW_NETWORK"] = "false"
    os.environ["EXEC_ALLOW_WORKSPACE_WRITEBACK"] = "true"
    os.environ["EXEC_MAX_STDOUT_CHARS"] = "20000"
    os.environ["EXEC_MAX_STDERR_CHARS"] = "20000"
    os.environ["WORKSPACE_GUARD_ENABLED"] = "true"
    os.environ["EXECUTION_GUARD_ENABLED"] = "true"

    import app.config
    import app.main

    app.config.get_settings.cache_clear()
    reload(app.main)
    return TestClient(app.main.create_app())


def patch_workspace_guard_composer(monkeypatch, *, answer: str | None = None, error: Exception | None = None) -> None:
    import app.runtime as runtime_module

    def _fake_compose(*args, **kwargs):
        if error is not None:
            raise error
        return answer or "这是一个自然语言整理结果。"

    monkeypatch.setattr(runtime_module.guard_response, "compose_workspace_guard_answer", _fake_compose)


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
    assert body["mcp_allow_write"] is True
    assert body["external_prefetch"]["mode"] == "prefetch"
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


def test_dev_users_and_knowledge_endpoint_expose_richer_debug_fields(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)

    users = client.get("/gateway/dev-users")
    assert users.status_code == 200
    user_ids = {item["user_id"] for item in users.json()["users"]}
    assert {"alice", "bob", "charlie"} <= user_ids

    token = client.get("/gateway/dev-token/alice").json()["token"]
    response = client.get(
        "/gateway/knowledge",
        params={"q": "运维 手册", "project_id": "beta"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "alice"
    assert body["project_id"] == "beta"
    assert body["query"] == "运维 手册"
    assert body["hit_count"] >= 1
    assert any(hit["scope_id"] == "beta" for hit in body["hits"])


def test_workspace_mcp_probe_can_write_and_trace_summary_shows_timeline(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    write_res = client.post(
        "/gateway/debug/workspace-mcp/write",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "project_id": "alpha",
            "path": "notes/mcp-probe.txt",
            "content": "hello from mcp",
            "overwrite": True,
        },
    )
    assert write_res.status_code == 200
    write_body = write_res.json()
    assert write_body["ok"] is True
    assert write_body["path"] == "notes/mcp-probe.txt"

    read_res = client.post(
        "/gateway/debug/workspace-mcp/read",
        headers={"Authorization": f"Bearer {token}"},
        json={"project_id": "alpha", "path": "notes/mcp-probe.txt"},
    )
    assert read_res.status_code == 200
    assert "hello from mcp" in read_res.json()["content"]

    summary = client.get(f"/gateway/trace/{write_body['trace_id']}/summary")
    assert summary.status_code == 200
    timeline = summary.json()["audit_timeline"]
    assert any(item["event_type"] == "mcp_tool_call" for item in timeline)


def test_agent_config_override_changes_effective_agents(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    before = client.get(
        "/gateway/agent-configs/effective",
        params={"project_id": "alpha"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert before.status_code == 200
    items = {item["agent_key"]: item for item in before.json()["items"]}
    assert items["workspace_agent"]["included_in_team"] is True

    update = client.put(
        "/gateway/agent-configs/workspace_agent",
        headers={"Authorization": f"Bearer {token}"},
        json={"project_id": "alpha", "enabled": False, "allow_auto_route": False, "priority": 10},
    )
    assert update.status_code == 200

    after = client.get(
        "/gateway/agent-configs/effective",
        params={"project_id": "alpha"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert after.status_code == 200
    items = {item["agent_key"]: item for item in after.json()["items"]}
    assert items["workspace_agent"]["enabled"] is False
    assert items["workspace_agent"]["included_in_team"] is False


def test_chat_file_intent_is_forced_through_workspace_guard(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/charlie").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "当前我目录下有哪些文件", "project_id": "alpha"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "workspace_guard"
    assert body["user_id"] == "charlie"
    assert body["workspace_root"].endswith("/demo/charlie")
    assert body["selected_agents"] == ["Workspace Agent"]
    assert body["member_outputs"]
    assert any(item["phase"] == "workspace_guard_data" for item in body["member_outputs"])
    assert "notes/alpha-analysis.md" in body["answer"]
    assert ".env" not in body["answer"]
    assert "app/" not in body["answer"]

    summary = client.get(f"/gateway/trace/{body['trace_id']}/summary")
    assert summary.status_code == 200
    timeline = summary.json()["audit_timeline"]
    assert any(item["event_type"] == "mcp_tool_call" for item in timeline)
    assert any(
        item["payload"].get("tool_name") == "workspace_list_files"
        for item in timeline
        if item["event_type"] == "mcp_tool_call"
    )


def test_chat_shorter_file_phrase_still_hits_workspace_guard(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "我目录有哪些文件", "project_id": "alpha"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "workspace_guard"
    assert body["selected_agents"] == ["Workspace Agent"]
    assert ".env" not in body["answer"]
    assert "app/" not in body["answer"]
    assert "notes/customer-risk.md" in body["answer"] or "notes/beta-handoff.md" in body["answer"]


def test_chat_workspace_space_phrase_does_not_get_misrouted_to_execution(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "我空间有什么文件？", "project_id": "alpha"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "workspace_guard"
    assert body["selected_agents"] == ["Workspace Agent"]
    assert ".env" not in body["answer"]
    assert "当前未能安全完成代码执行" not in body["answer"]


def test_workspace_guard_list_compose_success(monkeypatch, tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    patch_workspace_guard_composer(
        monkeypatch,
        answer="我在你当前工作区里看到了 1 个文件：`notes/alpha-analysis.md`。如果你愿意，我可以继续读取它的内容。",
    )
    token = client.get("/gateway/dev-token/charlie").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "当前我目录下有哪些文件", "project_id": "alpha"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "workspace_guard"
    assert body["selected_agents"] == ["Workspace Agent"]
    phases = [item["phase"] for item in body["member_outputs"]]
    assert "workspace_guard_data" in phases
    assert "workspace_guard_compose" in phases
    assert "workspace_guard_fallback" not in phases
    assert body["answer"] == "我在你当前工作区里看到了 1 个文件：`notes/alpha-analysis.md`。如果你愿意，我可以继续读取它的内容。"
    assert not body["answer"].startswith("Workspace Agent 已通过 MCP")
    assert "notes/alpha-analysis.md" in body["answer"]
    assert ".env" not in body["answer"]


def test_workspace_guard_compose_failure_falls_back(monkeypatch, tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    patch_workspace_guard_composer(monkeypatch, error=RuntimeError("composer unavailable"))
    token = client.get("/gateway/dev-token/alice").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "我目录有哪些文件", "project_id": "alpha"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "workspace_guard"
    phases = [item["phase"] for item in body["member_outputs"]]
    assert "workspace_guard_data" in phases
    assert "workspace_guard_compose_failed" in phases
    assert "workspace_guard_fallback" in phases
    assert any("composer failed, fallback to template" in note for note in body["notes"])
    assert "Workspace Agent 已通过 MCP" in body["answer"]
    assert ".env" not in body["answer"]


def test_workspace_guard_read_uses_compose(monkeypatch, tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    patch_workspace_guard_composer(
        monkeypatch,
        answer="我已经读取了 `notes/alpha-analysis.md`。这份笔记主要在讲 Charlie 对 alpha 项目的分析视角，以及如何验证项目知识和个人知识的隔离。",
    )
    token = client.get("/gateway/dev-token/charlie").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "读取 notes/alpha-analysis.md", "project_id": "alpha"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "workspace_guard"
    phases = [item["phase"] for item in body["member_outputs"]]
    assert "workspace_guard_data" in phases
    assert "workspace_guard_compose" in phases
    assert body["answer"].startswith("我已经读取了 `notes/alpha-analysis.md`")
    assert "/Users/chending/Downloads/agno-test-project" not in body["answer"]


def test_workspace_guard_write_compose_failure_falls_back(monkeypatch, tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    patch_workspace_guard_composer(monkeypatch, error=RuntimeError("compose timeout"))
    token = client.get("/gateway/dev-token/alice").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "写入 drafts/result.txt 内容: hello world", "project_id": "alpha"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "workspace_guard"
    phases = [item["phase"] for item in body["member_outputs"]]
    assert "workspace_guard_data" in phases
    assert "workspace_guard_compose_failed" in phases
    assert "workspace_guard_fallback" in phases
    assert "drafts/result.txt" in body["answer"]


def test_execution_run_succeeds_and_records_trace(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/charlie").json()["token"]

    response = client.post(
        "/gateway/exec/run",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "project_id": "alpha",
            "language": "python",
            "command": "python inline_task.py",
            "files": [{"path": "inline_task.py", "content": "print('hello sandbox')"}],
            "timeout_seconds": 5,
            "writeback": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["job"]["status"] == "success"
    assert "hello sandbox" in body["stdout"]

    summary = client.get(f"/gateway/trace/{body['trace_id']}/summary")
    assert summary.status_code == 200
    event_types = [item["event_type"] for item in summary.json()["audit_timeline"]]
    assert "sandbox_job_created" in event_types
    assert "sandbox_started" in event_types
    assert "sandbox_completed" in event_types


def test_execution_timeout_is_enforced(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/charlie").json()["token"]

    response = client.post(
        "/gateway/exec/run",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "project_id": "alpha",
            "language": "python",
            "command": "python loop.py",
            "files": [{"path": "loop.py", "content": "while True:\n    pass\n"}],
            "timeout_seconds": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["job"]["status"] == "timeout"


def test_execution_cannot_see_repo_root(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    response = client.post(
        "/gateway/exec/run",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "project_id": "alpha",
            "language": "python",
            "command": "python inspect.py",
            "files": [
                {
                    "path": "inspect.py",
                    "content": (
                        "import json, os\n"
                        "targets = {name: os.path.exists(name) for name in ['.env', 'app', 'configs', 'notes/customer-risk.md']}\n"
                        "print(json.dumps(targets, ensure_ascii=False))\n"
                    ),
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["job"]["status"] == "success"
    assert '".env": false' in body["stdout"].lower()
    assert '"app": false' in body["stdout"].lower()
    assert '"configs": false' in body["stdout"].lower()
    assert '"notes/customer-risk.md": true' in body["stdout"].lower()


def test_execution_is_user_isolated(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    alice_token = client.get("/gateway/dev-token/alice").json()["token"]
    bob_token = client.get("/gateway/dev-token/bob").json()["token"]

    alice_res = client.post(
        "/gateway/exec/run",
        headers={"Authorization": f"Bearer {alice_token}"},
        json={
            "project_id": "alpha",
            "language": "python",
            "command": "python inspect.py",
            "files": [
                {
                    "path": "inspect.py",
                    "content": (
                        "import os\n"
                        "print(os.path.exists('notes/customer-risk.md'))\n"
                        "print(os.path.exists('notes/beta-todo.md'))\n"
                    ),
                }
            ],
        },
    )
    assert alice_res.status_code == 200
    assert "True\nFalse" in alice_res.json()["stdout"]

    bob_res = client.post(
        "/gateway/exec/run",
        headers={"Authorization": f"Bearer {bob_token}"},
        json={
            "project_id": "beta",
            "language": "python",
            "command": "python inspect.py",
            "files": [
                {
                    "path": "inspect.py",
                    "content": (
                        "import os\n"
                        "print(os.path.exists('notes/customer-risk.md'))\n"
                        "print(os.path.exists('notes/beta-todo.md'))\n"
                    ),
                }
            ],
        },
    )
    assert bob_res.status_code == 200
    assert "False\nTrue" in bob_res.json()["stdout"]


def test_execution_network_is_disabled_by_default(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    response = client.post(
        "/gateway/exec/run",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "project_id": "alpha",
            "language": "python",
            "command": "python net.py",
            "files": [
                {
                    "path": "net.py",
                    "content": (
                        "from urllib.request import urlopen\n"
                        "print(urlopen('https://example.com', timeout=2).read()[:10])\n"
                    ),
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["job"]["status"] == "failed"
    assert "Network access is disabled" in body["stderr"]


def test_execution_writeback_respects_policy(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/alice").json()["token"]

    no_writeback = client.post(
        "/gateway/exec/run",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "project_id": "alpha",
            "language": "python",
            "command": "python create.py",
            "files": [
                {
                    "path": "create.py",
                    "content": "from pathlib import Path\nPath('notes/no-writeback.txt').write_text('sandbox only', encoding='utf-8')\n",
                }
            ],
            "writeback": False,
        },
    )
    assert no_writeback.status_code == 200
    workspace = client.get("/gateway/workspace", headers={"Authorization": f"Bearer {token}"}).json()
    paths = {item["path"] for item in workspace["files"]}
    assert "notes/no-writeback.txt" not in paths

    with_writeback = client.post(
        "/gateway/exec/run",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "project_id": "alpha",
            "language": "python",
            "command": "python create2.py",
            "files": [
                {
                    "path": "create2.py",
                    "content": "from pathlib import Path\nPath('notes/with-writeback.txt').write_text('persisted', encoding='utf-8')\n",
                }
            ],
            "writeback": True,
        },
    )
    assert with_writeback.status_code == 200
    workspace = client.get("/gateway/workspace", headers={"Authorization": f"Bearer {token}"}).json()
    paths = {item["path"] for item in workspace["files"]}
    assert "notes/with-writeback.txt" in paths


def test_chat_execution_intent_is_forced_through_execution_guard(tmp_path: Path) -> None:
    client = create_test_client(tmp_path)
    token = client.get("/gateway/dev-token/charlie").json()["token"]

    response = client.post(
        "/gateway/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "message": "帮我运行这段 Python 代码并告诉我输出：\n```python\nprint('hello guard')\n```",
            "project_id": "alpha",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "execution_guard"
    assert body["selected_agents"] == ["Execution Agent"]
    assert "hello guard" in body["answer"]

    summary = client.get(f"/gateway/trace/{body['trace_id']}/summary")
    assert summary.status_code == 200
    event_types = [item["event_type"] for item in summary.json()["audit_timeline"]]
    assert "sandbox_started" in event_types
