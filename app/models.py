"""Deprecated v1 model import shim.

Relay Core v2 models live in `app.core_models`. These names are retained only for
legacy `/v1/jobs` and Dify compatibility callers.
"""
from .compat.models_v1 import *  # noqa: F401,F403
