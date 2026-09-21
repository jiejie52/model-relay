from functools import lru_cache
import hashlib

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "model-relay"
    log_level: str = "INFO"
    dependency_http_log_level: str = "WARNING"
    uvicorn_access_log: bool = False

    relay_api_token: SecretStr

    # Provider connection configuration. 0.5.1 defaults to "all" connection
    # availability: ENABLED_CONNECTIONS is retained only as a future/optional
    # allowlist and is ignored unless CONNECTION_AVAILABILITY_MODE=allowlist.
    aihubmix_api_key: SecretStr | None = None
    aihubmix_openai_base_url: str = "https://aihubmix.com/v1"
    aihubmix_gemini_base_url: str | None = None
    aihubmix_gemini_connection_id: str = "aihubmix_gemini_native"

    moonshot_api_key: SecretStr | None = None
    moonshot_base_url: str = "https://api.moonshot.cn/v1"
    moonshot_connection_id: str = "moonshot_official"

    connection_availability_mode: str = "all"  # all | allowlist
    enabled_connections: str = "aihubmix_default"  # legacy/future allowlist input
    default_connection_id: str = "aihubmix_default"

    # Public routing contract 2.1: callers provide provider/model only. The
    # deployment-local catalog resolves and freezes the private connection_id.
    route_revision: str = "relay-route-catalog/2026-09-21.2"
    route_catalog_json: str | None = None
    route_legacy_hint_mode: str = "warn"  # warn | strict
    route_gemini_model_pattern: str = "gemini-*"
    route_grok_model_pattern: str = "grok-*"
    route_kimi_model_pattern: str = "kimi-*"

    # Same source image, different deployment configuration.
    deployment_id: str = "railway"
    execution_pool: str = "railway-default"
    worker_execution_pools: str = "railway-default"

    supabase_url: str
    supabase_secret_key: SecretStr
    supabase_bucket: str = "dify-assets"
    supabase_signed_url_ttl: int = 604800
    default_storage_id: str = "supabase_shared"

    relay_storage_prefix: str = "relay"

    worker_max_runtime_seconds: int = 2400
    worker_poll_seconds: float = 2.0
    job_lease_seconds: int = 120
    job_heartbeat_seconds: int = 30
    job_ttl_seconds: int = 604800
    session_ttl_seconds: int = 604800
    session_expiry_safety_seconds: int = 900

    # Sync requests are still persisted Relay Requests, but the API process owns
    # the provider connection. A timeout never silently creates an async Job.
    sync_request_deadline_seconds: int = 120

    relay_result_soft_limit_bytes: int = 524288
    relay_result_hard_limit_bytes: int = 786432
    relay_result_preview_bytes: int = 307200

    material_ingress_max_bytes: int = 104857600
    material_ingress_timeout_seconds: float = 120.0
    material_allow_http: bool = False
    material_default_durability_policy: str = "native_first"
    material_default_fallback_policy: str = "on_provider_unavailable"
    gemini_file_soft_ttl_seconds: int = 172800
    gemini_file_poll_seconds: float = 2.0
    gemini_file_processing_timeout_seconds: float = 300.0
    kimi_file_poll_seconds: float = 2.0
    kimi_file_processing_timeout_seconds: float = 300.0

    upstream_connect_timeout_seconds: float = 30.0
    upstream_write_timeout_seconds: float = 120.0
    upstream_pool_timeout_seconds: float = 30.0

    supabase_timeout_seconds: float = 60.0

    @property
    def supabase_root(self) -> str:
        return self.supabase_url.rstrip("/")

    @property
    def aihubmix_root(self) -> str:
        return self.aihubmix_openai_base_url.rstrip("/")

    @property
    def moonshot_root(self) -> str:
        return self.moonshot_base_url.rstrip("/")

    @property
    def connection_allowlist_set(self) -> set[str]:
        return {x.strip() for x in self.enabled_connections.split(",") if x.strip()}

    @property
    def enabled_connection_set(self) -> set[str]:
        """Compatibility view used by older code/logging.

        In the default ``all`` mode this returns all built-in connection IDs,
        regardless of the legacy ENABLED_CONNECTIONS value. This is deliberate:
        route availability is no longer accidentally controlled by an old env
        variable. A future deployment can opt into the allowlist explicitly.
        """
        if not self.connection_restrictions_enabled:
            return self.known_connection_ids
        return self.connection_allowlist_set

    @property
    def known_connection_ids(self) -> set[str]:
        return {
            "aihubmix_default",
            self.aihubmix_gemini_connection_id,
            self.moonshot_connection_id,
        }

    @property
    def connection_restrictions_enabled(self) -> bool:
        return str(self.connection_availability_mode or "all").strip().lower() == "allowlist"

    def connection_is_enabled(self, connection_id: str) -> bool:
        if not self.connection_restrictions_enabled:
            return True
        return connection_id in self.connection_allowlist_set

    def connection_configuration(self, connection_id: str) -> tuple[bool, str | None]:
        """Return whether the built-in connection has enough server config.

        This does not perform network health checks. Unknown/custom connections
        are considered configuration-neutral and are validated by adapter
        registration instead.
        """
        if connection_id == "aihubmix_default":
            if self.aihubmix_api_key is None:
                return False, "AIHUBMIX_API_KEY is not configured"
            return True, None
        if connection_id == self.aihubmix_gemini_connection_id:
            missing: list[str] = []
            if self.aihubmix_api_key is None:
                missing.append("AIHUBMIX_API_KEY")
            if not self.aihubmix_gemini_base_url:
                missing.append("AIHUBMIX_GEMINI_BASE_URL")
            if missing:
                return False, "missing server configuration: " + ", ".join(missing)
            return True, None
        if connection_id == self.moonshot_connection_id:
            if self.moonshot_api_key is None:
                return False, "MOONSHOT_API_KEY is not configured"
            return True, None
        return True, None

    def connection_is_configured(self, connection_id: str) -> bool:
        configured, _ = self.connection_configuration(connection_id)
        return configured

    def connection_is_active(self, connection_id: str) -> bool:
        return self.connection_is_enabled(connection_id) and self.connection_is_configured(connection_id)

    @property
    def worker_pool_set(self) -> set[str]:
        pools = {x.strip() for x in self.worker_execution_pools.split(",") if x.strip()}
        return pools or {self.execution_pool}

    def connection_account_scope_hash(self, connection_id: str) -> str:
        """Opaque fingerprint used to prevent provider file reuse across keys."""
        if connection_id == self.aihubmix_gemini_connection_id:
            if self.aihubmix_api_key is None or not self.aihubmix_gemini_base_url:
                raise RuntimeError("Gemini native connection is not configured")
            material = f"{connection_id}|{self.aihubmix_gemini_base_url.rstrip('/')}|{self.aihubmix_api_key.get_secret_value()}"
        elif connection_id == self.moonshot_connection_id:
            if self.moonshot_api_key is None:
                raise RuntimeError("Moonshot connection is not configured")
            material = f"{connection_id}|{self.moonshot_root}|{self.moonshot_api_key.get_secret_value()}"
        else:
            # Non-native file connections do not expose a provider-side file
            # resource, but still get a stable scope for snapshot/audit fields.
            material = f"{connection_id}|relay-fallback"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@lru_cache
def get_settings() -> Settings:
    return Settings()
