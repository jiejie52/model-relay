"""Deprecated import shim.

Fusion is an application runtime, not part of Relay Core. New code should import
`app.application.fusion_runtime`.
"""
from .application.fusion_runtime import *  # noqa: F401,F403
from .application.fusion_runtime import _normalize_stage_output  # noqa: F401
