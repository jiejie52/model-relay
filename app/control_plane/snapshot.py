from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import os
from typing import Any, Mapping
from app.immutable import freeze_json
from app.control_plane.definitions import ModelControlPlane, ConnectionSpec

SUPPORTED_PROTOCOLS = frozenset({"responses", "chat_completions", "claude_messages", "gemini_native", "moonshot_chat"})

@dataclass(frozen=True)
class ImmutableConfigSnapshot:
    snapshot_id: str
    catalog_hash: str
    route_revision: str
    supplies: Mapping[str, Any]
    capabilities: Mapping[str, Any]
    protocols: Mapping[str, Any]
    cache_policies: Mapping[str, Any]
    control_plane_hash: str
    connections: Mapping[str, ConnectionSpec] = field(default_factory=dict)
    route_catalog: Any = None

    def __post_init__(self):
        for name in ("supplies", "capabilities", "protocols", "cache_policies"):
            object.__setattr__(self, name, freeze_json(dict(getattr(self, name))))
        object.__setattr__(self, "connections", freeze_json({k: ConnectionSpec(
            **{**v.canonical(), "metadata": freeze_json(v.metadata)}) for k, v in self.connections.items()}))
        if self.route_catalog is not None and self.catalog_hash != self.route_catalog.catalog_hash:
            raise ValueError("Snapshot route catalog digest mismatch")

    def connection(self, connection_id):
        return self.connections.get(connection_id)

    def credential(self, connection_id, settings):
        spec = self.connection(connection_id)
        if spec is None: return None
        if spec.channel_id == "aihubmix":
            return settings.aihubmix_api_key.get_secret_value() if settings.aihubmix_api_key else None
        if connection_id == settings.moonshot_connection_id:
            return settings.moonshot_api_key.get_secret_value() if settings.moonshot_api_key else None
        return (os.getenv(spec.credential_env) or None) if spec.credential_env else None

    def connection_configuration(self, connection_id, settings):
        spec = self.connection(connection_id)
        if spec is None: return settings.connection_configuration(connection_id)
        if not spec.base_url: return False, "connection base_url is not configured"
        if spec.protocol in SUPPORTED_PROTOCOLS and not self.credential(connection_id, settings):
            return False, "connection credential is not configured"
        return True, None

    def account_scope_hash(self, connection_id, settings):
        spec = self.connection(connection_id)
        if spec is None: return settings.connection_account_scope_hash(connection_id)
        material = "|".join([spec.connection_id, spec.channel_id, spec.account_id or "", spec.base_url or "",
                             self.credential(connection_id, settings) or "unconfigured"])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

def get_active_config_snapshot(settings, *, control_plane=None, catalog=None):
    plane = control_plane or ModelControlPlane.from_settings(settings)
    if plane.status != "published": raise ValueError("Only published snapshots can enter execution")
    return ImmutableConfigSnapshot(
        str(plane.release_metadata.get("release_id") or plane.control_plane_hash),
        catalog.catalog_hash if catalog is not None else plane.control_plane_hash,
        catalog.revision if catalog is not None else settings.route_revision,
        {k: v.canonical() for k, v in plane.offerings.items()},
        {k: v.canonical() for k, v in plane.capability_contracts.items()},
        {k: v.canonical() for k, v in plane.protocol_profiles.items()},
        {k: v.canonical() for k, v in plane.cache_policies.items()}, plane.control_plane_hash,
        connections=plane.connections, route_catalog=catalog)

def verify_node_snapshot_compatibility(snapshot, *, supported_protocols=SUPPORTED_PROTOCOLS):
    enabled_profiles = {s.get("protocol_profile_id") for s in snapshot.supplies.values() if s.get("enabled", True)}
    unknown = {p.get("protocol") for key, p in snapshot.protocols.items()
               if key in enabled_profiles and p.get("protocol") not in supported_protocols}
    if unknown: raise ValueError("Node cannot execute snapshot protocols: " + ", ".join(sorted(map(str, unknown))))
    return True
