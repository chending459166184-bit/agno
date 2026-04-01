from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.context import RequestContext
from app.db import Database
from app.model_gateway.registry import ModelRegistry


SYSTEM_AGENT_DEFAULTS = [
    {
        "agent_key": "enterprise_orchestrator",
        "display_name": "Enterprise Orchestrator",
        "agent_type": "orchestrator",
        "description": "主智能体，负责协调内部智能体与外部 broker。",
        "is_system": True,
        "is_editable": False,
        "default_enabled": True,
        "default_priority": 100,
        "default_allow_auto_route": True,
        "default_model_alias_task": "orchestrate",
        "skills_group": ["shared", "orchestrator"],
        "tool_summary": ["Agno Team coordination", "Shared skills"],
    },
    {
        "agent_key": "knowledge_agent",
        "display_name": "Knowledge Agent",
        "agent_type": "internal",
        "description": "检索当前项目知识与用户个人知识。",
        "is_system": True,
        "is_editable": True,
        "default_enabled": True,
        "default_priority": 80,
        "default_allow_auto_route": True,
        "default_model_alias_task": "knowledge",
        "skills_group": [],
        "tool_summary": ["search_project_knowledge"],
    },
    {
        "agent_key": "workspace_agent",
        "display_name": "Workspace Agent",
        "agent_type": "internal",
        "description": "通过 Workspace MCP 读取或写入当前用户文件空间。",
        "is_system": True,
        "is_editable": True,
        "default_enabled": True,
        "default_priority": 70,
        "default_allow_auto_route": True,
        "default_model_alias_task": "workspace",
        "skills_group": [],
        "tool_summary": ["workspace MCPTools"],
    },
    {
        "agent_key": "test_agent",
        "display_name": "Test Agent",
        "agent_type": "internal",
        "description": "生成测试建议、风险清单和验收点。",
        "is_system": True,
        "is_editable": True,
        "default_enabled": True,
        "default_priority": 60,
        "default_allow_auto_route": True,
        "default_model_alias_task": "testing",
        "skills_group": ["testing"],
        "tool_summary": ["Testing skill playbook"],
    },
    {
        "agent_key": "external_agent_broker",
        "display_name": "External Agent Broker",
        "agent_type": "broker",
        "description": "通过 MCP discovery 查外部 agents，并通过 A2A 做委托。",
        "is_system": True,
        "is_editable": True,
        "default_enabled": True,
        "default_priority": 50,
        "default_allow_auto_route": True,
        "default_model_alias_task": "external_broker",
        "skills_group": ["shared", "external-broker"],
        "tool_summary": ["external discovery", "A2A delegation"],
    },
]

AGENT_TASK_TYPE = {
    "enterprise_orchestrator": "orchestrate",
    "knowledge_agent": "knowledge",
    "workspace_agent": "workspace",
    "test_agent": "testing",
    "external_agent_broker": "external_broker",
}


@dataclass(slots=True)
class EffectiveAgentConfig:
    agent_key: str
    display_name: str
    agent_type: str
    description: str
    enabled: bool
    priority: int
    allow_auto_route: bool
    preferred_model_alias: str | None
    note: str
    config_json: dict[str, Any]
    source: str
    is_system: bool
    is_editable: bool
    skills_group: list[str]
    tool_summary: list[str]
    included_in_team: bool
    task_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_key": self.agent_key,
            "display_name": self.display_name,
            "agent_type": self.agent_type,
            "description": self.description,
            "enabled": self.enabled,
            "priority": self.priority,
            "allow_auto_route": self.allow_auto_route,
            "preferred_model_alias": self.preferred_model_alias,
            "note": self.note,
            "config_json": self.config_json,
            "source": self.source,
            "is_system": self.is_system,
            "is_editable": self.is_editable,
            "skills_group": self.skills_group,
            "tool_summary": self.tool_summary,
            "included_in_team": self.included_in_team,
            "task_type": self.task_type,
        }


class AgentConfigService:
    def __init__(self, database: Database, model_registry: ModelRegistry) -> None:
        self.database = database
        self.model_registry = model_registry

    def ensure_defaults(self) -> None:
        defaults = []
        router_defaults = self.model_registry.default_aliases_by_task()
        for item in SYSTEM_AGENT_DEFAULTS:
            defaults.append(
                {
                    **item,
                    "default_model_alias": router_defaults.get(item["default_model_alias_task"]),
                }
            )
        self.database.ensure_agent_catalog(defaults)

    def list_catalog(self) -> list[dict[str, Any]]:
        return self.database.list_agent_catalog()

    def list_bindings(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        return self.database.list_agent_bindings(tenant_id=tenant_id, user_id=user_id)

    def get_effective_configs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> list[EffectiveAgentConfig]:
        catalog_rows = {row["agent_key"]: row for row in self.database.list_agent_catalog()}
        binding_rows = self.database.list_agent_bindings(tenant_id=tenant_id, user_id=user_id)
        user_overrides = {row["agent_key"]: row for row in binding_rows if row["project_id"] is None}
        project_overrides = {
            row["agent_key"]: row for row in binding_rows if row["project_id"] == project_id
        }

        effective: list[EffectiveAgentConfig] = []
        for agent_key, catalog in catalog_rows.items():
            source = "default"
            row = project_overrides.get(agent_key)
            if row is not None:
                source = "project_override"
            else:
                row = user_overrides.get(agent_key)
                if row is not None:
                    source = "user_override"
            enabled = bool(
                catalog["default_enabled"] if row is None or row["enabled"] is None else row["enabled"]
            )
            priority = int(
                catalog["default_priority"] if row is None or row["priority"] is None else row["priority"]
            )
            allow_auto_route = bool(
                catalog["default_allow_auto_route"]
                if row is None or row["allow_auto_route"] is None
                else row["allow_auto_route"]
            )
            preferred_model_alias = (
                catalog["default_model_alias"]
                if row is None or row["preferred_model_alias"] is None
                else row["preferred_model_alias"]
            )
            note = ""
            config_json: dict[str, Any] = {}
            if row is not None:
                note = row.get("note") or ""
                config_json = row.get("config_json") or {}
            task_type = AGENT_TASK_TYPE.get(agent_key)
            included_in_team = enabled and (
                agent_key == "enterprise_orchestrator" or allow_auto_route
            )
            effective.append(
                EffectiveAgentConfig(
                    agent_key=agent_key,
                    display_name=catalog["display_name"],
                    agent_type=catalog["agent_type"],
                    description=catalog["description"],
                    enabled=True if agent_key == "enterprise_orchestrator" else enabled,
                    priority=priority,
                    allow_auto_route=True if agent_key == "enterprise_orchestrator" else allow_auto_route,
                    preferred_model_alias=preferred_model_alias,
                    note=note,
                    config_json=config_json,
                    source=source,
                    is_system=bool(catalog["is_system"]),
                    is_editable=bool(catalog["is_editable"]),
                    skills_group=list(catalog.get("skills_group") or []),
                    tool_summary=list(catalog.get("tool_summary") or []),
                    included_in_team=included_in_team if agent_key != "enterprise_orchestrator" else True,
                    task_type=task_type,
                )
            )
        effective.sort(key=lambda item: (-item.priority, item.display_name.lower()))
        return effective

    def update_binding(
        self,
        ctx: RequestContext,
        *,
        agent_key: str,
        project_id: str | None,
        enabled: bool | None,
        priority: int | None,
        allow_auto_route: bool | None,
        preferred_model_alias: str | None,
        note: str | None,
        config_json: dict[str, Any] | None,
    ) -> dict[str, Any]:
        catalog = self.database.get_agent_catalog(agent_key)
        if catalog is None:
            raise ValueError(f"未知 agent_key: {agent_key}")
        if not bool(catalog["is_editable"]):
            raise ValueError(f"{agent_key} 是只读系统智能体，当前不支持修改")
        if preferred_model_alias and preferred_model_alias not in self.model_registry.alias_names():
            raise ValueError(f"未知模型 alias: {preferred_model_alias}")
        return self.database.upsert_agent_binding(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            project_id=project_id,
            agent_key=agent_key,
            enabled=enabled,
            priority=priority,
            allow_auto_route=allow_auto_route,
            preferred_model_alias=preferred_model_alias or None,
            note=note or "",
            config_json=config_json or {},
        )

    def delete_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent_key: str,
        project_id: str | None,
    ) -> None:
        catalog = self.database.get_agent_catalog(agent_key)
        if catalog is None:
            raise ValueError(f"未知 agent_key: {agent_key}")
        if not bool(catalog["is_editable"]):
            raise ValueError(f"{agent_key} 是只读系统智能体，不允许删除 override")
        self.database.delete_agent_binding(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=agent_key,
            project_id=project_id,
        )
