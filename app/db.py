from __future__ import annotations

import json
import sqlite3
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    JSON,
    Column,
    DateTime,
    Integer,
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


def sqlite_timestamp() -> str:
    return utcnow().astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


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
        self.agent_catalog = Table(
            "agent_catalog",
            self.metadata,
            Column("agent_key", String, primary_key=True),
            Column("display_name", String, nullable=False),
            Column("agent_type", String, nullable=False),
            Column("description", Text, nullable=False),
            Column("is_system", Boolean, nullable=False),
            Column("is_editable", Boolean, nullable=False),
            Column("default_enabled", Boolean, nullable=False),
            Column("default_priority", Integer, nullable=False),
            Column("default_allow_auto_route", Boolean, nullable=False),
            Column("default_model_alias", String, nullable=True),
            Column("skills_group", JSON, nullable=False),
            Column("tool_summary", JSON, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self.user_agent_bindings = Table(
            "user_agent_bindings",
            self.metadata,
            Column("binding_id", String, primary_key=True),
            Column("tenant_id", String, nullable=False),
            Column("user_id", String, nullable=False),
            Column("project_id", String, nullable=True),
            Column("agent_key", String, nullable=False),
            Column("enabled", Boolean, nullable=True),
            Column("priority", Integer, nullable=True),
            Column("allow_auto_route", Boolean, nullable=True),
            Column("preferred_model_alias", String, nullable=True),
            Column("note", Text, nullable=False),
            Column("config_json", JSON, nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self.execution_jobs = Table(
            "execution_jobs",
            self.metadata,
            Column("job_id", String, primary_key=True),
            Column("trace_id", String, nullable=False),
            Column("request_id", String, nullable=False),
            Column("session_id", String, nullable=True),
            Column("tenant_id", String, nullable=False),
            Column("user_id", String, nullable=False),
            Column("project_id", String, nullable=False),
            Column("status", String, nullable=False),
            Column("language", String, nullable=False),
            Column("command", Text, nullable=False),
            Column("entrypoint", String, nullable=True),
            Column("sandbox_mode", String, nullable=False),
            Column("sandbox_id", String, nullable=True),
            Column("workspace_root", String, nullable=False),
            Column("job_root", String, nullable=False),
            Column("started_at", DateTime(timezone=True), nullable=True),
            Column("finished_at", DateTime(timezone=True), nullable=True),
            Column("duration_ms", Integer, nullable=True),
            Column("exit_code", Integer, nullable=True),
            Column("stdout_path", String, nullable=True),
            Column("stderr_path", String, nullable=True),
            Column("artifact_count", Integer, nullable=False),
            Column("network_enabled", Boolean, nullable=False),
            Column("writeback_enabled", Boolean, nullable=False),
            Column("resource_json", JSON, nullable=False),
        )
        self.execution_artifacts = Table(
            "execution_artifacts",
            self.metadata,
            Column("artifact_id", String, primary_key=True),
            Column("job_id", String, nullable=False),
            Column("relative_path", String, nullable=False),
            Column("size_bytes", Integer, nullable=False),
            Column("mime_type", String, nullable=False),
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
                self.execution_artifacts,
                self.execution_jobs,
                self.session_contexts,
                self.user_agent_bindings,
                self.agent_catalog,
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

    def ensure_agent_catalog(self, entries: list[dict]) -> None:
        now = utcnow()
        with self.engine.begin() as conn:
            for entry in entries:
                agent_key = entry["agent_key"]
                payload = {
                    "agent_key": agent_key,
                    "display_name": entry["display_name"],
                    "agent_type": entry["agent_type"],
                    "description": entry["description"],
                    "is_system": bool(entry["is_system"]),
                    "is_editable": bool(entry["is_editable"]),
                    "default_enabled": bool(entry["default_enabled"]),
                    "default_priority": int(entry["default_priority"]),
                    "default_allow_auto_route": bool(entry["default_allow_auto_route"]),
                    "default_model_alias": entry.get("default_model_alias"),
                    "skills_group": list(entry.get("skills_group") or []),
                    "tool_summary": list(entry.get("tool_summary") or []),
                    "updated_at": now,
                }
                existing = conn.execute(
                    select(self.agent_catalog.c.agent_key).where(self.agent_catalog.c.agent_key == agent_key)
                ).first()
                if existing:
                    conn.execute(
                        self.agent_catalog.update()
                        .where(self.agent_catalog.c.agent_key == agent_key)
                        .values(**payload)
                    )
                else:
                    conn.execute(insert(self.agent_catalog).values(**payload, created_at=now))

    def list_agent_catalog(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.agent_catalog).order_by(self.agent_catalog.c.agent_key.asc())
            ).mappings().all()
        return [dict(row) for row in rows]

    def get_agent_catalog(self, agent_key: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.agent_catalog).where(self.agent_catalog.c.agent_key == agent_key)
            ).mappings().first()
        return dict(row) if row else None

    def list_agent_bindings(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.user_agent_bindings)
                .where(
                    self.user_agent_bindings.c.tenant_id == tenant_id,
                    self.user_agent_bindings.c.user_id == user_id,
                )
                .order_by(
                    self.user_agent_bindings.c.project_id.asc().nullsfirst(),
                    self.user_agent_bindings.c.agent_key.asc(),
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    def upsert_agent_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        project_id: str | None,
        agent_key: str,
        enabled: bool | None,
        priority: int | None,
        allow_auto_route: bool | None,
        preferred_model_alias: str | None,
        note: str,
        config_json: dict,
    ) -> dict:
        now = utcnow()
        payload = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "project_id": project_id,
            "agent_key": agent_key,
            "enabled": enabled,
            "priority": priority,
            "allow_auto_route": allow_auto_route,
            "preferred_model_alias": preferred_model_alias,
            "note": note,
            "config_json": config_json,
            "updated_at": now,
        }
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.user_agent_bindings).where(
                    self.user_agent_bindings.c.tenant_id == tenant_id,
                    self.user_agent_bindings.c.user_id == user_id,
                    self.user_agent_bindings.c.agent_key == agent_key,
                    self.user_agent_bindings.c.project_id.is_(project_id)
                    if project_id is None
                    else self.user_agent_bindings.c.project_id == project_id,
                )
            ).mappings().all()
            row = rows[0] if rows else None
            if row:
                conn.execute(
                    self.user_agent_bindings.update()
                    .where(self.user_agent_bindings.c.binding_id == row["binding_id"])
                    .values(**payload)
                )
                binding_id = row["binding_id"]
            else:
                binding_id = f"binding_{uuid4().hex[:18]}"
                conn.execute(insert(self.user_agent_bindings).values(binding_id=binding_id, **payload))
        return {"binding_id": binding_id, **payload}

    def delete_agent_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent_key: str,
        project_id: str | None,
    ) -> None:
        predicate = (
            self.user_agent_bindings.c.project_id.is_(None)
            if project_id is None
            else self.user_agent_bindings.c.project_id == project_id
        )
        with self.engine.begin() as conn:
            conn.execute(
                self.user_agent_bindings.delete().where(
                    self.user_agent_bindings.c.tenant_id == tenant_id,
                    self.user_agent_bindings.c.user_id == user_id,
                    self.user_agent_bindings.c.agent_key == agent_key,
                    predicate,
                )
            )

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

    def get_run_by_trace(self, trace_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.runs)
                .where(self.runs.c.trace_id == trace_id)
                .order_by(self.runs.c.created_at.desc())
            ).mappings().first()
        return dict(row) if row else None

    def create_execution_job(
        self,
        *,
        ctx: RequestContext,
        language: str,
        command: str,
        entrypoint: str | None,
        sandbox_mode: str,
        workspace_root: str,
        job_root: str,
        network_enabled: bool,
        writeback_enabled: bool,
        resource_json: dict,
    ) -> str:
        job_id = f"job_{uuid4().hex[:18]}"
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.execution_jobs).values(
                    job_id=job_id,
                    trace_id=ctx.trace_id,
                    request_id=ctx.request_id,
                    session_id=ctx.session_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    project_id=ctx.project_id,
                    status="queued",
                    language=language,
                    command=command,
                    entrypoint=entrypoint,
                    sandbox_mode=sandbox_mode,
                    sandbox_id=None,
                    workspace_root=workspace_root,
                    job_root=job_root,
                    started_at=None,
                    finished_at=None,
                    duration_ms=None,
                    exit_code=None,
                    stdout_path=None,
                    stderr_path=None,
                    artifact_count=0,
                    network_enabled=network_enabled,
                    writeback_enabled=writeback_enabled,
                    resource_json=resource_json,
                )
            )
        return job_id

    def update_execution_job_paths(self, job_id: str, *, job_root: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.execution_jobs.update()
                .where(self.execution_jobs.c.job_id == job_id)
                .values(job_root=job_root)
            )

    def mark_execution_job_running(self, job_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.execution_jobs.update()
                .where(self.execution_jobs.c.job_id == job_id)
                .values(status="running", started_at=utcnow())
            )

    def complete_execution_job(
        self,
        job_id: str,
        *,
        status: str,
        sandbox_mode: str,
        sandbox_id: str | None,
        duration_ms: int | None,
        exit_code: int | None,
        stdout_path: str | None,
        stderr_path: str | None,
        artifact_count: int,
        resource_json: dict,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.execution_jobs.update()
                .where(self.execution_jobs.c.job_id == job_id)
                .values(
                    status=status,
                    sandbox_mode=sandbox_mode,
                    sandbox_id=sandbox_id,
                    duration_ms=duration_ms,
                    exit_code=exit_code,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    artifact_count=artifact_count,
                    resource_json=resource_json,
                    finished_at=utcnow(),
                )
            )

    def get_execution_job(self, job_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.execution_jobs).where(self.execution_jobs.c.job_id == job_id)
            ).mappings().first()
        return dict(row) if row else None

    def add_execution_artifact(
        self,
        *,
        job_id: str,
        relative_path: str,
        size_bytes: int,
        mime_type: str,
    ) -> dict:
        payload = {
            "artifact_id": f"artifact_{uuid4().hex[:18]}",
            "job_id": job_id,
            "relative_path": relative_path,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "created_at": utcnow(),
        }
        with self.engine.begin() as conn:
            conn.execute(insert(self.execution_artifacts).values(**payload))
        return payload

    def list_execution_artifacts(self, job_id: str) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.execution_artifacts)
                .where(self.execution_artifacts.c.job_id == job_id)
                .order_by(self.execution_artifacts.c.relative_path.asc())
            ).mappings().all()
        return [dict(row) for row in rows]

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
        demo_workspace_root = workspace_root / "demo"
        if demo_workspace_root.exists():
            shutil.rmtree(demo_workspace_root)
        catalog_file = external_agent_catalog_file or (
            project_root / "data" / "external_agents" / "catalog.json"
        )
        if catalog_file.exists():
            catalog_file.unlink()
        self.ensure_demo_seed_data(project_root, workspace_root, catalog_file, force_reset_catalog=True)

    def ensure_demo_seed_data(
        self,
        project_root: Path,
        workspace_root: Path,
        external_agent_catalog_file: Path | None = None,
        *,
        force_reset_catalog: bool = False,
    ) -> None:
        demo_users = [
            AuthenticatedUser(
                tenant_id="demo",
                user_id="alice",
                display_name="Alice Chen",
                role="manager",
                project_ids=["alpha", "beta"],
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
            AuthenticatedUser(
                tenant_id="demo",
                user_id="charlie",
                display_name="Charlie Wang",
                role="analyst",
                project_ids=["alpha"],
                default_project_id="alpha",
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
            source_path = str(spec["path"].relative_to(project_root))
            if self.has_content_source(source_path):
                continue
            self.ingest_document(
                tenant_id="demo",
                scope_type=spec["scope_type"],
                scope_id=spec["scope_id"],
                owner_user_id=spec["owner_user_id"],
                project_id=spec["project_id"],
                title=spec["title"],
                source_path=source_path,
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
                "notes/beta-handoff.md": (
                    "# Beta 试点切换说明\n\n"
                    "- Alice 也参与 beta 项目的跨团队沟通。\n"
                    "- 需要验证同一用户切换到 beta 项目时，知识命中会转到 beta 范围。"
                ),
            },
            "bob": {
                "notes/beta-todo.md": (
                    "# Beta 待办\n\n"
                    "- 检查夜间批处理告警。\n"
                    "- 补充接口超时告警阈值。\n"
                    "- Beta 项目不要暴露给 Alpha 用户。"
                ),
                "notes/release-checklist.md": (
                    "# Beta 发布检查\n\n"
                    "- 重点回归 beta 的接口超时与日志告警。\n"
                    "- 不需要 alpha 项目的任何个人文件。"
                ),
            },
            "charlie": {
                "notes/alpha-analysis.md": (
                    "# Alpha 分析视角\n\n"
                    "- Charlie 关注 alpha 项目的指标拆解与知识验证。\n"
                    "- 需要验证同一 alpha 项目下，不同用户能命中相同项目知识但不同个人知识。"
                ),
            },
        }
        for user_id, files in personal_notes.items():
            user_root = ensure_workspace(workspace_root / "demo" / user_id)
            for rel_path, content in files.items():
                absolute_path = user_root / rel_path
                if not absolute_path.exists():
                    save_text_file(user_root, rel_path, content, overwrite=True)
                source_path = f"workspace/{user_id}/{rel_path}"
                if self.has_content_source(source_path):
                    continue
                self.ingest_document(
                    tenant_id="demo",
                    scope_type="personal",
                    scope_id=user_id,
                    owner_user_id=user_id,
                    project_id=None,
                    title=f"{user_id}::{rel_path}",
                    source_path=source_path,
                    body_text=absolute_path.read_text(encoding="utf-8"),
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
        self.ensure_demo_external_agent_catalog(catalog_file, overwrite=force_reset_catalog)

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

    def ensure_demo_external_agent_catalog(self, catalog_file: Path, *, overwrite: bool = False) -> None:
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        if catalog_file.exists() and not overwrite:
            return
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

    def record_member_output(
        self,
        ctx: RequestContext,
        *,
        member_name: str,
        order: int,
        content: str,
        phase: str = "team",
    ) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="member_output_captured",
            payload={
                "project_id": ctx.project_id,
                "member_name": member_name,
                "order": order,
                "phase": phase,
                "content": content,
            },
        )

    def record_prefetch_triggered(self, ctx: RequestContext, *, payload: dict) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="prefetch_triggered",
            payload={"project_id": ctx.project_id, **payload},
        )

    def record_workspace_guard_data_captured(self, ctx: RequestContext, *, payload: dict) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="workspace_guard_data_captured",
            payload={"project_id": ctx.project_id, **payload},
        )

    def record_workspace_guard_compose_started(self, ctx: RequestContext, *, payload: dict) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="workspace_guard_compose_started",
            payload={"project_id": ctx.project_id, **payload},
        )

    def record_workspace_guard_compose_succeeded(self, ctx: RequestContext, *, payload: dict) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="workspace_guard_compose_succeeded",
            payload={"project_id": ctx.project_id, **payload},
        )

    def record_workspace_guard_compose_failed(self, ctx: RequestContext, *, payload: dict) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="workspace_guard_compose_failed",
            payload={"project_id": ctx.project_id, **payload},
        )

    def _record_sandbox_event(
        self,
        ctx: RequestContext,
        *,
        event_type: str,
        job_id: str,
        payload: dict,
    ) -> None:
        self.append_audit(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type=event_type,
            payload={"project_id": ctx.project_id, "job_id": job_id, **payload},
        )

    def record_sandbox_job_created(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(ctx, event_type="sandbox_job_created", job_id=job_id, payload=payload)

    def record_sandbox_stage_prepared(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(ctx, event_type="sandbox_stage_prepared", job_id=job_id, payload=payload)

    def record_sandbox_started(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(ctx, event_type="sandbox_started", job_id=job_id, payload=payload)

    def record_sandbox_completed(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(ctx, event_type="sandbox_completed", job_id=job_id, payload=payload)

    def record_sandbox_failed(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(ctx, event_type="sandbox_failed", job_id=job_id, payload=payload)

    def record_sandbox_timeout(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(ctx, event_type="sandbox_timeout", job_id=job_id, payload=payload)

    def record_sandbox_killed(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(ctx, event_type="sandbox_killed", job_id=job_id, payload=payload)

    def record_sandbox_artifact_recorded(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(
            ctx,
            event_type="sandbox_artifact_recorded",
            job_id=job_id,
            payload=payload,
        )

    def record_sandbox_writeback_applied(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(
            ctx,
            event_type="sandbox_writeback_applied",
            job_id=job_id,
            payload=payload,
        )

    def record_sandbox_writeback_skipped(self, ctx: RequestContext, *, job_id: str, payload: dict) -> None:
        self._record_sandbox_event(
            ctx,
            event_type="sandbox_writeback_skipped",
            job_id=job_id,
            payload=payload,
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
                sqlite_timestamp(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
