from functools import lru_cache

from pydantic import Field, SecretStr
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

    aihubmix_api_key: SecretStr
    aihubmix_openai_base_url: str = "https://aihubmix.com/v1"
    # Relay-owned infrastructure routing. Every model request must resolve to an
    # explicitly configured AIHubMix channel, so AIHubMix cannot silently move a
    # session/request to another physical provider. Exact model mappings win over
    # provider-family mappings. Values are AIHubMix channel IDs from the console.
    aihubmix_official_model_channels: dict[str, int] = Field(default_factory=dict)
    aihubmix_official_provider_channels: dict[str, int] = Field(default_factory=dict)

    supabase_url: str
    supabase_secret_key: SecretStr
    supabase_bucket: str = "dify-assets"
    supabase_signed_url_ttl: int = 604800

    relay_storage_prefix: str = "relay"

    worker_max_runtime_seconds: int = 2400
    worker_poll_seconds: float = 2.0
    job_lease_seconds: int = 120
    job_heartbeat_seconds: int = 30
    job_ttl_seconds: int = 604800
    session_ttl_seconds: int = 604800
    session_expiry_safety_seconds: int = 900

    relay_result_soft_limit_bytes: int = 524288
    relay_result_hard_limit_bytes: int = 786432
    relay_result_preview_bytes: int = 307200

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
