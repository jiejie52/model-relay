"""Legacy import compatibility for the Fusion application runtime.

Business logic lives in app.applications.fusion_runtime so V2 Core modules do not
need to import or recognize Fusion stages.
"""
from .applications.fusion_runtime import *  # noqa: F401,F403
from .applications import fusion_runtime as _impl

# Private helpers are re-exported only for existing tests/legacy callers.
_normalize_stage_output = _impl._normalize_stage_output
_canonical_hash = _impl._canonical_hash
_stable_corpus_value = _impl._stable_corpus_value
