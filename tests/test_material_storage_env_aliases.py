from __future__ import annotations

import os
from contextlib import contextmanager

from app.config import Settings


@contextmanager
def isolated_env(values: dict[str, str]):
    old = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def _required_env() -> dict[str, str]:
    return {
        "RELAY_API_TOKEN": "relay-token",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": "sb-secret",
    }


def test_explicit_material_s3_names_are_supported() -> None:
    env = _required_env() | {
        "MATERIAL_S3_ENDPOINT": "https://explicit.example",
        "MATERIAL_S3_BUCKET": "explicit-bucket",
        "MATERIAL_S3_ACCESS_KEY_ID": "explicit-access",
        "MATERIAL_S3_SECRET_ACCESS_KEY": "explicit-secret",
        "MATERIAL_S3_ADDRESSING_STYLE": "path",
    }
    with isolated_env(env):
        settings = Settings()
    assert settings.material_store_configured
    assert settings.material_s3_endpoint == "https://explicit.example"
    assert settings.material_s3_bucket == "explicit-bucket"
    assert settings.material_s3_addressing_style == "path"


def test_railway_reference_names_are_supported() -> None:
    env = _required_env() | {
        "ENDPOINT": "https://t3.storageapi.dev",
        "BUCKET": "relay-files-abc123",
        "ACCESS_KEY_ID": "railway-access",
        "SECRET_ACCESS_KEY": "railway-secret",
        "REGION": "auto",
    }
    with isolated_env(env):
        settings = Settings()
    assert settings.material_store_configured
    assert settings.material_s3_endpoint == "https://t3.storageapi.dev"
    assert settings.material_s3_bucket == "relay-files-abc123"
    assert settings.material_s3_region == "auto"
    assert settings.material_s3_addressing_style == "auto"


def test_aws_style_railway_names_are_supported() -> None:
    env = _required_env() | {
        "AWS_ENDPOINT_URL": "https://t3.storageapi.dev",
        "AWS_S3_BUCKET_NAME": "relay-files-abc123",
        "AWS_ACCESS_KEY_ID": "aws-access",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "AWS_DEFAULT_REGION": "auto",
        "AWS_S3_URL_STYLE": "virtual",
    }
    with isolated_env(env):
        settings = Settings()
    assert settings.material_store_configured
    assert settings.material_s3_endpoint == "https://t3.storageapi.dev"
    assert settings.material_s3_bucket == "relay-files-abc123"
    assert settings.material_s3_addressing_style == "virtual"


def test_explicit_material_names_win_over_aliases() -> None:
    env = _required_env() | {
        "MATERIAL_S3_ENDPOINT": "https://explicit.example",
        "MATERIAL_S3_BUCKET": "explicit-bucket",
        "MATERIAL_S3_ACCESS_KEY_ID": "explicit-access",
        "MATERIAL_S3_SECRET_ACCESS_KEY": "explicit-secret",
        "AWS_ENDPOINT_URL": "https://aws.example",
        "AWS_S3_BUCKET_NAME": "aws-bucket",
        "AWS_ACCESS_KEY_ID": "aws-access",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
    }
    with isolated_env(env):
        settings = Settings()
    assert settings.material_s3_endpoint == "https://explicit.example"
    assert settings.material_s3_bucket == "explicit-bucket"
    assert settings.material_s3_access_key_id.get_secret_value() == "explicit-access"


def test_missing_logical_fields_are_reported_without_secret_values() -> None:
    env = _required_env() | {
        "ENDPOINT": "https://t3.storageapi.dev",
        "BUCKET": "relay-files-abc123",
    }
    with isolated_env(env):
        settings = Settings()
    assert not settings.material_store_configured
    assert settings.material_store_missing_fields == ["access_key_id", "secret_access_key"]
