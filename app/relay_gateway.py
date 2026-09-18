"""Legacy import shim; implementation lives in app.compatibility.relay_gateway."""
from .compatibility.relay_gateway import router, dify_relay_gateway

__all__ = ["router", "dify_relay_gateway"]
