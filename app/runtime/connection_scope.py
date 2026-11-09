"""Task-local physical connection selection; no routing or fallback."""
from contextvars import ContextVar
from contextlib import contextmanager
_current = ContextVar("relay_connection_identity", default=None)
_client = ContextVar("relay_connection_client", default=None)
def current_identity(): return _current.get()
def current_client(): return _client.get()
@contextmanager
def connection_scope(identity=None, client=None):
    a, b = _current.set(identity), _client.set(client)
    try: yield
    finally: _client.reset(b); _current.reset(a)

class ScopedAdapter:
    def __init__(self, adapter, connection): self.adapter, self.connection = adapter, connection
    def __getattr__(self, name):
        member = getattr(self.adapter, name)
        import inspect
        if not inspect.iscoroutinefunction(member): return member
        async def invoke(*args, **kwargs):
            if name == "execute" and args:
                bundle = args[0]
                bundle.verify_integrity()
                if bundle.connection_spec_hash and (bundle.connection_spec_hash!=self.connection.spec.identity_hash or bundle.account_scope_id!=self.connection.account_scope_hash):
                    raise ValueError('CONNECTION_SPEC_OR_SCOPE_MISMATCH')
                expected = bundle.snapshot.get("connection_identity_hash")
                if expected and expected != self.connection.identity_hash:
                    raise ValueError("CONNECTION_IDENTITY_MISMATCH")
            with connection_scope(self.connection.identity_hash, self.connection):
                return await member(*args, **kwargs)
        return invoke
