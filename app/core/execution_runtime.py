"""Compatibility import for the physical runtime; Session commits moved to the execution plane."""
from app.runtime.engine import RelayRuntimeEngine
from app.shared_contracts import ExecutionConflictError as SessionConflictError
SharedExecutionRuntime = RelayRuntimeEngine
