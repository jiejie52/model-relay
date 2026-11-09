"""One deployment-time release resolution shared by API, Worker and diagnostics."""
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from app.control_plane.definitions import ModelControlPlane


@dataclass(frozen=True)
class LoadedConfiguration:
    settings: object
    control_plane: object
    diagnostics: dict


def load_runtime_configuration(settings):
    bootstrap_name = settings.model_control_plane_bootstrap_file
    bootstrap = {}
    if bootstrap_name:
        path = Path(bootstrap_name)
        if not path.is_file():
            raise ValueError("Control-plane bootstrap file is missing: " + str(path))
        bootstrap = json.loads(path.read_text())
        if bootstrap.get("schema_version") != "relay-control-plane-bootstrap/1":
            raise ValueError("Unsupported control-plane bootstrap schema")
        if set(bootstrap) - {"schema_version", "release_pointer", "require_published_release", "retain_seed_connections"}:
            raise ValueError("Unknown control-plane bootstrap option")
        for key in ("require_published_release", "retain_seed_connections"):
            if key in bootstrap and type(bootstrap[key]) is not bool:
                raise ValueError("Bootstrap flags must be boolean")
    explicit = bool(settings.model_control_plane_file or settings.model_control_plane_json or settings.model_control_plane_release_pointer)
    pointer = settings.model_control_plane_release_pointer
    if not explicit:
        pointer = bootstrap.get("release_pointer")
    updates = {"retain_seed_connections": bootstrap.get("retain_seed_connections", settings.retain_seed_connections)}
    if pointer:
        if settings.model_control_plane_file or settings.model_control_plane_json:
            raise ValueError("Release pointer conflicts with direct snapshot configuration")
        source = Path(pointer)
        if not source.is_file():
            raise ValueError("No active published control-plane release. Compile, approve and activate a release before starting nodes: " + str(source))
        selected = json.loads(source.read_text())  # Read the atomic pointer exactly once.
        def absolute_reference(item):
            path = Path(item["path"])
            return {**item, "path": str((source.parent / path).resolve()) if not path.is_absolute() else str(path)}
        active = absolute_reference(selected)
        retained = json.loads(settings.retained_control_plane_files) + [absolute_reference(x) for x in selected.get("retained", [])]
        updates.update(model_control_plane_file=active["path"], model_control_plane_file_sha256=selected["sha256"],
            model_control_plane_release_pointer=None, retained_control_plane_files=json.dumps(retained))
    resolved = settings.model_copy(update=updates)
    if bootstrap.get("require_published_release") and (not resolved.model_control_plane_file or resolved.model_control_plane_json):
        raise ValueError("This deployment requires an approved pinned release file; builtin/inline configuration is not active")
    plane = ModelControlPlane.from_settings(resolved)
    if bootstrap.get("require_published_release") and not all(plane.release_metadata.get(k) for k in (
        "candidate_sha256", "approved_by", "approval_ref")):
        raise ValueError("Deployment release has no external approval metadata")
    path = resolved.model_control_plane_file
    diagnostics = {
        "source": "pinned_release" if path else "inline" if resolved.model_control_plane_json else "builtin_seed",
        "release_file": str(Path(path).resolve()) if path else None,
        "release_file_sha256": resolved.model_control_plane_file_sha256 if path else None,
        "snapshot_id": str(plane.release_metadata.get("release_id") or plane.control_plane_hash),
        "control_plane_hash": plane.control_plane_hash,
        "route_revision": plane.revision,
        "route_policies": plane.release_metadata.get("route_policies", []),
    }
    return LoadedConfiguration(resolved, plane, diagnostics)


def describe_configuration(loaded):
    plane, settings = loaded.control_plane, loaded.settings
    rows = []
    for supply in plane.offerings.values():
        spec = plane.connection(supply.connection_id)
        configured, reason = plane.connection_configuration(supply.connection_id, settings)
        endpoint = urlsplit(spec.rest_base_url or spec.base_url or "")
        # Legacy endpoint settings are not certified managed URLs. Never log
        # userinfo/query credentials that an operator may have put there.
        host = endpoint.hostname or ""
        authority = host + (":" + str(endpoint.port) if endpoint.port else "")
        safe_endpoint = urlunsplit((endpoint.scheme, authority, endpoint.path, "", ""))
        rows.append({"provider": supply.provider, "model": supply.model_pattern,
            "offering_id": supply.offering_id, "channel_id": spec.channel_id,
            "connection_id": spec.connection_id, "enabled": supply.enabled,
            "endpoint": safe_endpoint, "configured": configured,
            "configuration_reason": reason, "priority": supply.priority,
            "unavailable_action": supply.metadata.get("unavailable_action", "try_next")})
    return {**loaded.diagnostics, "supplies": rows}
