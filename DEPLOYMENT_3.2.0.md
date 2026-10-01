# Model Relay 3.2.0 - Gemini Physical Cache Projection Deployment

## Database

3.2.0 adds no SQL migration. The database must already include:

```text
sql/005_relay_cache_control.sql
sql/006_gemini_stateful_cache.sql
```

Do not remove or loosen the 006 `unknown` cache-operation semantics. This release deliberately logs/records missing-handle or uncertain-create outcomes and does not recreate them.

## Rollout order

1. Verify SQL 006 RPCs are available (`start_cache_operation_v31`, `record_cache_operation_observation_v31`, `publish_cache_resource_v31`, `finish_cache_operation_v31`, `invalidate_cache_resource_v31`).
2. Deploy the same 3.2.0 build to API and Worker.
3. Confirm `/health` reports `3.2.0` on both sides and that their control-plane revision/hash match.
4. Create a **new Gemini Session** to opt into the new physical layout. Sessions created before 3.2.0 keep the 3.1 layout because they do not carry the frozen `_relay_gemini_projection` marker.
5. Start with controlled `requested_cache_mode=on` smoke traffic, then `auto` after validating token thresholds and cache usage.

## Observability expectations

New useful events include:

- `gemini_physical_cache_plan_frozen`
- `cache_measurement_provider_http_error`
- `cache_create_provider_http_error`
- `cache_create_exception_no_recreate`
- `cache_create_verify_provider_http_error`
- `cache_handle_unavailable_no_recreate`
- `cache_create_not_claimed_no_recreate`

The physical-plan event logs only versions/hashes/counting decisions; it does not log frozen file URIs or full provider payloads. Provider HTTP error evidence is sanitized before logging and before writing the operation ledger.

## Compatibility boundary

3.2.0 changes Gemini's provider-facing physical projection, not Relay's canonical Session / Material / History model. Existing Sessions continue using the legacy projection; new Sessions freeze the new layout. Do not backfill the new layout metadata into existing Sessions in place.
