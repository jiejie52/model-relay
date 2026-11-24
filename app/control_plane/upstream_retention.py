"""Resolve the Relay authorization to persist upstream conversation state."""


def resolve_upstream_retention(connection_metadata=None, capability=None, policy=None, *, tenant_zdr=False):
    connection_metadata = connection_metadata or {}
    capability = capability or {}
    policy = policy or {}
    zero_retention = tenant_zdr or any(
        source.get("zero_data_retention") is True
        for source in (connection_metadata, capability, capability.get("metadata") or {},
                       capability.get("continuation") or {}, policy.get("metadata") or {})
    )
    allowed = not zero_retention and connection_metadata.get("allow_upstream_store") is True
    reason = "zdr_forbidden" if zero_retention else "authorized" if allowed else "storage_not_authorized"
    return {"version": "relay-upstream-retention/1", "allow_store": allowed, "reason": reason}
