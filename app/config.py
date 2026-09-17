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

    # Existing AIHubMix/OpenAI-compatible provider. Optional so a Kimi-only
    # deployment does not have to configure credentials it never uses.
    aihubmix_api_key: SecretStr | None = None
    aihubmix_openai_base_url: str = "https://aihubmix.com/v1"

    # Official Kimi/Moonshot endpoint profile.
    moonshot_api_key: SecretStr | None = None
    moonshot_base_url: str = "https://api.moonshot.ai/v1"

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

    # Raw provider errors are never normalized or summarized. Small errors can be
    # returned inline; larger ones are delivered by the authenticated raw endpoint.
    relay_raw_error_inline_limit_bytes: int = 262144

    upstream_connect_timeout_seconds: float = 30.0
    upstream_write_timeout_seconds: float = 120.0
    upstream_pool_timeout_seconds: float = 30.0

    supabase_timeout_seconds: float = 60.0

    # Engine names allow rolling upgrade without old workers claiming new jobs.
    core_execution_engine: str = "core-v2"
    legacy_core_execution_engine: str = "core-legacy-v1"
    legacy_fusion_execution_engine: str = "fusion-legacy-v1"

    @property
    def supabase_root(self) -> str:
        return self.supabase_url.rstrip("/")

    @property
    def aihubmix_root(self) -> str:
        return self.aihubmix_openai_base_url.rstrip("/")

    @property
    def moonshot_root(self) -> str:
        return self.moonshot_base_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
