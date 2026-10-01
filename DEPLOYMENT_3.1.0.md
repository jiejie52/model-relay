# Model Relay 3.1.0 - Gemini Stateful Cache Deployment

## 1. Version semantics

This is a processing-method enhancement of the 3.0.0 cache requirement, so the release is `3.1.0`.

## 2. Database order

For an existing 3.0.0 database, apply:

```text
sql/006_gemini_stateful_cache.sql
```

For a fresh database, apply `001` through `006` in numeric order.

After applying SQL on Supabase/PostgREST, reload schema if your environment requires it:

```sql
NOTIFY pgrst, 'reload schema';
```

Do not deploy 3.1.0 API/Worker before SQL 006 is visible through PostgREST. The runtime calls `start_cache_operation_v31`, `record_cache_operation_observation_v31`, `publish_cache_resource_v31`, `finish_cache_operation_v31`, and `invalidate_cache_resource_v31`.

## 3. Required provider configuration

The existing Gemini Native connection remains authoritative:

```text
AIHUBMIX_API_KEY=...
AIHUBMIX_GEMINI_BASE_URL=https://aihubmix.com/gemini
AIHUBMIX_GEMINI_CONNECTION_ID=aihubmix_gemini_native
MODEL_CONTROL_PLANE_REVISION=relay-model-control-plane/2026-10-01.1
CACHE_PREPARE_TIMEOUT_SECONDS=60
CACHE_EXPIRY_SAFETY_SECONDS=30
```

`AIHUBMIX_GEMINI_BASE_URL` must be the same native Gemini proxy base used by the inference adapter. The cache resource adapter derives `v1beta/models/*:countTokens` and `v1beta/cachedContents` from it.

## 4. Published cache-enabled supplies

Built-in 3.1.0 publishes Stateful Cache only for the exact supplies:

```text
gemini-3.1-flash-lite
gemini-3.8-flash
```

The generic `gemini-*` fallback remains `candidate`. Do not change the generic contract to verified just because a model speaks Gemini Native protocol.

Existing Sessions retain their previously frozen RouteBinding/Capability/Cache contracts. Create a **new Session** after deploying 3.1.0 to use the new exact verified supply.

## 5. Expected execution sequence

For a cache-aware Gemini Request:

```text
frozen Request/ContextPlan
  -> Material Binding freeze
  -> build provider cache prefix (system + committed history)
  -> find compatible ready prefix resource
       -> cachedContents.get verify
       -> reuse
     or
       -> countTokens
       -> below threshold: effective=None, full normal inference
       -> create operation singleflight (leased)
       -> cache-operation dispatch fence (running)
       -> cachedContents.create
       -> persist returned handle to operation ledger
       -> cachedContents.get verify
       -> publish ready resource
  -> install CacheExecutionBinding
  -> atomic seal_cache_and_dispatch_v3
  -> generateContent(cachedContent=<handle>, contents=<suffix + current>)
  -> usageMetadata.cachedContentTokenCount
  -> result_stored / commit
```

Current request input/materials are not put into CachedContent. They remain the dynamic suffix. Once the request commits, that complete logical turn becomes eligible to be part of a future committed-history cache prefix.

## 6. Smoke test

During rollout, use `requested_cache_mode=on` so capability/prepare failures are visible instead of auto-falling back.

A typical conversation test should be at least two to three turns:

1. First turn establishes substantial committed history.
2. Second turn can create a CachedContent resource from turn 1 and reference it for inference.
3. Third turn should reuse the same resource generation when the committed prefix still matches, sending turn 2 + current input as suffix.

Inspect the Request result `cache` object. A provider-confirmed hit is evidenced by non-zero `cache_read_tokens`, sourced from Gemini `cachedContentTokenCount`.

## 7. Failure rules

- `countTokens` below the minimum: no cache resource, normal inference. This is not an error, including when requested mode is `on`.
- `auto` + clear cache prepare failure: may execute the pre-authorized uncached same logical context before model dispatch.
- `on` + cache prepare failure: fail before model dispatch.
- Once a create operation is `running`, Provider side effect is possible. Transport/5xx, post-create verification failure, or expired running lease becomes `unknown`; the same create must not be re-issued blindly.
- After `seal_cache_and_dispatch_v3` / `dispatch_started`, cache 404/expiry/429/provider errors **must not** trigger a second uncached model inference.

## 8. Rollback

Application rollback to 3.0.0 is possible only if no 3.1-only Requests need to execute on the old worker. SQL 006 is additive and can remain installed. Disable creation of new 3.1 Sessions before rolling application code back.
