"""Legacy environment conversion only. New connections use credential_ref."""
import hashlib

def legacy_credential(spec, settings):
    if spec.channel_id == "aihubmix": value = settings.aihubmix_api_key
    elif spec.connection_id == settings.moonshot_connection_id: value = settings.moonshot_api_key
    else: return None
    return value.get_secret_value() if value else None

def legacy_scope(spec, settings):
    from app.control_plane.connections import resolve_credential
    return hashlib.sha256("|".join([spec.connection_id, spec.channel_id, spec.account_id or "", spec.base_url or "",
        resolve_credential(spec, settings) or "unconfigured"]).encode()).hexdigest()

def legacy_sdk_base(spec, settings):
    if spec.connection_id == settings.aihubmix_gemini_connection_id:
        base = settings.aihubmix_gemini_sdk_base_url.rstrip("/")
        return "https://aihubmix.com/gemini" if base.lower() == "https://aihubmix.com" else base
    return (spec.rest_base_url or spec.base_url or "").rstrip("/")

def legacy_runtime(settings, protocol):
    from app.control_plane.definitions import ModelControlPlane
    from app.control_plane.connections import ConnectionRuntime, resolve_credential
    plane = ModelControlPlane._builtin(settings)
    spec = next(x for x in plane.connections.values() if x.protocol == protocol)
    return ConnectionRuntime(spec, settings, resolve_credential(spec, settings) or "")


def legacy_material_scope(spec, settings):
    return settings.connection_account_scope_hash(spec.connection_id)
