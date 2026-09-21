# Gemini Relay-Authoritative Size 0.5.3 Implementation

## Frozen rule

Relay no longer requires Dify/caller to provide authoritative aggregate file bytes.

For `provider=gemini`:

1. `/v2/materials` reads the actual payload bytes itself and records `actual_size` + SHA-256.
2. Per-material ingress chooses an immediate safe transport from the bytes Relay actually received.
3. Before provider dispatch, `BindingResolver` reloads the exact Request material set and sums `relay_materials.actual_size` itself.
4. That server-calculated Request total is authoritative:

```text
Relay sum(actual_size) <= 103809024
  -> Supabase Private Bucket
  -> renewable Signed URL
  -> gemini_external_url
  -> GeminiNativeAdapter fileData.fileUri

Relay sum(actual_size) > 103809024
  -> gemini_file_uri
  -> AIHubMix Gemini Native Proxy -> Gemini Files API
  -> GeminiNativeAdapter fileData.fileUri
```

`GEMINI_FILES_THRESHOLD_BYTES` remains configurable; default is 99 MiB.

## Caller aggregate fields

The 0.5.2 compatibility fields remain accepted:

- `request_file_total_bytes`
- `request_file_count`
- `material_batch_id`
- equivalent `X-Relay-*` headers

They are diagnostic hints only. They do not select transport. If a caller total differs from bytes Relay actually received, Relay logs `gemini_client_size_hint_ignored` and continues with its own measurement.

## Multi-file requests

`POST /v2/materials` still creates one Material at a time, so each small material can initially receive a Supabase External URL binding. At Request freeze, Relay calculates the complete material total again.

If several individually-small materials together cross 99 MiB, Relay promotes them to Gemini Files API before model inference, freezes the new binding generation, and deletes the input-file Supabase fallback copy after the provider file binding is ready. This guarantees the final Provider request uses the Request-level authoritative decision.

Because the public Material API remains one-material-at-a-time, a multi-file request can briefly have Supabase bridge objects before the full Request set is known. Eliminating even this transient staging requires a future atomic batch-ingress API. 0.5.3 removes those input copies before dispatch once the request total is known.

## Recovery

- A frozen Request binding snapshot is never recalculated on retry.
- `<=99 MiB` External URL bindings remain renewable from the retained Supabase object.
- `>99 MiB` promoted Files API bindings increment binding generation and then remove the input fallback copy.
- Legacy 0.5.2 small materials that were already stored only in Gemini Files API with no fallback bytes cannot be converted back to External URL; Relay returns `MATERIAL_REUPLOAD_REQUIRED` instead of silently keeping the wrong transport.

## Logging

New/changed events:

- `gemini_material_transport_selected` with `decision_source=relay_actual_bytes`
- `gemini_client_size_hint_ignored`
- `gemini_request_size_calculated`
- `gemini_request_size_authoritative`
- `gemini_request_transport_reconciled`
- `gemini_request_transport_promotion_started/completed`
- `gemini_supabase_input_copy_removed`
- `gemini_supabase_input_copy_cleanup_failed`

No file URL query, API key, token, or raw prompt is added to normal logs.

## Database

No schema migration is required. 0.5.3 reuses `actual_size`, provider binding generation, `material_fallback_objects`, and existing request binding snapshots.
