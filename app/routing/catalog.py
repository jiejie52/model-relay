from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import hashlib
import json
from typing import Any

from ..config import Settings


@dataclass(frozen=True)
class RouteEntry:
    provider: str
    model_pattern: str
    connection_id: str
    priority: int = 100
    deployment_id: str | None = None
    requires_file_adapter: bool = False

    def matches(self, *, provider: str, model: str, deployment_id: str) -> bool:
        if self.provider.lower() != provider.lower():
            return False
        if self.deployment_id not in (None, "*", deployment_id):
            return False
        return fnmatchcase(model.lower(), self.model_pattern.lower())


class RouteCatalog:
    """Deployment-local provider/model -> private connection mapping.

    The catalog is a Relay implementation detail. Callers never supply or select
    a connection from this catalog. A deterministic built-in catalog is derived
    from enabled connections; ROUTE_CATALOG_JSON can replace it without changing
    the public API contract.
    """

    def __init__(self, *, revision: str, entries: list[RouteEntry]) -> None:
        self.revision = revision
        self.entries = sorted(entries, key=lambda item: item.priority, reverse=True)
        canonical = {
            "revision": self.revision,
            "entries": [
                {
                    "provider": x.provider,
                    "model_pattern": x.model_pattern,
                    "connection_id": x.connection_id,
                    "priority": x.priority,
                    "deployment_id": x.deployment_id,
                    "requires_file_adapter": x.requires_file_adapter,
                }
                for x in self.entries
            ],
        }
        self.catalog_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_settings(cls, settings: Settings) -> "RouteCatalog":
        raw = (settings.route_catalog_json or "").strip()
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                revision = settings.route_revision
                rows = parsed
            elif isinstance(parsed, dict):
                revision = str(parsed.get("revision") or settings.route_revision)
                rows = parsed.get("routes")
            else:
                raise ValueError("ROUTE_CATALOG_JSON must be a JSON array or object")
            if not isinstance(rows, list):
                raise ValueError("ROUTE_CATALOG_JSON routes must be a JSON array")
            entries = [cls._entry_from_mapping(item, settings.deployment_id) for item in rows]
            return cls(revision=revision, entries=entries)

        entries: list[RouteEntry] = []
        enabled = settings.enabled_connection_set
        deployment = settings.deployment_id
        if settings.aihubmix_gemini_connection_id in enabled:
            entries.append(
                RouteEntry(
                    provider="gemini",
                    model_pattern=settings.route_gemini_model_pattern,
                    connection_id=settings.aihubmix_gemini_connection_id,
                    priority=100,
                    deployment_id=deployment,
                    requires_file_adapter=True,
                )
            )
        if "aihubmix_default" in enabled:
            entries.append(
                RouteEntry(
                    provider="grok",
                    model_pattern=settings.route_grok_model_pattern,
                    connection_id="aihubmix_default",
                    priority=100,
                    deployment_id=deployment,
                    requires_file_adapter=False,
                )
            )
        if settings.moonshot_connection_id in enabled:
            entries.append(
                RouteEntry(
                    provider="kimi",
                    model_pattern=settings.route_kimi_model_pattern,
                    connection_id=settings.moonshot_connection_id,
                    priority=100,
                    deployment_id=deployment,
                    requires_file_adapter=True,
                )
            )
        return cls(revision=settings.route_revision, entries=entries)

    @staticmethod
    def _entry_from_mapping(value: Any, default_deployment: str) -> RouteEntry:
        if not isinstance(value, dict):
            raise ValueError("Each route catalog entry must be an object")
        provider = str(value.get("provider") or "").strip().lower()
        model_pattern = str(value.get("model_pattern") or "").strip()
        connection_id = str(value.get("connection_id") or "").strip()
        if not provider or not model_pattern or not connection_id:
            raise ValueError("Route entries require provider, model_pattern and connection_id")
        return RouteEntry(
            provider=provider,
            model_pattern=model_pattern,
            connection_id=connection_id,
            priority=int(value.get("priority") or 100),
            deployment_id=str(value.get("deployment_id") or default_deployment),
            requires_file_adapter=bool(value.get("requires_file_adapter", False)),
        )

    def match(self, *, provider: str, model: str, deployment_id: str) -> RouteEntry | None:
        matches = [
            entry
            for entry in self.entries
            if entry.matches(provider=provider, model=model, deployment_id=deployment_id)
        ]
        if not matches:
            return None
        top_priority = matches[0].priority
        top = [entry for entry in matches if entry.priority == top_priority]
        if len(top) > 1:
            connections = {entry.connection_id for entry in top}
            if len(connections) > 1:
                raise ValueError(
                    f"Ambiguous route catalog for provider={provider} model={model}: "
                    + ",".join(sorted(connections))
                )
        return top[0]

    def providers(self) -> list[str]:
        return sorted({entry.provider for entry in self.entries})

    def has_provider(self, provider: str, *, deployment_id: str) -> bool:
        provider_n = str(provider or "").lower()
        return any(
            entry.provider.lower() == provider_n
            and entry.deployment_id in (None, "*", deployment_id)
            for entry in self.entries
        )
