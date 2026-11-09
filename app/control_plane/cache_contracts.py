from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from app.core.idempotency import stable_hash

CacheMode = Literal["off", "auto", "on"]
ExplicitCacheMaterialMode = Literal['inline_bytes_only', 'file_uri_allowed']

def explicit_cache_material_mode(cache) -> ExplicitCacheMaterialMode:
    mode=(cache or {}).get('explicit_cache_material_mode','inline_bytes_only')
    if mode not in ('inline_bytes_only','file_uri_allowed'):
        raise ValueError('Invalid explicit_cache_material_mode')
    return mode

def validate_supply_cache_transport(connection, contract, *, requires_file_adapter):
    mode=explicit_cache_material_mode(contract.cache)
    if mode=='file_uri_allowed' and 'stateful_resource' in contract.cache.get('supported_mechanisms',[]):
        transports=contract.metadata.get('material_transport',{}).get('supported_material_transports',[])
        if ('provider_files_api' not in transports or not requires_file_adapter
                or connection.protocol!='gemini_native'):
            raise ValueError('file_uri_allowed requires provider_files_api, registered Gemini file/cache adapters and requires_file_adapter')


CacheMechanism = Literal["stateful_resource", "breakpoint", "implicit_prefix"]


CacheResolutionStatus = Literal["pending", "finalized"]


def provider_managed_trial_enabled(config: dict[str, Any]) -> bool:
    """An explicit control-plane grant; no inferred threshold or certification."""
    return (
        config.get("verification_status") == "trial"
        and config.get("auto_trial_enabled") is True
        and config.get("threshold_mode") == "provider_managed_trial"
    )

