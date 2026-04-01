from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
)
from sqlalchemy.engine import Engine

from app.context import AuthenticatedUser, RequestContext
from app.workspace import ensure_workspace, save_text_file


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tokenize(text: str) -> set[str]:
    import re

    lowered = (text or "").lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for block in re.findall(r"[\u4e00-\u9fff]{1,}", lowered):
        tokens.add(block)
        if len(block) > 1:
            tokens.update(block[idx : idx + 2] for idx in range(len(block) - 1))
        if len(block) > 2:
            tokens.update(block[idx : idx + 3] for idx in range(len(block) - 2))
    return {token for token in tokens if token.strip()}


class Database:
    def __init__(self, db_file: Path) -> None:
        self.db_file = db_file
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.db_file}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        self.metadata = MetaData()
        self.users = Table(
            "users",
            self.metadata,
            Column("user_id", String, primary_key=True),
            Column("tenant_id", String, nullable=False),
            Column("display_name", String, nullable=False),
            Column("role", String, nullable=False),
            Column("project_ids_json", JSON, nullable=False),
            Column("default_project_id", String, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.session_contexts = Table(
            "session_contexts",
            self.metadata,
            Column("session_id", String, primary_key=True),
            Column("tenant_id", String, nullable=False),
            Column("user_id", String, nullable=False),
            Column("project_id", String, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self.runs = Table(
            "runs",
            self.metadata,
            Column("run_id", String, primary_key=True),
            Column("trace_id", String, nullable=False),
            Column("request_id", String, nullable=False),
            Column("session_id", String, nullable=False),
            Column("tenant_id", String, nullable=False),
            Column("user_id", String, nullable=False),
            Column("project_id", String, nullable=False),
            Column("team_id", String, nullable=False),
            Column("selected_agents_json", JSON, nullable=False),
            Column("status", String, nullable=False),
            Column("input_text", Text, nullable=False),
            Column("output_text", Text, nullable=False),
            Column("mode", String, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.contents = Table(
            "contents",
            self.metadata,
            Column("content_id", String, primary_key=True),
            Column("tenant_id", String, nullable=False),
            Column("scope_type", String, nullable=False),
            Column("scope_id", String, nullable=False),
            Column("owner_user_id", String, nullable=True),
            Column("project_id", String, nullable=True),
            Column("title", String, nullable=False),
            Column("body_text", Text, nullable=False),
            Column("metadata_json", JSON, nullable=False),
            Column("status", String, nullable=False),
            Column("source_path", String, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self.content_chunks = Table(
            "content_chunks",
            self.metadata,
            Column("chunk_id", String, primary_key=True),
            Column("content_id", String, nullable=False),
            Column("tenant_id", String, nullable=False),
            Column("scope_type", String, nullable=False),
            Column("scope_id", String, nullable=False),
            Column("chunk_order", String, nullable=False),
            Column("chunk_text", Text, nullable=False),
            Column("token_index_json", JSON, nullable=False),
            Column("metadata_json", JSON, nullable=False),
        )
        self.audit_logs = Table(
            "audit_logs",
            self.metadata,
            Column("audit_id", String, primary_key=True),
            Column("trace_id", String, nullable=False),
            Column("request_id", String, nullable=False),
            Column("session_id", String, nullable=True),
            Column("tenant_id", String, nullable=True),
            Column("user_id", String, nullable=True),
            Column("event_type", String, nullable=False),
            Column("payload_json", JSON, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )

    def init_schema(self) -> None:
        self.metadata.create_all(self.engine)

    def reset_demo_data(self) -> None:
        with self.engine.begin() as conn:
            for table in (
                self.audit_logs,
                self.content_chunks,
                self.contents,
                self.runs,
                self.session_contexts,
                self.users,
            ):
                conn.execute(delete(table))

    def upsert_user(self, user: AuthenticatedUser) -> None:
        values = {
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "display_name": user.display_name,
            "role": user.role,
            "project_ids_json": user.project_ids,
            "default_project_id": user.default_project_id,
            "created_at": utcnow(),
        }
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(self.users.c.user_id).where(self.users.c.user_id == user.user_id)
            ).first()
            if existing:
                conn.execute(
                    self.users.update()
                    .where(self.users.c.user_id == user.user_id)
                    .values(**values)
                )
            else:
                conn.execute(insert(self.users).values(**values))

    def list_users(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(select(self.users)).mappings().all()
        return [dict(row) for row in rows]

    def get_user(self, user_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.users).where(self.users.c.user_id == user_id)
            ).mappings().first()
        return dict(row) if row else None

    def has_content_source(self, source_path: str) -> bool:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.contents.c.content_id).where(self.contents.c.source_path == source_path)
            ).first()
        return row is not None

    def touch_session_context(self, ctx: RequestContext) -> None:
        now = utcnow()
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(self.session_contexts.c.session_id).where(
                    self.session_contexts.c.session_id == ctx.session_id
                )
            ).first()
            payload = {
                "session_id": ctx.session_id,
                "tenant_id": ctx.tenant_id,
                "user_id": ctx.user_id,
                "project_id": ctx.project_id,
                "updated_at": now,
            }
            if existing:
                conn.execute(
                    self.session_contexts.update()
                    .where(self.session_contexts.c.session_id == ctx.session_id)
                    .values(**payload)
                )
            else:
                conn.execute(
                    insert(self.session_contexts).values(
                        **payload,
                        created_at=now,
                    )
                )

    def append_audit(
        self,
        *,
        trace_id: str,
        request_id: str,
        session_id: str | None,
        tenant_id: str | None,
        user_id: str | None,
        event_type: str,
        payload: dict,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.audit_logs).values(
                    audit_id=f"audit_{uuid4().hex[:18]}",
                    trace_id=trace_id,
                    request_id=request_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    event_type=event_type,
                    payload_json=payload,
                    created_at=utcnow(),
                )
            )

    def list_audit_events(self, trace_id: str) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.audit_logs)
                .where(self.audit_logs.c.trace_id == trace_id)
                .order_by(self.audit_logs.c.created_at.asc())
            ).mappings().all()
        return [dict(row) for row in rows]

    def create_run(
        self,
        *,
        ctx: RequestContext,
        input_text: str,
        output_text: str,
        status: str,
        mode: str,
        selected_agents: list[str],
    ) -> str:
        run_id = f"run_{uuid4().hex[:18]}"
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.runs).values(
                    run_id=run_id,
                    trace_id=ctx.trace_id,
                    request_id=ctx.request_id,
                    session_id=ctx.session_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    project_id=ctx.project_id,
                    team_id="enterprise_orchestrator",
                    selected_agents_json=selected_agents,
                    status=status,
                    input_text=input_text,
                    output_text=output_text,
                    mode=mode,
                    created_at=utcnow(),
                )
            )
        return run_id

    def ingest_document(
        self,
        *,
        tenant_id: str,
        scope_type: str,
        scope_id: str,
        owner_user_id: str | None,
        project_id: str | None,
        title: str,
        source_path: str,
        body_text: str,
        metadata: dict,
    ) -> str:
        content_id = f"content_{uuid4().hex[:18]}"
        now = utcnow()
        chunks = chunk_text(body_text)
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.contents).values(
                    content_id=content_id,
                    tenant_id=tenant_id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    title=title,
                    body_text=body_text,
                    metadata_json=metadata,
                    status="published",
                    source_path=source_path,
                    created_at=now,
                    updated_at=now,
                )
            )
            for index, chunk in enumerate(chunks, start=1):
                conn.execute(
                    insert(self.content_chunks).values(
                        chunk_id=f"chunk_{uuid4().hex[:18]}",
                        content_id=content_id,
                        tenant_id=tenant_id,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        chunk_order=str(index),
                        chunk_text=chunk,
                        token_index_json=sorted(tokenize(chunk)),
                        metadata_json=metadata,
                    )
                )
        return content_id

    def search_knowledge(
        self,
        *,
        tenant_id: str,
        user_id: str,
        project_id: str,
        query: str,
        limit: int = 4,
    ) -> list[dict]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    self.content_chunks.c.chunk_id,
                    self.content_chunks.c.content_id,
                    self.content_chunks.c.scope_type,
                    self.content_chunks.c.scope_id,
                    self.content_chunks.c.chunk_text,
                    self.content_chunks.c.token_index_json,
                    self.contents.c.title,
                    self.contents.c.source_path,
                    self.contents.c.metadata_json,
                ).join(
                    self.contents,
                    self.content_chunks.c.content_id == self.contents.c.content_id,
                ).where(
                    self.content_chunks.c.tenant_id == tenant_id,
                    self.contents.c.status == "published",
                )
            ).mappings().all()

        scored: list[dict] = []
        for row in rows:
            scope_type = row["scope_type"]
            scope_id = row["scope_id"]
            if scope_type == "project" and scope_id != project_id:
                continue
            if scope_type == "personal" and scope_id != user_id:
                continue
            chunk_tokens = set(row["token_index_json"] or [])
            score = len(query_tokens & chunk_tokens)
            if score <= 0:
                continue
            chunk_text = row["chunk_text"]
            scored.append(
                {
                    "content_id": row["content_id"],
                    "title": row["title"],
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "score": score,
                    "snippet": chunk_text[:240].strip(),
                    "source_path": row["source_path"],
                    "metadata": row["metadata_json"],
                }
            )
        scored.sort(key=lambda item: (-item["score"], item["title"]))
        return scored[:limit]

    def seed_demo_data(
        self,
        project_root: Path,
        workspace_root: Path,
        external_agent_catalog_file: Path | None = None,
    ) -> None:
        self.reset_demo_data()
        demo_users = [
            AuthenticatedUser(
                tenant_id="demo",
                user_id="alice",
                display_name="Alice Chen",
                role="manager",
                project_ids=["alpha"],
                default_project_id="alpha",
            ),
            AuthenticatedUser(
                tenant_id="demo",
                user_id="bob",
                display_name="Bob Li",
                role="tester",
                project_ids=["beta"],
                default_project_id="beta",
            ),
        ]
        for user in demo_users:
            self.upsert_user(user)

        docs_dir = project_root / "docs" / "seed"
        doc_specs = [
            {
                "path": docs_dir / "project_alpha_requirements.md",
                "scope_type": "project",
                "scope_id": "alpha",
                "project_id": "alpha",
                "owner_user_id": None,
                "title": "Alpha 项目需求说明",
            },
            {
                "path": docs_dir / "project_alpha_test_baseline.md",
                "scope_type": "project",
                "scope_id": "alpha",
                "project_id": "alpha",
                "owner_user_id": None,
                "title": "Alpha 项目测试基线",
            },
            {
                "path": docs_dir / "project_beta_ops_runbook.md",
                "scope_type": "project",
                "scope_id": "beta",
                "project_id": "beta",
                "owner_user_id": None,
                "title": "Beta 项目运维手册",
            },
        ]
        for spec in doc_specs:
            self.ingest_document(
                tenant_id="demo",
                scope_type=spec["scope_type"],
                scope_id=spec["scope_id"],
                owner_user_id=spec["owner_user_id"],
                project_id=spec["project_id"],
                title=spec["title"],
                source_path=str(spec["path"].relative_to(project_root)),
                body_text=spec["path"].read_text(encoding="utf-8"),
                metadata={
                    "tenant_id": "demo",
                    "scope_type": spec["scope_type"],
                    "scope_id": spec["scope_id"],
                    "project_id": spec["project_id"],
                    "status": "published",
                    "classification": "internal",
                },
            )

        personal_notes = {
            "alice": {
                "notes/customer-risk.md": (
                    "# Alpha 客户风险记录\n\n"
                    "- 客户希望 4 月中旬前完成试点。\n"
                    "- 关注点是多用户隔离、审计追踪、测试建议是否可落地。\n"
                    "- 演示时要重点展示用户空间文件隔离。"
                ),
                "drafts/test-focus.txt": (
                    "优先验证登录隔离、知识过滤、MCP 文件读取、审计链路。"
                ),
            },
            "bob": {
                "notes/beta-todo.md": (
                    "# Beta 待办\n\n"
                    "- 检查夜间批处理告警。\n"
                    "- 补充接口超时告警阈值。\n"
                    "- Beta 项目不要暴露给 Alpha 用户。"
                ),
            },
        }
        for user_id, files in personal_notes.items():
            user_root = ensure_workspace(workspace_root / "demo" / user_id)
            for rel_path, content in files.items():
                save_text_file(user_root, rel_path, content, overwrite=True)
                self.ingest_document(
                    tenant_id="demo",
                    scope_type="personal",
                    scope_id=user_id,
                    owner_user_id=user_id,
                    project_id=None,
                    title=f"{user_id}::{rel_path}",
                    source_path=f"workspace/{user_id}/{rel_path}",
                    body_text=content,
                    metadata={
                        "tenant_id": "demo",
                        "scope_type": "personal",
                        "scope_id": user_id,
                        "owner_user_id": user_id,
                        "status": "published",
                        "classification": "private",
                    },
                )
        catalog_file = external_agent_catalog_file or (
            project_root / "data" / "external_agents" / "catalog.json"
        )
        self.ensure_demo_external_agent_catalog(catalog_file)

    def provision_codex_bridge_user(
        self,
        *,
        user: AuthenticatedUser,
        workspace_root: Path,
        identity: dict,
    ) -> None:
        existing = self.get_user(user.user_id)
        self.upsert_user(user)
        user_root = ensure_workspace(workspace_root / user.tenant_id / user.user_id)
        rel_path = "notes/codex-bridge.md"
        absolute_path = user_root / rel_path
        if not absolute_path.exists():
            content = (
                f"# Codex 登录态桥接说明\n\n"
                f"- 来源: {identity.get('source', 'codex_auth_json')}\n"
                f"- 显示名称: {identity.get('name') or user.display_name}\n"
                f"- 邮箱: {identity.get('email') or 'unknown'}\n"
                f"- 默认项目: {user.default_project_id}\n"
                f"- 允许项目: {', '.join(user.project_ids)}\n"
                f"- 说明: 当前用户是通过本机 Codex 登录态桥接进入本地 Agno 平台的。"
            )
            save_text_file(user_root, rel_path, content, overwrite=True)
        source_path = f"workspace/{user.user_id}/{rel_path}"
        if existing is None and not self.has_content_source(source_path):
            self.ingest_document(
                tenant_id=user.tenant_id,
                scope_type="personal",
                scope_id=user.user_id,
                owner_user_id=user.user_id,
                project_id=None,
                title=f"{user.user_id}::{rel_path}",
                source_path=source_path,
                body_text=absolute_path.read_text(encoding="utf-8"),
                metadata={
                    "tenant_id": user.tenant_id,
                    "scope_type": "personal",
                    "scope_id": user.user_id,
                    "owner_user_id": user.user_id,
                    "status": "published",
                    "classification": "private",
                    "source": "codex_bridge",
                },
            )

    def ensure_demo_external_agent_catalog(self, catalog_file: Path) -> None:
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        catalog_file.write_text(
            json.dumps(
                {
                    "version": "1.0.0",
                    "agents": [
                        {
                            "agent_id": "compliance-reviewer",
                            "name": "Compliance Reviewer",
                            "description": "外部合规顾问，适合制度审查、验收条件与风险分级。",
                            "category": "compliance",
                            "capabilities": [
                                "policy-review",
                                "risk-assessment",
                                "acceptance-criteria",
                            ],
                            "tags": ["compliance", "review", "risk"],
                            "provider": "demo-external",
                            "metadata": {
                                "specialty": "制度审查、流程合规、风险分级",
                                "response_style": "给出结论、风险等级和后续动作",
                            },
                            "card_path": "/demo-a2a/agents/compliance-reviewer/.well-known/agent-card.json",
                            "message_path": "/demo-a2a/agents/compliance-reviewer/v1/message:send",
                        },
                        {
                            "agent_id": "security-architect",
                            "name": "Security Architect",
                            "description": "外部安全架构顾问，适合身份边界、隔离策略与审计控制建议。",
                            "category": "security",
                            "capabilities": [
                                "threat-modeling",
                                "access-boundary-review",
                                "audit-hardening",
                            ],
                            "tags": ["security", "architecture", "audit"],
                            "provider": "demo-external",
                            "metadata": {
                                "specialty": "身份边界、权限模型、审计加固",
                                "response_style": "输出边界分析、风险点和缓解建议",
                            },
                            "card_path": "/demo-a2a/agents/security-architect/.well-known/agent-card.json",
                            "message_path": "/demo-a2a/agents/security-architect/v1/message:send",
                        },
                        {
                            "agent_id": "analytics-copilot",
                            "name": "Analytics Copilot",
                            "description": "外部数据分析顾问，适合指标解释、日志分析与运营洞察。",
                            "category": "analytics",
                            "capabilities": [
                                "metric-analysis",
                                "log-triage",
                                "ops-insight",
                            ],
                            "tags": ["analytics", "operations", "diagnosis"],
                            "provider": "demo-external",
                            "metadata": {
                                "specialty": "指标解读、日志排查、运营分析",
                                "response_style": "先总结，再给观察点与验证建议",
                            },
                            "card_path": "/demo-a2a/agents/analytics-copilot/.well-known/agent-card.json",
                            "message_path": "/demo-a2a/agents/analytics-copilot/v1/message:send",
                        },
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def record_external_agent_discovery(
        self,
        ctx: RequestContext,
        *,
        agent_count: int,
        from_cache: bool,
        source_results: list[dict],
    ) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="external_agent_discovery",
            payload={
                "project_id": ctx.project_id,
                "agent_count": agent_count,
                "from_cache": from_cache,
                "source_results": source_results,
            },
        )

    def record_external_agent_selected(
        self,
        ctx: RequestContext,
        *,
        selected_agent_id: str,
        selection: dict,
    ) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="external_agent_selected",
            payload={
                "project_id": ctx.project_id,
                "selected_agent_id": selected_agent_id,
                "selection": selection,
            },
        )

    def record_a2a_request_sent(
        self,
        ctx: RequestContext,
        *,
        agent_id: str,
        payload: dict,
    ) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="a2a_request_sent",
            payload={"project_id": ctx.project_id, "agent_id": agent_id, **payload},
        )

    def record_a2a_response_received(
        self,
        ctx: RequestContext,
        *,
        agent_id: str,
        payload: dict,
    ) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="a2a_response_received",
            payload={"project_id": ctx.project_id, "agent_id": agent_id, **payload},
        )

    def record_a2a_error(
        self,
        ctx: RequestContext,
        *,
        agent_id: str,
        payload: dict,
    ) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="a2a_error",
            payload={"project_id": ctx.project_id, "agent_id": agent_id, **payload},
        )


def chunk_text(text: str, target_size: int = 500) -> list[str]:
    parts = [part.strip() for part in text.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for part in parts:
        if size + len(part) > target_size and current:
            chunks.append("\n\n".join(current))
            current = [part]
            size = len(part)
        else:
            current.append(part)
            size += len(part)
    if current:
        chunks.append("\n\n".join(current))
    return chunks or [text.strip()]


def write_mcp_audit_log(
    *,
    db_file: Path,
    trace_id: str,
    request_id: str,
    session_id: str,
    tenant_id: str,
    user_id: str,
    event_type: str,
    payload: dict,
) -> None:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            """
            INSERT INTO audit_logs (
                audit_id, trace_id, request_id, session_id, tenant_id, user_id,
                event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit_{uuid4().hex[:18]}",
                trace_id,
                request_id,
                session_id,
                tenant_id,
                user_id,
                event_type,
                json.dumps(payload, ensure_ascii=False),
                utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
