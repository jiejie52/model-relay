from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )

    app_name: str = "model-relay"
    log_level: str = "INFO"

    # Relay authentication. V2 can additionally bind the shared bearer token to
    # one tenant with RELAY_ALLOWED_TENANT_ID. Multi-token authorization remains
    # an outer-gateway concern; the repository still verifies tenant + conversation.
    relay_api_token: SecretStr
    relay_allowed_tenant_id: str | None = None

    # Existing AIHubMix/OpenAI-compatible profile. Optional so a Kimi-only V2
    # deployment is not forced to configure unrelated credentials.
    aihubmix_api_key: SecretStr | None = None
    aihubmix_openai_base_url: str = "https://aihubmix.com/v1"

    # Official Moonshot/Kimi profile.
    moonshot_api_key: SecretStr | None = None
    moonshot_api_origin: str = "https://api.moonshot.ai/v1"

    # Optional native Gemini profile. If unset, that profile is not registered.
    gemini_api_key: SecretStr | None = None
    gemini_api_origin: str = "https://generativelanguage.googleapis.com/v1beta"

    # Optional service-registered profile overrides. This never accepts arbitrary
    # client base_url/key values; it only lets operators register deployment profiles.
    provider_profiles_json: str | None = None

    # Existing Supabase Postgres + execution archive remain authoritative.
    supabase_url: str
    supabase_secret_key: SecretStr
    supabase_bucket: str = "dify-assets"
    supabase_signed_url_ttl: int = 604800
    relay_storage_prefix: str = "relay"

    # Railway Storage Bucket (S3-compatible) is only for canonical uploaded files.
    #
    # Operators may use the explicit MATERIAL_S3_* contract from the V2 design,
    # or Railway's current bucket variable references / AWS-compatible names.
    # Explicit MATERIAL_S3_* names always win when more than one form is present.
    material_s3_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MATERIAL_S3_ENDPOINT", "AWS_ENDPOINT_URL", "ENDPOINT"),
    )
    material_s3_bucket: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MATERIAL_S3_BUCKET", "AWS_S3_BUCKET_NAME", "BUCKET"),
    )
    material_s3_region: str = Field(
        default="auto",
        validation_alias=AliasChoices("MATERIAL_S3_REGION", "AWS_DEFAULT_REGION", "REGION"),
    )
    material_s3_addressing_style: str = Field(
        default="auto",
        validation_alias=AliasChoices("MATERIAL_S3_ADDRESSING_STYLE", "AWS_S3_URL_STYLE"),
    )
    material_s3_access_key_id: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("MATERIAL_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", "ACCESS_KEY_ID"),
    )
    material_s3_secret_access_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("MATERIAL_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "SECRET_ACCESS_KEY"),
    )
    material_s3_presign_seconds: int = 3600
    material_s3_prefix: str = "relay-materials"

    material_max_bytes: int = 100 * 1024 * 1024
    material_fetch_timeout_seconds: float = 120.0
    material_fetch_connect_timeout_seconds: float = 20.0
    material_ready_ttl_seconds: int = 30 * 24 * 3600
    material_gc_grace_seconds: int = 24 * 3600
    material_upload_concurrency: int = 4
    material_inline_max_bytes: int = 10 * 1024 * 1024
    material_url_allowed_ports: str = "443"
    material_url_max_redirects: int = 3

    provider_fetch_url_ttl: int = 3600
    binding_safety_window: int = 300
    binding_prepare_wait_seconds: float = 60.0

    worker_max_runtime_seconds: int = 2400
    worker_poll_seconds: float = 2.0
    job_lease_seconds: int = 120
    job_heartbeat_seconds: int = 30
    job_ttl_seconds: int = 604800
    session_ttl_seconds: int = 604800
    session_expiry_safety_seconds: int = 900

    # V1 success-result limits are preserved for Dify compatibility. Raw errors
    # intentionally do not use these limits.
    relay_result_soft_limit_bytes: int = 524288
    relay_result_hard_limit_bytes: int = 786432
    relay_result_preview_bytes: int = 307200

    upstream_connect_timeout_seconds: float = 30.0
    upstream_write_timeout_seconds: float = 120.0
    upstream_pool_timeout_seconds: float = 30.0
    upstream_read_timeout_seconds: float | None = None

    supabase_timeout_seconds: float = 60.0

    raw_error_delivery: str = "auto"  # auto | inline | reference
    raw_error_inline_max_bytes: int = 262144
    error_retention_seconds: int = 30 * 24 * 3600

    execution_engine: str = "v2"
    compatibility_enabled: bool = True

    @property
    def supabase_root(self) -> str:
        return self.supabase_url.rstrip("/")

    @property
    def aihubmix_root(self) -> str:
        return self.aihubmix_openai_base_url.rstrip("/")

    @property
    def moonshot_root(self) -> str:
        return self.moonshot_api_origin.rstrip("/")

    @property
    def gemini_root(self) -> str:
        return self.gemini_api_origin.rstrip("/")

    @property
    def material_store_configured(self) -> bool:
        return bool(
            self.material_s3_endpoint
            and self.material_s3_bucket
            and self.material_s3_access_key_id
            and self.material_s3_secret_access_key
        )


    @property
    def material_store_missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.material_s3_endpoint:
            missing.append("endpoint")
        if not self.material_s3_bucket:
            missing.append("bucket")
        if not self.material_s3_access_key_id:
            missing.append("access_key_id")
        if not self.material_s3_secret_access_key:
            missing.append("secret_access_key")
        return missing

    @property
    def allowed_material_url_ports(self) -> set[int]:
        result: set[int] = set()
        for part in self.material_url_allowed_ports.split(","):
            part = part.strip()
            if part:
                result.add(int(part))
        return result or {443}

    def provider_profile_overrides(self) -> dict[str, Any]:
        raw = (self.provider_profiles_json or "").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("PROVIDER_PROFILES_JSON must be a JSON object")
        return parsed


@lru_cache
def get_settings() -> Settings:
    return Settings()
