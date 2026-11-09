"""Connection interpretation shared by governance and assembly. No network I/O."""
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
from app.immutable import freeze_json


def resolve_credential(spec, settings):
    ref = spec.credential_ref or spec.credential_env
    if ref:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref):
            raise ValueError("Credential references must be controlled environment names")
        value = getattr(settings, ref.lower(), None)
        if hasattr(value, 'get_secret_value'): return value.get_secret_value()
        secret=os.getenv(ref)
        if secret: return secret
        from pathlib import Path
        filename=getattr(settings,'credential_env_file',None)
        if filename and Path(filename).is_file():
            from dotenv import dotenv_values
            return dotenv_values(filename).get(ref)
        return None
    from app.compatibility.connection_config import legacy_credential
    return legacy_credential(spec, settings)


def scope_identity(spec, settings):
    if not spec.credential_ref:
        from app.compatibility.connection_config import legacy_scope
        return legacy_scope(spec, settings)
    return hashlib.sha256(json.dumps([spec.provider, spec.channel_id, spec.account_id, spec.project_id,
        spec.region, (spec.rest_base_url or spec.base_url or "").rstrip("/")], separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ConnectionRuntime:
    spec: object
    settings: object = field(repr=False, compare=False)
    secret: str = field(repr=False, compare=False)
    profile: dict = field(default_factory=dict)
    pool: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        import httpx
        timeouts = self.spec.metadata.get("timeouts") or {}
        allowed = {"upstream_connect_timeout_seconds", "upstream_write_timeout_seconds", "upstream_pool_timeout_seconds",
                   "cache_prepare_timeout_seconds", "gemini_file_processing_timeout_seconds", "kimi_file_processing_timeout_seconds"}
        if set(timeouts)-allowed or any(type(v) not in (int, float) or not __import__("math").isfinite(v) or v <= 0 for v in timeouts.values()):
            raise ValueError("Invalid connection timeout policy")
        if timeouts: object.__setattr__(self, "settings", self.settings.model_copy(update=dict(timeouts)))
        if self.pool is None: object.__setattr__(self, "pool", httpx.AsyncHTTPTransport(retries=0))
        object.__setattr__(self, "profile", freeze_json(self.profile))
        for url in (self.rest_base, self.sdk_base, self.spec.upload_base_url):
            if url:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("Connection endpoints require credential-free HTTPS URLs")
        if self.auth_type not in {"bearer_header", "x_goog_api_key_header", "x_api_key_header"}:
            raise ValueError("Unsupported connection auth_type")

    @property
    def identity_hash(self):
        from app.core.idempotency import stable_hash
        return stable_hash({"connection": self.spec.identity_hash, "protocol_profile": self.profile})
    @property
    def account_scope_hash(self): return scope_identity(self.spec, self.settings)
    @property
    def material_scope_hash(self):
        if self.spec.credential_ref: return self.account_scope_hash
        from app.compatibility.connection_config import legacy_material_scope
        return legacy_material_scope(self.spec, self.settings)
    @property
    def connection_id(self): return self.spec.connection_id
    @property
    def rest_base(self): return (self.spec.rest_base_url or self.spec.base_url or "").rstrip("/")
    @property
    def api_version(self): return self.spec.api_version or self.spec.sdk_endpoint_options.get("api_version") or "v1beta"
    @property
    def sdk_base(self):
        explicit = self.spec.sdk_endpoint_options.get("base_url")
        if explicit: return explicit.rstrip("/")
        if not self.spec.credential_ref:
            from app.compatibility.connection_config import legacy_sdk_base
            return legacy_sdk_base(self.spec, self.settings)
        root = self.rest_base
        return root[:-(len(self.api_version)+1)] if root.endswith("/"+self.api_version) else root
    @property
    def auth_type(self): return self.spec.auth_type or ("x_goog_api_key_header" if self.spec.protocol == "gemini_native" else "bearer_header")
    def api_url(self, path):
        root = self.rest_base
        if self.spec.protocol == "gemini_native" and not root.endswith("/"+self.api_version): root += "/"+self.api_version
        return root + "/" + path.lstrip("/")
    def headers(self):
        name = {"bearer_header": "Authorization", "x_goog_api_key_header": "x-goog-api-key", "x_api_key_header": "x-api-key"}[self.auth_type]
        return {name: ("Bearer "+self.secret if name == "Authorization" else self.secret), "Content-Type": "application/json"}
    def allowed_hosts(self):
        explicit = self.profile.get("metadata", {}).get("allowed_hosts")
        return tuple(explicit or {urlsplit(x).hostname for x in (self.rest_base, self.sdk_base, self.spec.upload_base_url) if x})
