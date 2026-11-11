"""Bounded deployment probes through guarded transport, outside user Requests.

Results describe actual observations; no draft can be relabelled as passed by
the workbook. Probe resources are never published as Session cache bindings.
"""
import asyncio
import json
import re
import httpx
from jsonschema import validate
from app.control_plane.connections import ConnectionRuntime
from app.runtime.transport_guard import provider_transport
from app.protocol_adapters.wire_projection.schema_projector import project_schema_for_provider

VALIDATOR_VERSION='deployment-probes/1'

class ProbeFailure(RuntimeError): pass

def _project_probe_thinking(payload, cap, protocol):
    """Probe a declared representative mapping through the registered projector."""
    spec = cap.thinking
    if spec.get("version") != "relay-thinking/2": return False
    from app.protocol_adapters.thinking_projection import project_thinking
    from app.thinking_contracts import THINKING_CONTRACT_VERSION
    mappings = spec["mappings"]
    intent = next((i for i in ("LOW", "MEDIUM", "HIGH", "XHIGH", "MAX")
        if mappings[i]["is_allowed"] and mappings[i]["satisfies_require"]), "AUTO")
    r = mappings[intent]
    e = {"mode": r["effective_mode"], "wire_action": r["wire_action"],
         "wire_strategy": spec["wire_strategy"], "level": r.get("effective_level"),
         "budget_tokens": r.get("effective_budget")}
    if intent == "AUTO" and e["mode"] != "fixed_on":
        e.update(wire_action="omit", level=None, budget_tokens=None)
    if e["wire_action"] == "send_budget" and (spec.get("when_thinking_active") or {}).get("budget_lt_max_output"):
        limit = cap.supported_options.get("max_output_tokens", {}).get("max")
        required = e["budget_tokens"] + 1
        if limit is None or required > limit: raise ProbeFailure("probe_budget_exceeds_output_contract")
        target, key = (payload.setdefault("generationConfig", {}), "maxOutputTokens") if protocol == "gemini_native" else (payload, "max_output_tokens" if protocol == "responses" else "max_tokens")
        target[key] = max(target.get(key, 0), required)
    project_thinking(payload, {"thinking": {"version": THINKING_CONTRACT_VERSION, "effective": e}}, protocol)
    return True

async def verify_supply(settings, plane, supply, *, journal=None):
    spec=plane.connection(supply.connection_id); cap=plane.contract(supply.capability_contract_id)
    profile=plane.protocol_profile(supply.protocol_profile_id)
    secret=plane.credential(supply.connection_id,settings)
    if not secret: return {'status':'pending','reason':'credential_missing'}
    runtime=ConnectionRuntime(spec,settings,secret,profile.canonical())
    file_name=cache_name=None
    facets=[]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0),transport=provider_transport(settings,supply.provider,
                url=runtime.rest_base,always=True,connection=runtime),trust_env=False) as client:
            async def request(method,url,**kwargs):
                response=await client.request(method,url,headers={**runtime.headers(),**kwargs.pop('headers',{})},**kwargs)
                if not 200<=response.status_code<300: raise ProbeFailure('upstream_http_'+str(response.status_code))
                return response
            model=supply.metadata.get('upstream_model',supply.model_pattern)
            protocol=spec.protocol
            schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
            native=cap.structured_output.get('mode')=='native_json_schema'
            mechanisms=cap.cache.get('supported_mechanisms',[])
            modes=cap.metadata.get('material_transport',{}).get('supported_material_transports',[])
            if 'external_signed_url' in modes:
                raise ProbeFailure('external_url_requires_dedicated_reachability_validation')
            if protocol=='gemini_native':
                file_part=None
                if 'provider_files_api' in modes:
                    minimum=max([p.get('minimum_cacheable_tokens',1024) for p in cap.cache.get('mechanism_profiles',{}).values()] or [1])
                    raw=('configuration verification '*(minimum*2 if mechanisms else 2)).encode()
                    if len(raw)>2*1024*1024: raise ProbeFailure('validation_fixture_exceeds_limit')
                    url=spec.upload_base_url
                    if not url: raise ProbeFailure('upload_endpoint_missing')
                    url=url.rstrip('/')+('' if url.rstrip('/').endswith('/files') else '/files')
                    start=await request('POST',url,headers={'X-Goog-Upload-Protocol':'resumable','X-Goog-Upload-Command':'start',
                        'X-Goog-Upload-Header-Content-Length':str(len(raw)),'X-Goog-Upload-Header-Content-Type':'text/plain'},json={'file':{'display_name':'relay-deployment-verification'}})
                    upload_url=start.headers.get('x-goog-upload-url')
                    if not upload_url: raise ProbeFailure('upload_url_missing')
                    result=(await request('POST',upload_url,headers={'Content-Type':'text/plain','X-Goog-Upload-Offset':'0',
                        'X-Goog-Upload-Command':'upload, finalize'},content=raw)).json()
                    file=result.get('file',result);file_name=file.get('name')
                    if not file_name or not re.fullmatch(r'files/[A-Za-z0-9._-]+',file_name): raise ProbeFailure('invalid_file_handle')
                    for _ in range(5):
                        if file.get('state')=='ACTIVE': break
                        if file.get('state')=='FAILED': raise ProbeFailure('file_processing_failed')
                        await asyncio.sleep(.2)
                        file=(await request('GET',runtime.api_url(file_name))).json()
                    if file.get('state')!='ACTIVE' or not file.get('uri'): raise ProbeFailure('file_not_ready')
                    file_part={'fileData':{'fileUri':file['uri'],'mimeType':'text/plain'}}
                    facets.append('provider_files_api')
                generation={'maxOutputTokens':min(1024,cap.supported_options.get('max_output_tokens',{}).get('max',1024))}
                if native: generation.update(responseMimeType='application/json',
                    responseSchema=project_schema_for_provider(schema,provider='gemini',model=model).schema)
                payload={'contents':[{'role':'user','parts':([file_part] if file_part else [])+[{'text':'Return exactly {"ok":true} as JSON.'}]}], 'generationConfig':generation}
                accepted=cap.thinking.get('accepted_levels',['auto'])
                levels=([level for level in accepted if level!='auto'] or accepted)[:1]
                last=None
                for level in levels:
                    config=dict(generation)
                    probe_payload={**payload,'generationConfig':config}
                    if _project_probe_thinking(probe_payload,cap,protocol):
                        config=probe_payload['generationConfig']
                    elif cap.thinking.get('wire_strategy')=='gemini_thinking_budget':
                        budget=cap.thinking.get('budget_map',{}).get(level)
                        if type(budget) is not int or budget < -1: raise ProbeFailure('thinking_budget_unexpressible')
                        config['thinkingConfig']={'thinkingBudget':budget}
                    elif level!='auto': config['thinkingConfig']={'thinkingLevel':level.upper()}
                    last=(await request('POST',runtime.api_url('models/'+model+':generateContent'),json={**payload,'generationConfig':config})).json()
                    text=''.join(p.get('text','') for c in last.get('candidates',[]) for p in c.get('content',{}).get('parts',[]) if not p.get('thought'))
                    if not text: raise ProbeFailure('empty_probe_output')
                    if native:
                        try: validate(json.loads(text),schema)
                        except Exception: raise ProbeFailure('native_schema_probe_failed') from None
                facets.extend(['inference','representative_thinking']+(['native_json_schema'] if native else []))
                if 'stateful_resource' in mechanisms:
                    prefix={'contents':[{'role':'user','parts':[file_part] if file_part else [{'text':'configuration verification '*8192}]}]}
                    count=(await request('POST',runtime.api_url('models/'+model+':countTokens'),json=prefix)).json().get('totalTokens',0)
                    minimum=cap.cache['mechanism_profiles']['stateful_resource']['minimum_cacheable_tokens']
                    if count<minimum: raise ProbeFailure('probe_context_below_cache_threshold')
                    ttl=cap.cache['mechanism_profiles']['stateful_resource'].get('ttl_seconds',300)
                    if journal is None: raise ProbeFailure('cache_probe_requires_durable_operation')
                    await journal({'phase':'cache_create_started'})
                    data=(await request('POST',runtime.api_url('cachedContents'),json={'model':'models/'+model,**prefix,'ttl':str(ttl)+'s'})).json()
                    cache_name=data.get('name')
                    if not cache_name or not re.fullmatch(r'cachedContents/[A-Za-z0-9._-]+',cache_name): raise ProbeFailure('invalid_cache_handle')
                    await journal({'phase':'cache_created','handle':cache_name})
                    cached={'cachedContent':cache_name,'contents':[{'role':'user','parts':[{'text':'Return {"ok":true}.'}]}],'generationConfig':generation}
                    result=(await request('POST',runtime.api_url('models/'+model+':generateContent'),json=cached)).json()
                    if not result.get('usageMetadata',{}).get('cachedContentTokenCount',0): raise ProbeFailure('cache_hit_not_observed')
                    facets.append('stateful_resource')
                if 'implicit_prefix' in mechanisms:
                    if not last.get('usageMetadata',{}).get('cachedContentTokenCount',0):
                        repeated=(await request('POST',runtime.api_url('models/'+model+':generateContent'),json=payload)).json()
                        if not repeated.get('usageMetadata',{}).get('cachedContentTokenCount',0): raise ProbeFailure('implicit_hit_not_observed')
                    facets.append('implicit_prefix')
            else:
                if mechanisms or set(modes)-{'inline_data'}: raise ProbeFailure('resource_validation_not_implemented_for_protocol')
                prompt='Return exactly {"ok":true}.'
                if protocol in ('chat_completions','moonshot_chat'):
                    payload={'model':model,'messages':[{'role':'user','content':prompt}],'max_tokens':128}
                    if native: payload['response_format']={'type':'json_schema','json_schema':{'name':'validation','strict':True,'schema':schema}}
                    _project_probe_thinking(payload,cap,protocol)
                    data=(await request('POST',runtime.rest_base+'/chat/completions',json=payload)).json()
                    text=data.get('choices',[{}])[0].get('message',{}).get('content','')
                elif protocol=='responses':
                    payload={'model':model,'input':prompt,'max_output_tokens':128}
                    if native: payload['text']={'format':{'type':'json_schema','name':'validation','strict':True,'schema':schema}}
                    _project_probe_thinking(payload,cap,protocol)
                    data=(await request('POST',runtime.rest_base+'/responses',json=payload)).json()
                    text=''.join(p.get('text','') for o in data.get('output',[]) for p in o.get('content',[]) if p.get('type')=='output_text')
                elif protocol=='claude_messages':
                    if native: raise ProbeFailure('native_schema_probe_not_implemented_for_protocol')
                    data=(await request('POST',runtime.rest_base+'/v1/messages',headers={'anthropic-version':'2023-06-01'},json={
                        'model':model,'max_tokens':128,'messages':[{'role':'user','content':prompt}]})).json()
                    text=''.join(p.get('text','') for p in data.get('content',[]))
                else: raise ProbeFailure('unsupported_protocol')
                if not text: raise ProbeFailure('empty_probe_output')
                if native:
                    try: validate(json.loads(text),schema)
                    except Exception: raise ProbeFailure('native_schema_probe_failed') from None
                facets.append('inference')
            return {'status':'ready','facets':facets,'validator_version':VALIDATOR_VERSION}
    except asyncio.CancelledError: raise
    except ProbeFailure as exc: return {'status':'pending','reason':str(exc)}
    except Exception as exc: return {'status':'pending','reason':'validation_'+type(exc).__name__}
    finally:
        # Bounded best-effort removal of disposable deployment resources.
        for handle in [cache_name,file_name]:
            if handle:
                try:
                    async with httpx.AsyncClient(timeout=3,transport=provider_transport(settings,supply.provider,url=runtime.rest_base,always=True,connection=runtime),trust_env=False) as cleanup:
                        await cleanup.delete(runtime.api_url(handle),headers=runtime.headers())
                except Exception: pass
        await runtime.pool.aclose()
