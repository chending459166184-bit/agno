from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import httpx

from app.config import Settings
from app.model_gateway.registry import ModelRegistry


@dataclass(slots=True)
class AliasProbe:
    alias: str
    provider_kind: str
    configured: bool
    missing_env: list[str]
    listed_in_proxy: bool = False
    real_call_ok: bool = False
    latency_ms: int | None = None
    sample_output: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class RuntimeHealthStatus:
    live: bool
    proxy_reachable: bool
    proxy_base_url: str
    reason: str
    aliases: list[AliasProbe] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)

    @property
    def healthy_aliases(self) -> set[str]:
        return {alias.alias for alias in self.aliases if alias.real_call_ok}

    def as_dict(self) -> dict:
        return {
            "live": self.live,
            "live_model_configured": self.live,
            "proxy_reachable": self.proxy_reachable,
            "proxy_base_url": self.proxy_base_url,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "healthy_aliases": sorted(self.healthy_aliases),
            "aliases": [alias.as_dict() for alias in self.aliases],
        }


class LiteLLMHealthChecker:
    def __init__(self, settings: Settings, registry: ModelRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self._cached_status: RuntimeHealthStatus | None = None
        self._cache_expires_at = 0.0

    def probe(self, *, force_refresh: bool = False) -> RuntimeHealthStatus:
        now = time.time()
        if not force_refresh and self._cached_status and now < self._cache_expires_at:
            return self._cached_status

        headers = {"Authorization": f"Bearer {self.settings.litellm_master_key}"}
        proxies_listed: set[str] = set()
        proxy_reachable = False
        alias_probes: list[AliasProbe] = []
        reason = "LiteLLM Proxy 未响应"

        try:
            with httpx.Client(timeout=self.settings.litellm_request_timeout_seconds) as client:
                models_response = client.get(
                    f"{self.settings.litellm_proxy_base_url}/v1/models",
                    headers=headers,
                )
                models_response.raise_for_status()
                proxy_reachable = True
                payload = models_response.json()
                for item in payload.get("data", []):
                    if isinstance(item, dict) and item.get("id"):
                        proxies_listed.add(str(item["id"]))

                for alias_def in self.registry.list_aliases():
                    probe = AliasProbe(
                        alias=alias_def.name,
                        provider_kind=alias_def.provider_kind,
                        configured=alias_def.configured(),
                        missing_env=alias_def.missing_env(),
                        listed_in_proxy=alias_def.name in proxies_listed,
                    )
                    if probe.configured and probe.listed_in_proxy:
                        self._probe_alias(client, headers, alias_def.name, probe)
                    alias_probes.append(probe)
        except Exception as exc:
            reason = f"LiteLLM Proxy 不可达: {exc}"
            alias_probes = [
                AliasProbe(
                    alias=alias_def.name,
                    provider_kind=alias_def.provider_kind,
                    configured=alias_def.configured(),
                    missing_env=alias_def.missing_env(),
                    error=None if alias_def.configured() else "缺少 provider 环境变量",
                )
                for alias_def in self.registry.list_aliases()
            ]
        else:
            live = any(alias.real_call_ok for alias in alias_probes)
            if live:
                reason = "LiteLLM Proxy 可达，且至少一个 alias 完成了真实调用"
            else:
                reason = "LiteLLM Proxy 可达，但当前没有 alias 通过真实调用探测"
            status = RuntimeHealthStatus(
                live=live,
                proxy_reachable=proxy_reachable,
                proxy_base_url=self.settings.litellm_proxy_base_url,
                reason=reason,
                aliases=alias_probes,
            )
            self._cached_status = status
            self._cache_expires_at = now + self.settings.litellm_health_ttl_seconds
            return status

        status = RuntimeHealthStatus(
            live=False,
            proxy_reachable=proxy_reachable,
            proxy_base_url=self.settings.litellm_proxy_base_url,
            reason=reason,
            aliases=alias_probes,
        )
        self._cached_status = status
        self._cache_expires_at = now + self.settings.litellm_health_ttl_seconds
        return status

    def _probe_alias(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        alias_name: str,
        probe: AliasProbe,
    ) -> None:
        prompts = [
            self.settings.litellm_probe_prompt,
            f"Reply exactly with {alias_name}.",
        ]
        last_error: str | None = None
        started = time.perf_counter()
        for prompt in prompts:
            try:
                completion = client.post(
                    f"{self.settings.litellm_proxy_base_url}/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": alias_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 24,
                    },
                )
                completion.raise_for_status()
                body = completion.json()
                probe.real_call_ok = True
                probe.sample_output = self._extract_text(body)[:160]
                probe.error = None
                break
            except Exception as exc:
                last_error = str(exc)
        probe.latency_ms = int((time.perf_counter() - started) * 1000)
        if not probe.real_call_ok:
            probe.error = last_error

    def _extract_text(self, payload: dict) -> str:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
            return "".join(texts)
        return ""
