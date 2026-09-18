from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_uploaded_file_module_import_survives_missing_boto3() -> None:
    root = Path(__file__).resolve().parents[1]
    code = r'''
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "boto3" or name.startswith("boto3."):
        raise ModuleNotFoundError("simulated missing boto3")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import app.storage.uploaded_files as u
assert u._BOTO_IMPORT_ERROR is not None
print("ok")
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
