# Gemini Dual Transport 0.5.2 Implementation

## Frozen rule

For `provider=gemini` + `purpose=inference_input`, Relay keeps the same internal Gemini Native route and selects only the file transport:

```text
sum(current request file bytes) <= 103809024
  -> Supabase original input object
  -> short-lived Signed URL
  -> provider_material_bindings.representation=gemini_external_url
  -> GeminiNativeAdapter fileData.fileUri=<https signed URL>

sum(current request file bytes) > 103809024
  -> no Supabase input-file object
  -> AIHubMix Gemini Native Proxy
  -> Gemini Files API
  -> provider_material_bindings.representation=gemini_file_uri
  -> GeminiNativeAdapter fileData.fileUri=<Gemini fileUri>
```

The threshold is configurable with `GEMINI_FILES_THRESHOLD_BYTES`; default is 99 MiB.

## Aggregate information

The current public Material endpoint creates one Material at a time. Exact multi-file routing therefore needs the caller to repeat aggregate request information with every Material:

- body/form: `request_file_total_bytes`, `request_file_count`, `material_batch_id`
- or headers: `X-Relay-Request-File-Total-Bytes`, `X-Relay-Request-File-Count`, `X-Relay-Material-Batch-Id`

If aggregate bytes are missing and `request_file_count=1`, Relay infers total from the received material. If the aggregate cannot be proven, Files API is selected conservatively.

## Supabase implementation

The <=99 MiB path reuses Relay's existing object-storage abstraction instead of embedding Workflow-specific HTTP code:

- `FallbackObjectStorage.store()` -> `SupabaseObjectStorage.put_bytes()`
- object metadata stays in `relay_objects` + `material_fallback_objects`
- upload preserves content type and uses Supabase `x-upsert=true`
- `FallbackObjectStorage.sign_read_url()` -> Supabase sign endpoint
- TTL uses `SUPABASE_SIGNED_URL_TTL`, default 604800 seconds and capped at 7 days
- `material_id` remains the stable business identity; Signed URL is only a renewable provider binding

The implementation intentionally does not copy the Dify Workflow's certificate-verification bypass for temporary Dify URLs. Relay source fetching continues to use `safe_fetch.py` and its SSRF/TLS policy.

## Binding refresh

When a `gemini_external_url` binding expires:

1. `BindingResolver` finds the retained fallback object.
2. It generates a new Supabase Signed URL.
3. It creates the next binding generation with the same material hash/object.
4. It does not call Gemini Files API.

For the >99 MiB `gemini_file_uri` path, existing Gemini Files API lifecycle and rebind behavior remain unchanged, except that input-file Supabase persistence is explicitly suppressed.

## Logging

Transport decision and bridge lifecycle use structured events:

- `gemini_material_transport_selected`
- `gemini_request_file_total_missing`
- `material_fallback_store_started/completed/failed`
- `gemini_external_url_sign_started/completed`
- `gemini_external_url_binding_failed`
- `provider_file_binding_started/completed/failed`
- `gemini_files_supabase_policy_suppressed`
- `material_upload_completed/failed`

Logs include `ingress_id`, `material_id`, provider/model, route/connection (internal logs only), batch id, aggregate bytes, threshold, phase and duration where available.

## Database

No schema change is required. 0.5.2 reuses the 0.4.0 native-file migration tables and stores transport facts in existing material metadata/provider binding/fallback rows.
