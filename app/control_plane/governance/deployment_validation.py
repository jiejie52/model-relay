"""Bounded deployment probes through guarded transport, outside user Requests.

Results describe actual observations; no draft can be relabelled as passed by
the workbook. Probe resources are never published as Session cache bindings.
"""
import asyncio
import json
import logging
import re
import secrets
from types import SimpleNamespace
from urllib.parse import quote
import httpx
from jsonschema import validate
from app.control_plane.connections import ConnectionRuntime
from app.runtime.transport_guard import provider_transport
from app.protocol_adapters.wire_projection.schema_projector import project_schema_for_provider
from app.canonical_schema import StructuredOutputSpec
from app.core.continuation_fingerprint import normalize_context_head
from app.control_plane.upstream_retention import resolve_upstream_retention
from app.protocol_adapters.wire_projection.gemini_interactions_wire import project_gemini_interactions_payload, project_gemini_interactions_response_format
from app.protocol_adapters.wire_projection.responses_wire import observe_responses_outcome
from app.runtime.continuation_contracts import ContinuationExecutionBinding
from app.protocol_adapters.profiles.gemini_interactions import extract_interactions_text
from app.protocol_adapters.profiles.openai_compatible import OpenAICompatibleResponsesProvider
from app.protocol_adapters.base import ProviderHTTPError
from app.protocol_adapters.error_observation import provider_http_error_observation
from app.observability import warning as log_warning

VALIDATOR_VERSION='deployment-probes/3'
GEMINI_INTERACTIONS_VALIDATOR_VERSION='deployment-probes/gemini-interactions/7'
logger = logging.getLogger("model-relay-deployment-validation")


def validation_version(protocol):
    return GEMINI_INTERACTIONS_VALIDATOR_VERSION if protocol == 'gemini_interactions' else VALIDATOR_VERSION

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
        if protocol == "gemini_native":
            target, key = payload.setdefault("generationConfig", {}), "maxOutputTokens"
        elif protocol == "gemini_interactions":
            target, key = payload.setdefault("generation_config", {}), "max_output_tokens"
        elif protocol == "responses":
            target, key = payload, "max_output_tokens"
        else:
            target, key = payload, "max_tokens"
        target[key] = max(target.get(key, 0), required)
    project_thinking(payload, {"thinking": {"version": THINKING_CONTRACT_VERSION, "effective": e}}, protocol)
    return True

def _interactions_probe_document():
    """A small valid PDF probes Files API document input without new dependencies."""
    stream = b"BT /F1 12 Tf 72 720 Td (Relay deployment verification.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    raw = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n")
    xref = len(raw)
    raw.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        raw.extend(f"{offset:010d} 00000 n \n".encode())
    raw.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(raw)


async def verify_supply(settings, plane, supply, *, journal=None):
    spec=plane.connection(supply.connection_id); cap=plane.contract(supply.capability_contract_id)
    profile=plane.protocol_profile(supply.protocol_profile_id)
    secret=plane.credential(supply.connection_id,settings)
    if not secret: return {'status':'pending','reason':'credential_missing'}
    runtime=ConnectionRuntime(spec,settings,secret,profile.canonical())
    file_name=cache_name=None
    cleanup_anchor_urls=[]
    facets=[]
    cont = cap.continuation if isinstance(cap.continuation, dict) else {}
    cont_policy = plane.continuation_policy(supply.continuation_policy_id) if getattr(supply, 'continuation_policy_id', None) else None
    cont_anchor_class = str(cont.get('anchor_class') or 'none')
    policy = cont_policy.canonical() if cont_policy is not None else {}
    retention = resolve_upstream_retention(spec.metadata, cap.canonical(), policy)
    probe_stateful_continuation = (
        retention["allow_store"]
        and cont_anchor_class == "immutable_tree_anchor"
        and cont.get("requires_upstream_store") is True
        and policy.get("default_mode") in {"prefer_stateful_anchor", "require_stateful_anchor"}
    )
    continuation_reason = None if probe_stateful_continuation else (
        retention["reason"] if not retention["allow_store"] else "continuation_not_enabled"
    )
    cleanup_incomplete = False
    store_outcome_unknown = False
    operation_started = False
    result = None

    async def cleanup_probe_anchors():
        nonlocal cleanup_incomplete
        if not cleanup_anchor_urls:
            return
        async with httpx.AsyncClient(timeout=3,transport=provider_transport(settings,supply.provider,url=runtime.rest_base,always=True,connection=runtime),trust_env=False) as cleanup:
            for anchor_url in reversed(cleanup_anchor_urls):
                try:
                    del_resp = await cleanup.delete(anchor_url, headers=runtime.headers())
                    if del_resp.status_code in (404, 405, 501):
                        cleanup_incomplete = True
                        log_warning(
                            logger,
                            'deployment_probe_anchor_delete_unsupported',
                            connection_id=spec.connection_id,
                            protocol=spec.protocol,
                            model=supply.model_pattern,
                            offering_id=supply.offering_id,
                            http_status=del_resp.status_code,
                        )
                    elif not 200 <= del_resp.status_code < 300:
                        cleanup_incomplete = True
                except Exception as exc:
                    cleanup_incomplete = True
                    log_warning(
                        logger,
                        'deployment_probe_anchor_delete_failed',
                        connection_id=spec.connection_id,
                        protocol=spec.protocol,
                        model=supply.model_pattern,
                        offering_id=supply.offering_id,
                        exception_type=type(exc).__name__,
                    )

    try:
        if policy.get("default_mode") == "require_stateful_anchor" and not probe_stateful_continuation:
            raise ProbeFailure(continuation_reason)
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0),transport=provider_transport(settings,supply.provider,
                url=runtime.rest_base,always=True,connection=runtime),trust_env=False) as client:
            async def request(method,url,**kwargs):
                response=await client.request(method,url,headers={**runtime.headers(),**kwargs.pop('headers',{})},**kwargs)
                if not 200<=response.status_code<300:
                    if spec.protocol == 'gemini_interactions':
                        error = ProviderHTTPError(response.status_code, response.content,
                            content_type=response.headers.get('content-type'), response_headers=dict(response.headers),
                            phase='deployment_probe', request_method=method, request_url=url,
                            raw_body_source='response.content')
                        log_warning(logger, 'deployment_probe_http_error', connection_id=spec.connection_id,
                            protocol=spec.protocol, model=supply.model_pattern, offering_id=supply.offering_id,
                            provider_http_error=provider_http_error_observation(error))
                    raise ProbeFailure('upstream_http_'+str(response.status_code))
                return response
            model=supply.metadata.get('upstream_model',supply.model_pattern)
            protocol=spec.protocol
            schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
            native=cap.structured_output.get('mode')=='native_json_schema'
            mechanisms=cap.cache.get('supported_mechanisms',[])
            modes=cap.metadata.get('material_transport',{}).get('supported_material_transports',[])
            provider = OpenAICompatibleResponsesProvider(
                settings, base_url=runtime.rest_base, api_key=secret, connection_id=spec.connection_id,
                channel_id=spec.channel_id,
            ) if protocol == "responses" else None
            probe_urls = set()
            nonce_head = secrets.token_hex(3)
            instruction = f"When asked to recall a verification code, reply only with {nonce_head}: followed by the requested codes."
            budget = min(4096, cap.supported_options.get("max_output_tokens", {}).get("max", 4096))

            def project_probe(prompt, *, parent=None, store=False, history=(), attachment=None, schema_probe=False,
                              tools=()):
                snapshot = {
                    "schema_version": "relay-request/2.4", "input": prompt, "current_query": prompt,
                    "provider": supply.provider, "model": model, "context_policy": "conversation",
                    "upstream_retention": retention, "effective_options": {"max_output_tokens": budget},
                    "normalized_head": normalize_context_head(instruction, list(tools), profile.canonical()),
                }
                if protocol == "responses":
                    snapshot["_relay_responses_continuation"] = {"store": store, "previous_response_id": parent}
                    # Responses stores a single logical user item per turn.
                    user_item = provider.make_user_item(prompt)
                    if attachment:
                        user_item = {**user_item, "content": [attachment, *user_item["content"]]}
                    snapshot["_relay_responses_current_user_item"] = user_item
                    payload = provider.project_payload(snapshot, session=None, material_prefix=[], history=list(history))
                    transport = {"kind": "responses", "user_item": user_item}
                    if schema_probe and native:
                        payload["text"] = {"format": {"type": "json_schema", "name": "validation", "strict": True, "schema": schema}}
                else:
                    mode = "stateful_anchor_delta" if parent else "stateful_root_init" if store else "stateless_full_replay"
                    binding = ContinuationExecutionBinding(
                        schema_version="relay-continuation-binding/2", effective_mode=mode,
                        mechanism="immutable_tree_anchor" if parent else "none", upstream_handle=parent,
                        store_upstream_state=store,
                    )
                    ctx = SimpleNamespace(
                        snapshot=snapshot, session={}, history=list(history),
                        material_ids=["probe-file"] if attachment else [],
                        material_bindings=[attachment] if attachment else [],
                        continuation_execution_binding=binding,
                    )
                    payload, user_content = project_gemini_interactions_payload(ctx)
                    transport = {"kind": "gemini_interactions", "interaction_user_input": user_content}
                    if schema_probe and native:
                        payload["response_format"] = project_gemini_interactions_response_format(
                            StructuredOutputSpec(mode="json_schema", schema=schema, name="validation",
                                                 strict=True, source="deployment_validation")
                        )
                _project_probe_thinking(payload, cap, protocol)
                return payload, transport

            async def inference_probe(prompt, *, parent=None, store=False, history=(), attachment=None, schema_probe=False,
                                      tools=()):
                payload, transport = project_probe(prompt, parent=parent, store=store, history=history,
                                                   attachment=attachment, schema_probe=schema_probe, tools=tools)
                url = runtime.api_url("interactions") if protocol == "gemini_interactions" else runtime.rest_base + "/responses"
                nonlocal store_outcome_unknown
                if store:
                    store_outcome_unknown = True
                response = await request("POST", url, json=payload)
                data = response.json()
                handle = data.get("id") if isinstance(data, dict) else None
                if store and isinstance(handle, str) and handle.strip() and handle not in {".", ".."}:
                    cleanup_url = url + "/" + quote(handle, safe="")
                    if cleanup_url not in probe_urls:
                        if journal is not None:
                            try:
                                await journal({"phase":"anchor_created", "handle":handle})
                            finally:
                                cleanup_anchor_urls.append(cleanup_url)
                        else:
                            cleanup_anchor_urls.append(cleanup_url)
                        probe_urls.add(cleanup_url)
                    store_outcome_unknown = False
                if protocol == "responses":
                    outcome = observe_responses_outcome(data)
                    if outcome["completion"] != "completed" or outcome["pending_client_tool"] or outcome["refusal"]:
                        raise ProbeFailure("responses_probe_not_completed")
                    text = provider.extract_visible_text(data)
                    transport["response_output"] = data["output"]
                else:
                    if not isinstance(data, dict) or data.get("error") or data.get("status") != "completed":
                        raise ProbeFailure("incomplete_interactions_probe")
                    text = extract_interactions_text(data)
                    transport["interaction_model_steps"] = data.get("steps") or []
                if not text.strip():
                    raise ProbeFailure("empty_probe_output")
                if store and (not isinstance(handle, str) or not handle.strip() or handle in {".", ".."}
                              or data.get("store") is False or handle == parent
                              or (protocol == "responses" and "previous_response_id" in data
                                  and data["previous_response_id"] != parent)):
                    raise ProbeFailure("stateful_continuation_handle_missing_or_invalid")
                return text.strip(), handle, transport

            async def certify_continuation():
                nonlocal operation_started
                if not probe_stateful_continuation:
                    return
                if journal is None:
                    raise ProbeFailure("anchor_probe_requires_durable_operation")
                await journal({"phase": "anchor_probe_started"})
                operation_started = True
                root_code, next_code = secrets.token_hex(5), secrets.token_hex(5)
                _, first, _ = await inference_probe(
                    f"Remember this root verification code: {root_code}. Reply OK.", store=True)
                text, second, _ = await inference_probe(
                    f"Store the second verification code {next_code}. Recall only the root verification code.",
                    parent=first, store=True)
                if text != f"{nonce_head}:{root_code}" or second == first:
                    raise ProbeFailure("stateful_continuation_recall_failed")
                text, third, _ = await inference_probe(
                    "Recall only the second verification code.", parent=second, store=True)
                if text != f"{nonce_head}:{next_code}" or len({first, second, third}) != 3:
                    raise ProbeFailure("stateful_continuation_recall_failed")
                facets.append("stateful_continuation")
                if not (cont.get("supports_multiturn_delta") and (policy.get("metadata") or {}).get("verify_multiturn_delta")):
                    return

                codes = [secrets.token_hex(5), secrets.token_hex(5)]
                transports = []
                for code in codes:
                    _, _, turn = await inference_probe(f"Remember supplemental code {code}. Reply OK.")
                    transports.append(turn)
                history = [{"transport_history": turn} for turn in transports] if provider is None else transports
                text, branch, _ = await inference_probe(
                    "Recall the root code and both supplemental codes in their original order, separated by commas.",
                    parent=first, store=True, history=history,
                )
                if text != f"{nonce_head}:{root_code},{codes[0]},{codes[1]}" or branch in {first, second, third}:
                    raise ProbeFailure("multiturn_delta_recall_failed")
                facets.extend(["multiturn_delta", "branching"])

            async def certify_optional_continuation():
                nonlocal continuation_reason
                try:
                    await certify_continuation()
                except (ProbeFailure, httpx.HTTPError, ValueError) as exc:
                    continuation_reason = str(exc) if isinstance(exc, ProbeFailure) else type(exc).__name__
                    if policy.get("default_mode") == "require_stateful_anchor" and "stateful_continuation" not in facets:
                        raise

            if 'external_signed_url' in modes:
                raise ProbeFailure('external_url_requires_dedicated_reachability_validation')
            if protocol in ('gemini_native', 'gemini_interactions'):
                file_part=None
                if 'provider_files_api' in modes and retention['reason'] != 'zdr_forbidden':
                    minimum=max([p.get('minimum_cacheable_tokens',1024) for p in cap.cache.get('mechanism_profiles',{}).values()] or [1])
                    mime_type = 'application/pdf' if protocol == 'gemini_interactions' else 'text/plain'
                    raw = _interactions_probe_document() if protocol == 'gemini_interactions' else ('configuration verification '*(minimum*2 if mechanisms else 2)).encode()
                    if len(raw)>2*1024*1024: raise ProbeFailure('validation_fixture_exceeds_limit')
                    url=spec.upload_base_url
                    if not url: raise ProbeFailure('upload_endpoint_missing')
                    url=url.rstrip('/')+('' if url.rstrip('/').endswith('/files') else '/files')
                    start=await request('POST',url,headers={'X-Goog-Upload-Protocol':'resumable','X-Goog-Upload-Command':'start',
                        'X-Goog-Upload-Header-Content-Length':str(len(raw)),'X-Goog-Upload-Header-Content-Type':mime_type},json={'file':{'display_name':'relay-deployment-verification'}})
                    upload_url=start.headers.get('x-goog-upload-url')
                    if not upload_url: raise ProbeFailure('upload_url_missing')
                    result=(await request('POST',upload_url,headers={'Content-Type':mime_type,'X-Goog-Upload-Offset':'0',
                        'X-Goog-Upload-Command':'upload, finalize'},content=raw)).json()
                    file=result.get('file',result);file_name=file.get('name')
                    if not file_name or not re.fullmatch(r'files/[A-Za-z0-9._-]+',file_name): raise ProbeFailure('invalid_file_handle')
                    for _ in range(5):
                        if file.get('state')=='ACTIVE': break
                        if file.get('state')=='FAILED': raise ProbeFailure('file_processing_failed')
                        await asyncio.sleep(.2)
                        file=(await request('GET',runtime.api_url(file_name))).json()
                    if file.get('state')!='ACTIVE' or not file.get('uri'): raise ProbeFailure('file_not_ready')
                    native_file_part={'fileData':{'fileUri':file['uri'],'mimeType':mime_type}}
                    file_part = native_file_part if protocol == 'gemini_native' else {
                        "material_id":"probe-file", "representation":"gemini_file_uri",
                        "external_uri":file["uri"], "content_type":mime_type,
                    }
                    facets.append('provider_files_api')
                if protocol == 'gemini_interactions':
                    prompt = 'Return exactly {"ok":true} as JSON.' if native else 'Reply briefly with OK.'
                    text, _, _ = await inference_probe(prompt, attachment=file_part, schema_probe=True)
                    if native:
                        try:
                            parsed = json.loads(text)
                            validate(parsed, schema)
                            if parsed.get("ok") is not True:
                                raise ValueError("Probe must answer ok=true")
                        except Exception:
                            raise ProbeFailure("native_schema_probe_failed") from None
                    await certify_optional_continuation()
                    if not native: facets.append("relay_result_object")
                    facets.extend(["inference", "representative_thinking"] + (["native_json_schema"] if native else []))
                    if "implicit_prefix" in mechanisms:
                        facets.append("implicit_prefix")
                else:
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
                        operation_started = True
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
                if protocol == 'responses':
                    if set(mechanisms) - {'implicit_prefix'} or set(modes) - {'inline_data'}:
                        raise ProbeFailure('resource_validation_not_implemented_for_protocol')
                elif mechanisms or set(modes)-{'inline_data'}:
                    raise ProbeFailure('resource_validation_not_implemented_for_protocol')
                prompt='Return exactly {"ok":true}.'
                if protocol in ('chat_completions','moonshot_chat'):
                    payload={'model':model,'messages':[{'role':'user','content':prompt}],'max_tokens':128}
                    if native: payload['response_format']={'type':'json_schema','json_schema':{'name':'validation','strict':True,'schema':schema}}
                    _project_probe_thinking(payload,cap,protocol)
                    data=(await request('POST',runtime.rest_base+'/chat/completions',json=payload)).json()
                    text=data.get('choices',[{}])[0].get('message',{}).get('content','')
                elif protocol=='responses':
                    text, _, _ = await inference_probe(prompt, schema_probe=True)
                    if native:
                        try:
                            validate(json.loads(text), schema)
                        except Exception:
                            raise ProbeFailure("native_schema_probe_failed") from None
                    await certify_optional_continuation()
                    if native:
                        facets.append('native_json_schema')
                    if 'implicit_prefix' in mechanisms:
                        facets.append('implicit_prefix')
                elif protocol=='claude_messages':
                    if native: raise ProbeFailure('native_schema_probe_not_implemented_for_protocol')
                    data=(await request('POST',runtime.rest_base+'/v1/messages',headers={'anthropic-version':'2023-06-01'},json={
                        'model':model,'max_tokens':128,'messages':[{'role':'user','content':prompt}]})).json()
                    text=''.join(p.get('text','') for p in data.get('content',[]))
                else: raise ProbeFailure('unsupported_protocol')
                if protocol != 'responses':
                    if not text: raise ProbeFailure('empty_probe_output')
                    if native:
                        try: validate(json.loads(text),schema)
                        except Exception: raise ProbeFailure('native_schema_probe_failed') from None
                facets.append('inference')
            tool_kinds = (profile.metadata or {}).get("hosted_tools") or []
            for kind in tool_kinds:
                _, _, transport = await inference_probe(
                    "Use the provided search tool now to find the latest NASA newsroom headline. Reply with one headline.",
                    tools=[{"type": kind}],
                )
                if protocol == "responses":
                    observed_tool = any(item.get("type") == "web_search_call" and item.get("status") == "completed"
                                        for item in transport["response_output"] if isinstance(item, dict))
                else:
                    steps = transport["interaction_model_steps"]
                    observed_tool = {"google_search_call", "google_search_result"} <= {
                        step.get("type") for step in steps if isinstance(step, dict)
                    }
                if not observed_tool:
                    raise ProbeFailure("hosted_tool_evidence_missing:" + kind)
                facets.append("hosted_tool:" + kind)
            result = {'status':'ready','facets':facets,'validator_version':validation_version(protocol),
                      'continuation_reason':continuation_reason}
    except asyncio.CancelledError: raise
    except ProbeFailure as exc: result = {'status':'pending','reason':str(exc)}
    except Exception as exc: result = {'status':'pending','reason':'validation_'+type(exc).__name__}
    finally:
        if cleanup_anchor_urls:
            try:
                await cleanup_probe_anchors()
            except Exception:
                cleanup_incomplete = True
        # Bounded best-effort removal of disposable deployment resources.
        for handle in [cache_name,file_name]:
            if handle:
                try:
                    async with httpx.AsyncClient(timeout=3,transport=provider_transport(settings,supply.provider,url=runtime.rest_base,always=True,connection=runtime),trust_env=False) as cleanup:
                        response = await cleanup.delete(runtime.api_url(handle),headers=runtime.headers())
                        if handle == cache_name and not 200 <= response.status_code < 300:
                            cleanup_incomplete = True
                except Exception:
                    if handle == cache_name:
                        cleanup_incomplete = True
        await runtime.pool.aclose()
    if operation_started:
        cleanup_incomplete = cleanup_incomplete or store_outcome_unknown or not (cache_name or cleanup_anchor_urls)
        result['probe_cleanup_incomplete'] = cleanup_incomplete
        result['probe_operation_closed'] = not cleanup_incomplete
    return result
