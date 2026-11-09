"""Evidence from a declared first-party connection and the exact emitted request.

This is protocol evidence, not an independent billing receipt or a provider-signed
attestation. Invalid/truncated/repaired output never confirms native constraints.
"""
import json
from urllib.parse import urlsplit
from jsonschema import validate

def native_response_facts(data,payload,context,connection,observed):
    metadata=(context.snapshot.get('protocol_profile') or {}).get('metadata') or {}
    if metadata.get('evidence_source_mode','gateway_headers')!='first_party_native_payload': return observed
    auth=context.snapshot.get('execution_authorization') or {}
    if auth.get('evidence_source_mode')!='first_party_native_payload': return observed
    host=urlsplit(connection.rest_base).hostname
    if host not in metadata.get('allowed_hosts',[]) or metadata.get('affinity_policy',{}).get('enabled'):
        return observed
    result={**observed,'evidence_source_mode':'first_party_native_payload','native_constraint_enforced':None}
    model=data.get('modelVersion')
    if not isinstance(model,str) or not model.strip(): return result
    result['actual_model']=model
    # official_direct records connection provenance; it makes no price/free claim.
    if isinstance(data.get('usageMetadata'),dict): result['billing_tier']='official_direct'
    candidates=data.get('candidates')
    if not isinstance(candidates,list) or not candidates or not isinstance(candidates[0],dict):return result
    first=candidates[0]
    if observed.get('native_constraint_enforced') is False or observed.get('protocol_downgraded') is True:
        result['native_constraint_enforced']=False
        return result
    generation=payload.get('generationConfig') or {}
    schema=generation.get('responseSchema') or generation.get('responseJsonSchema')
    if first.get('finishReason')!='STOP' or generation.get('responseMimeType')!='application/json' or not isinstance(schema,dict) or not schema or observed.get('json_repaired') is True:
        return result
    text=''.join(p.get('text','') for p in (first.get('content') or {}).get('parts',[]) if not p.get('thought'))
    try:
        parsed=json.loads(text)
        validate(parsed,schema)
        original=(context.snapshot.get('structured_output') or {}).get('schema')
        if original:validate(parsed,original)
    except Exception:return result
    result.update(native_constraint_enforced=True,json_repaired=False,
        native_evidence_basis='sealed_schema_request+normal_finish+valid_raw_json')
    return result
