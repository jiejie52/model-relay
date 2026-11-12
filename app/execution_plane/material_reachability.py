"""Supply-scoped binding eligibility; durability labels alone grant nothing."""
from datetime import datetime,timezone

from app.core.provider_binding_state import provider_binding_is_usable

def binding_reachable(binding, *, connection_id, account_scope_hash, transports):
    if not binding: return False
    if binding.get('connection_id',connection_id)!=connection_id or binding.get('account_scope_hash',account_scope_hash)!=account_scope_hash: return False
    if not provider_binding_is_usable(binding): return False
    expiry=binding.get('expires_at')
    if expiry:
        try:
            parsed=datetime.fromisoformat(str(expiry).replace('Z','+00:00'))
            if parsed.tzinfo is None or parsed<=datetime.now(timezone.utc): return False
        except (ValueError,TypeError): return False
    representation=binding.get('representation')
    if not transports: return True  # Historical contracts did not declare transport modes.
    required={'gemini_file_uri':'provider_files_api','gemini_external_url':'external_signed_url','gemini_inline_data':'inline_data'}.get(representation)
    return required in transports
