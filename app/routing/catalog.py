from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import hashlib
import json
from typing import Any

from ..config import Settings
from ..control_plane import CapabilityContract, ModelControlPlane


@dataclass(frozen=True)
class RouteEntry:
    provider: str
    model_pattern: str
    connection_id: str
    priority: int = 100
    deployment_id: str | None = None
    requires_file_adapter: bool = False
    offering_id: str | None = None
    channel_id: str | None = None
    protocol: str | None = None
    capability_contract_id: str | None = None
    capability_contract: dict[str, Any] | None = None
    observed_model_policy: str = "audit"
    quota: dict[str, Any] = field(default_factory=dict)
    source: str = "legacy"

    def matches(self, *, provider: str, model: str, deployment_id: str) -> bool:
        if self.provider.lower() != provider.lower():
            return False
        if self.deployment_id not in (None, "*", deployment_id):
            return False
        return fnmatchcase(model.lower(), self.model_pattern.lower())

    def supports_requirements(self, requirements: dict[str, Any] | None) -> tuple[bool, str | None]:
        if not self.capability_contract:
            return True, None
        contract = CapabilityContract(
            contract_id=str(self.capability_contract.get("contract_id") or self.capability_contract_id or "legacy"),
            revision=str(self.capability_contract.get("revision") or "legacy"),
            supported_options=dict(self.capability_contract.get("supported_options") or {}),
            thinking=dict(self.capability_contract.get("thinking") or {}),
            structured_output=dict(self.capability_contract.get("structured_output") or {}),
            input_modalities=tuple(str(x) for x in (self.capability_contract.get("input_modalities") or ["text"])),
            features=tuple(str(x) for x in (self.capability_contract.get("features") or [])),
            provider_defaults=dict(self.capability_contract.get("provider_defaults") or {}),
            metadata=dict(self.capability_contract.get("metadata") or {}),
        )
        return contract.supports_requirements(requirements)


class RouteCatalog:
    """Published provider/model -> executable offering catalog.

    Existing ROUTE_CATALOG_JSON remains supported as the legacy override. New
    deployments normally use ModelControlPlane, whose offerings carry channel,
    protocol and capability-contract identity while preserving the same public
    provider/model Session contract.
    """

    def __init__(self, *, revision: str, entries: list[RouteEntry], control_plane_hash: str | None = None) -> None:
        self.revision = revision
        self.entries = sorted(
            entries,
            key=lambda item: (-item.priority, item.offering_id or "", item.connection_id),
        )
        self.control_plane_hash = control_plane_hash
        canonical = {
            "revision": self.revision,
            "control_plane_hash": self.control_plane_hash,
            "entries": [
                {
                    "provider": x.provider,
                    "model_pattern": x.model_pattern,
                    "connection_id": x.connection_id,
                    "priority": x.priority,
                    "deployment_id": x.deployment_id,
                    "requires_file_adapter": x.requires_file_adapter,
                    "offering_id": x.offering_id,
                    "channel_id": x.channel_id,
                    "protocol": x.protocol,
                    "capability_contract_id": x.capability_contract_id,
                    "capability_contract": x.capability_contract,
                    "observed_model_policy": x.observed_model_policy,
                    "quota": x.quota,
                    "source": x.source,
                }
                for x in self.entries
            ],
        }
        self.catalog_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        control_plane: ModelControlPlane | None = None,
    ) -> "RouteCatalog":
        # Legacy full-route override retains precedence for compatibility.
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

        cp = control_plane or ModelControlPlane.from_settings(settings)
        entries: list[RouteEntry] = []
        for offering in cp.offerings.values():
            connection = cp.connection(offering.connection_id)
            contract = cp.contract(offering.capability_contract_id)
            if connection is None or contract is None:
                # ControlPlane validation normally makes this unreachable.
                continue
            entries.append(
                RouteEntry(
                    provider=offering.provider,
                    model_pattern=offering.model_pattern,
                    connection_id=offering.connection_id,
                    priority=offering.priority,
                    deployment_id=offering.deployment_id,
                    requires_file_adapter=offering.requires_file_adapter,
                    offering_id=offering.offering_id,
                    channel_id=connection.channel_id,
                    protocol=connection.protocol,
                    capability_contract_id=contract.contract_id,
                    capability_contract=contract.canonical(),
                    observed_model_policy=offering.observed_model_policy,
                    quota=dict(offering.quota),
                    source="control_plane",
                )
            )
        return cls(
            revision=cp.revision,
            entries=entries,
            control_plane_hash=cp.control_plane_hash,
        )

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
            offering_id=(str(value.get("offering_id")) if value.get("offering_id") else None),
            channel_id=(str(value.get("channel_id")) if value.get("channel_id") else None),
            protocol=(str(value.get("protocol")) if value.get("protocol") else None),
            capability_contract_id=(
                str(value.get("capability_contract_id")) if value.get("capability_contract_id") else None
            ),
            capability_contract=(dict(value.get("capability_contract")) if isinstance(value.get("capability_contract"), dict) else None),
            observed_model_policy=str(value.get("observed_model_policy") or "audit").lower(),
            quota=dict(value.get("quota") or {}),
            source="legacy",
        )

    def candidates(self, *, provider: str, model: str, deployment_id: str) -> list[RouteEntry]:
        return [
            entry
            for entry in self.entries
            if entry.matches(provider=provider, model=model, deployment_id=deployment_id)
        ]

    def match(self, *, provider: str, model: str, deployment_id: str) -> RouteEntry | None:
        matches = self.candidates(provider=provider, model=model, deployment_id=deployment_id)
        if not matches:
            return None
        top_priority = matches[0].priority
        top = [entry for entry in matches if entry.priority == top_priority]
        # Preserve the old fail-closed behavior for legacy route catalogs. New
        # ModelOffering entries have stable offering_id ordering at equal weight.
        legacy_top = [entry for entry in top if not entry.offering_id]
        if len(legacy_top) > 1:
            connections = {entry.connection_id for entry in legacy_top}
            if len(connections) > 1:
                raise ValueError(
                    f"Ambiguous route catalog for provider={provider} model={model}: "
                    + ",".join(sorted(connections))
                )
        return top[0]

    def providers(self) -> list[str]:
        return sorted({entry.provider for entry in self.entries})

    def patterns_for_provider(self, provider: str, *, deployment_id: str) -> list[str]:
        provider_n = str(provider or "").lower()
        return sorted({
            entry.model_pattern
            for entry in self.entries
            if entry.provider.lower() == provider_n
            and entry.deployment_id in (None, "*", deployment_id)
        })

    def has_provider(self, provider: str, *, deployment_id: str) -> bool:
        provider_n = str(provider or "").lower()
        return any(
            entry.provider.lower() == provider_n
            and entry.deployment_id in (None, "*", deployment_id)
            for entry in self.entries
        )
