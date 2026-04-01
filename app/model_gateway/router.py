from __future__ import annotations

from dataclasses import dataclass

from app.model_gateway.registry import AliasDefinition, ModelRegistry


@dataclass(slots=True)
class ModelRouteSelection:
    task_type: str
    alias: str
    candidates: list[str]
    alias_definition: AliasDefinition
    reason: str


class ModelRouter:
    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry

    def resolve(
        self,
        task_type: str,
        *,
        preferred_aliases: set[str] | None = None,
    ) -> ModelRouteSelection:
        route = self.registry.get_route(task_type)
        candidates = route.aliases or self.registry.get_route(self.registry.default_task_type).aliases
        if not candidates:
            raise ValueError(f"task_type={task_type} 没有可用 alias")

        filtered: list[str] = []
        if route.requires_tools:
            filtered = [
                alias for alias in candidates if self.registry.get_alias(alias).supports_tools
            ]
        primary_pool = filtered or candidates

        if preferred_aliases:
            for alias in primary_pool:
                alias_definition = self.registry.get_alias(alias)
                if alias in preferred_aliases and alias_definition.configured():
                    return ModelRouteSelection(
                        task_type=task_type,
                        alias=alias,
                        candidates=primary_pool,
                        alias_definition=alias_definition,
                        reason="matched_healthy_alias",
                    )
            if route.requires_tools:
                for alias in candidates:
                    alias_definition = self.registry.get_alias(alias)
                    if alias in preferred_aliases and alias_definition.configured():
                        return ModelRouteSelection(
                            task_type=task_type,
                            alias=alias,
                            candidates=candidates,
                            alias_definition=alias_definition,
                            reason="matched_healthy_alias_without_tool_support",
                        )

        for alias in primary_pool:
            alias_definition = self.registry.get_alias(alias)
            if alias_definition.configured():
                return ModelRouteSelection(
                    task_type=task_type,
                    alias=alias,
                    candidates=primary_pool,
                    alias_definition=alias_definition,
                    reason="matched_configured_alias",
                )
        if route.requires_tools:
            for alias in candidates:
                alias_definition = self.registry.get_alias(alias)
                if alias_definition.configured():
                    return ModelRouteSelection(
                        task_type=task_type,
                        alias=alias,
                        candidates=candidates,
                        alias_definition=alias_definition,
                        reason="matched_configured_alias_without_tool_support",
                    )

        alias = primary_pool[0]
        alias_definition = self.registry.get_alias(alias)
        return ModelRouteSelection(
            task_type=task_type,
            alias=alias,
            candidates=primary_pool,
            alias_definition=alias_definition,
            reason="fallback_first_alias",
        )
