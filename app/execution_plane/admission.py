"""Read-only candidate qualification; side effects belong to atomic acceptance."""
from app.core.idempotency import stable_hash
from app.execution_plane.route_resolver import RouteResolutionError,_PROVIDER_ALIASES
from app.execution_plane.services import ExecutionRequestError
from app.execution_plane.acceptance_support import _validate_materials_for_route

def reservation_scope(snapshot, connection_id):
    c=snapshot.connection(connection_id)
    return stable_hash([c.channel_id,c.account_id,c.project_id,c.region])

async def qualify_candidates(services, body):
    resolver=services.route_resolver
    failures={}
    provider=body.provider.strip().lower()
    provider=_PROVIDER_ALIASES.get(provider,provider)
    for entry in resolver.catalog.candidates(provider=provider,model=body.model,deployment_id=services.settings.deployment_id):
        policy=entry.quota
        c=resolver.config_snapshot.connection(entry.connection_id)
        if policy.get('allowed_tenants') and body.tenant_id not in policy['allowed_tenants']:
            failures[entry.offering_id]='tenant_not_authorized';continue
        if policy.get('allowed_regions') and c.region not in policy['allowed_regions']:
            failures[entry.offering_id]='region_not_authorized';continue
        try:
            route=resolver.resolve(provider=body.provider,model=body.model,purpose='session',
                requirements=body.capability_requirements.model_dump(mode='json'),offering_id=entry.offering_id)
            await _validate_materials_for_route(services,material_ids=body.material_ids,
                tenant_id=body.tenant_id,conversation_hash=body.conversation_hash,route=route)
        except (RouteResolutionError,ExecutionRequestError) as exc:
            detail=getattr(exc,'detail',{})
            failures[entry.offering_id]=getattr(exc,'code',None) or (detail.get('code') if isinstance(detail,dict) else None) or 'material_unreachable_for_scope'
    return failures
