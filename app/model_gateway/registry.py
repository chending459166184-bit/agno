from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.model_gateway.task_types import TASK_GENERAL


@dataclass(slots=True)
class AliasDefinition:
    name: str
    description: str = ""
    provider_kind: str = "unknown"
    supports_tools: bool = True
    litellm_params: dict[str, Any] = field(default_factory=dict)
    required_env: list[str] = field(default_factory=list)
    available_env: dict[str, str | None] = field(default_factory=dict)

    def configured(self) -> bool:
        if not self.required_env:
            return True
        return all(bool(self._get_env_value(env_name)) for env_name in self.required_env)

    def missing_env(self) -> list[str]:
        return [env_name for env_name in self.required_env if not self._get_env_value(env_name)]

    def _get_env_value(self, env_name: str) -> str | None:
        if env_name in self.available_env:
            return self.available_env[env_name]
        return os.getenv(env_name)


@dataclass(slots=True)
class TaskRouteDefinition:
    task_type: str
    aliases: list[str]
    requires_tools: bool = False


class ModelRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._proxy_config = self._load_yaml(settings.resolved_litellm_proxy_config)
        self._router_config = self._load_yaml(settings.resolved_model_router_config)
        self._aliases = self._build_aliases()
        self._routes = self._build_routes()
        self.default_task_type = str(self._router_config.get("default_task_type") or TASK_GENERAL)

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"未找到配置文件: {path}")
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"配置文件格式无效: {path}")
        return payload

    def _build_aliases(self) -> dict[str, AliasDefinition]:
        alias_meta = self._router_config.get("aliases") or {}
        items = self._proxy_config.get("model_list") or []
        aliases: dict[str, AliasDefinition] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            model_name = item.get("model_name")
            if not model_name:
                continue
            litellm_params = item.get("litellm_params") or {}
            meta = alias_meta.get(model_name) or {}
            required_env = sorted(self._extract_env_refs(litellm_params))
            aliases[str(model_name)] = AliasDefinition(
                name=str(model_name),
                description=str(meta.get("description") or ""),
                provider_kind=str(meta.get("provider_kind") or "unknown"),
                supports_tools=bool(meta.get("supports_tools", True)),
                litellm_params=dict(litellm_params),
                required_env=required_env,
                available_env={
                    env_name: self._resolve_config_value(env_name) for env_name in required_env
                },
            )
        return aliases

    def _build_routes(self) -> dict[str, TaskRouteDefinition]:
        task_routes = self._router_config.get("task_routes") or {}
        routes: dict[str, TaskRouteDefinition] = {}
        for task_type, raw in task_routes.items():
            if isinstance(raw, dict):
                aliases = [str(alias) for alias in raw.get("aliases") or [] if str(alias)]
                requires_tools = bool(raw.get("requires_tools", False))
            else:
                aliases = [str(alias) for alias in raw or [] if str(alias)]
                requires_tools = False
            routes[str(task_type)] = TaskRouteDefinition(
                task_type=str(task_type),
                aliases=aliases,
                requires_tools=requires_tools,
            )
        return routes

    def _extract_env_refs(self, value: Any) -> set[str]:
        refs: set[str] = set()
        if isinstance(value, dict):
            for child in value.values():
                refs |= self._extract_env_refs(child)
        elif isinstance(value, list):
            for child in value:
                refs |= self._extract_env_refs(child)
        elif isinstance(value, str) and value.startswith("os.environ/"):
            refs.add(value.split("/", 1)[1])
        return refs

    def _resolve_config_value(self, env_name: str) -> str | None:
        if env_name in os.environ:
            return os.environ[env_name]
        attr_name = env_name.lower()
        if hasattr(self.settings, attr_name):
            raw = getattr(self.settings, attr_name)
            if raw is None:
                return None
            return str(raw)
        return None

    def list_aliases(self) -> list[AliasDefinition]:
        return list(self._aliases.values())

    def alias_names(self) -> list[str]:
        return list(self._aliases.keys())

    def get_alias(self, alias: str) -> AliasDefinition:
        try:
            return self._aliases[alias]
        except KeyError as exc:
            raise KeyError(f"未定义模型 alias: {alias}") from exc

    def list_routes(self) -> list[TaskRouteDefinition]:
        return list(self._routes.values())

    def get_route(self, task_type: str) -> TaskRouteDefinition:
        route = self._routes.get(task_type)
        if route:
            return route
        fallback = self._routes.get(self.default_task_type)
        if fallback:
            return fallback
        raise KeyError(f"未定义 task_type 路由: {task_type}")

    def default_aliases_by_task(self) -> dict[str, str | None]:
        return {
            route.task_type: (route.aliases[0] if route.aliases else None)
            for route in self.list_routes()
        }
