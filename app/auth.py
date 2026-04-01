from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from app.config import Settings
from app.context import AuthenticatedUser


def issue_demo_token(settings: Settings, user: AuthenticatedUser) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.user_id,
        "tenant_id": user.tenant_id,
        "user_id": user.user_id,
        "display_name": user.display_name,
        "role": user.role,
        "project_ids": user.project_ids,
        "default_project_id": user.default_project_id,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=12)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(settings: Settings, token: str) -> AuthenticatedUser:
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )
    return AuthenticatedUser(
        tenant_id=payload["tenant_id"],
        user_id=payload["user_id"],
        display_name=payload["display_name"],
        role=payload["role"],
        project_ids=list(payload.get("project_ids", [])),
        default_project_id=payload["default_project_id"],
    )


def sanitize_user_id(raw: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.strip().lower()).strip("-")
    return value or "codex-user"


def _read_codex_auth_file(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"未找到 Codex 认证文件: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def read_codex_bridge_user(settings: Settings) -> tuple[AuthenticatedUser, dict]:
    if not settings.codex_bridge_enabled:
        raise ValueError("当前未开启 Codex 登录态桥接")

    payload = _read_codex_auth_file(settings.resolved_codex_auth_file)
    tokens = payload.get("tokens") or {}
    id_token = tokens.get("id_token")
    if not id_token:
        raise ValueError("Codex auth.json 中没有可用的 id_token")

    claims = jwt.decode(
        id_token,
        options={
            "verify_signature": False,
            "verify_exp": False,
            "verify_iat": False,
            "verify_aud": False,
            "verify_iss": False,
        },
        algorithms=["HS256", "RS256", "ES256"],
    )
    exp = claims.get("exp")
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if not isinstance(exp, int):
        raise ValueError("Codex id_token 缺少 exp，无法桥接")
    token_freshness = "fresh"
    if exp <= now_ts:
        if tokens.get("refresh_token") or tokens.get("access_token"):
            token_freshness = "stale"
        else:
            raise ValueError("Codex 登录态已过期，请先执行 codex login 刷新会话")

    email = claims.get("email") or ""
    name = claims.get("name") or email or "Codex User"
    sub = claims.get("sub") or "unknown"
    local_part = email.split("@", 1)[0] if "@" in email else sub[-12:]
    user_id = f"codex-{sanitize_user_id(local_part)}"
    project_ids = settings.codex_bridge_project_ids_list or [settings.default_project_id]
    user = AuthenticatedUser(
        tenant_id=settings.default_tenant_id,
        user_id=user_id,
        display_name=str(name),
        role=settings.codex_bridge_default_role,
        project_ids=project_ids,
        default_project_id=settings.effective_codex_default_project_id,
    )
    identity = {
        "source": "codex_auth_json",
        "auth_mode": payload.get("auth_mode"),
        "account_id": tokens.get("account_id"),
        "email": email,
        "name": name,
        "sub": sub,
        "exp": exp,
        "token_freshness": token_freshness,
        "iss": claims.get("iss"),
        "aud": claims.get("aud"),
    }
    return user, identity
