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

MATERIAL_ENV = [
    "MATERIAL_S3_ENDPOINT",
    "MATERIAL_S3_BUCKET",
    "MATERIAL_S3_ACCESS_KEY_ID",
    "MATERIAL_S3_SECRET_ACCESS_KEY",
]

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

missing_material = [name for name in MATERIAL_ENV if not os.getenv(name)]
if missing_material:
    print("[WARN] Material storage is not fully configured; missing variable names: " + ", ".join(missing_material))
else:
    print("[OK] Material storage variable names are present")

for name in ("RELAY_API_TOKEN", "SUPABASE_URL", "SUPABASE_SECRET_KEY"):
    print(f"[{'OK' if os.getenv(name) else 'WARN'}] {name} {'present' if os.getenv(name) else 'missing'}")

sys.exit(1 if failed else 0)
