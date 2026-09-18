"""Deployment preflight for Model Relay V2.

Prints dependency and configuration presence only. Secret values are never printed.
Exit code 1 means the build/runtime is incomplete.
"""
from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError, version as package_version
import os
import sys

REQUIRED_MODULES = [
    "fastapi",
    "uvicorn",
    "httpx",
    "pydantic",
    "pydantic_settings",
    "jsonschema",
    "multipart",
    "boto3",
    "botocore",
]

MATERIAL_ENV_GROUPS = {
    "endpoint": ("MATERIAL_S3_ENDPOINT", "AWS_ENDPOINT_URL", "ENDPOINT"),
    "bucket": ("MATERIAL_S3_BUCKET", "AWS_S3_BUCKET_NAME", "BUCKET"),
    "access_key_id": ("MATERIAL_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", "ACCESS_KEY_ID"),
    "secret_access_key": ("MATERIAL_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "SECRET_ACCESS_KEY"),
}

failed = False
print("Model Relay V2 runtime preflight")
for name in REQUIRED_MODULES:
    try:
        importlib.import_module(name)
        package_name = "python-multipart" if name == "multipart" else name.replace("_", "-")
        try:
            installed_version = package_version(package_name)
        except PackageNotFoundError:
            installed_version = "unknown"
        print(f"[OK] module {name} version={installed_version}")
    except Exception as exc:
        failed = True
        print(f"[FAIL] module {name}: {type(exc).__name__}: {exc}")

missing_material = [
    logical_name
    for logical_name, aliases in MATERIAL_ENV_GROUPS.items()
    if not any(os.getenv(name) for name in aliases)
]
if missing_material:
    print("[WARN] Material storage is not fully configured; missing logical fields: " + ", ".join(missing_material))
    print("[INFO] Accepted names include MATERIAL_S3_* plus Railway/AWS aliases such as ENDPOINT/BUCKET/ACCESS_KEY_ID/SECRET_ACCESS_KEY and AWS_ENDPOINT_URL/AWS_S3_BUCKET_NAME/AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY")
else:
    print("[OK] Material storage variables are present via an accepted naming scheme")

for name in ("RELAY_API_TOKEN", "SUPABASE_URL", "SUPABASE_SECRET_KEY"):
    print(f"[{'OK' if os.getenv(name) else 'WARN'}] {name} {'present' if os.getenv(name) else 'missing'}")

sys.exit(1 if failed else 0)
