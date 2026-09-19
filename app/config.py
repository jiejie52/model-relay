from functools import lru_cache

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

    relay_api_token: SecretStr

    # Connection configuration is optional per deployment.  Railway can enable
    # the existing AIHubMix connection while SAE can enable official Moonshot.
    aihubmix_api_key: SecretStr | None = None
    aihubmix_openai_base_url: str = "https://aihubmix.com/v1"

    moonshot_api_key: SecretStr | None = None
    moonshot_base_url: str = "https://api.moonshot.cn/v1"

    enabled_connections: str = "aihubmix_default"
    default_connection_id: str = "aihubmix_default"

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
    # the provider connection.  A timeout never silently creates an async Job.
    sync_request_deadline_seconds: int = 120

    relay_result_soft_limit_bytes: int = 524288
    relay_result_hard_limit_bytes: int = 786432
    relay_result_preview_bytes: int = 307200

    material_ingress_max_bytes: int = 104857600
    material_ingress_timeout_seconds: float = 120.0
    material_allow_http: bool = False

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
    def enabled_connection_set(self) -> set[str]:
        return {x.strip() for x in self.enabled_connections.split(",") if x.strip()}

    @property
    def worker_pool_set(self) -> set[str]:
        pools = {x.strip() for x in self.worker_execution_pools.split(",") if x.strip()}
        return pools or {self.execution_pool}


@lru_cache
def get_settings() -> Settings:
    return Settings()
