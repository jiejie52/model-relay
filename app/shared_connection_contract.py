"""Pure immutable connection identity checks shared across layer contracts."""
def frozen_connection_fields(snapshot,route=None):
    from app.shared_contracts import ExecutionConflictError
    route=route or {}
    for value in (snapshot,route):
        if value.get('connection_contract_version') not in (None,2):
            raise ExecutionConflictError('Unsupported connection contract version')
        if value.get('connection_contract_version') is None and (value.get('connection_spec_hash') or value.get('account_scope_id')):
            raise ExecutionConflictError('Typed connection identity requires its contract version')
    new=snapshot.get('connection_contract_version')==2 or route.get('connection_contract_version')==2
    if not new:return '', ''
    values=[]
    for key in ('connection_spec_hash','account_scope_id'):
        value=snapshot.get(key)
        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise ExecutionConflictError('New connection contract requires a SHA256 '+key)
        if route and route.get(key)!=value:raise ExecutionConflictError('Snapshot and Session connection identity differ')
        values.append(value)
    return tuple(values)
