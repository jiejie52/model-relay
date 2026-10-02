# Model Relay 4.1.0 Deployment

## Database

4.1.0 adds no SQL migration. Gemini stateful-cache deployments must already include:

```text
sql/005_relay_cache_control.sql
sql/006_gemini_stateful_cache.sql
```

## Control plane

Deploy API and Worker from the same 4.1.0 build and use:

```text
MODEL_CONTROL_PLANE_REVISION=relay-model-control-plane/2026-10-02.1
```

New Gemini Sessions freeze the 4.1 cache profile and `gemini-physical-cache-layout/4`. Existing Sessions keep the route/profile/layout already frozen into them. Therefore create a **new Gemini Session** to exercise the no-`countTokens` flow.

## Material settings

4.0.0 Files-first behavior remains active. Keep the configured 70 MiB fallback limit unless intentionally changed:

```text
GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES=73400320
```

`GEMINI_FILES_THRESHOLD_BYTES` remains configuration compatibility only; it does not select the primary Gemini upload transport.

## Expected current flow

For a new 4.1 Session with stateful caching selected:

```text
1. Prepare material bindings
2. Gemini Files API upload first
3. Freeze Gemini physical cache layout/4
4. Look up a compatible ready CachedContent resource
5. If none exists, acquire/fence cache-create operation
6. POST /v1beta/cachedContents directly
   - no pre-create :countTokens request
7. Persist returned cache handle immediately
8. GET the handle as Relay's post-create verification safeguard
9. Publish/install CacheExecutionBinding
10. Seal model dispatch
11. POST ...:generateContent with cachedContent=<handle>
    plus only the frozen uncached suffix
```

The post-create GET is a Relay reliability safeguard; it is not a token-measurement step and it never authorizes a second create.

## Failure behavior

- Provider rejects `cachedContents.create` deterministically (for example an explicit minimum-context rejection): cache create is recorded as failed. `auto` follows the existing `uncached_same_context` policy; `on` follows its fail-closed contract.
- Provider create may have happened but Relay cannot determine the result: operation becomes `unknown`; Relay does not issue another create automatically.
- Once model `dispatch_started` is sealed, cache failures never cause a second model inference with a different cache/transport choice.

## Rollout checks

1. API `/health` and Worker version both report `4.1.0`.
2. New Session metadata shows `gemini-physical-cache-layout/4`.
3. Cache plan shows `measurement_order=provider_create`.
4. Logs contain no `gemini_cache_count_tokens` event for a new layout/4 cache creation.
5. `gemini_cache_create` occurs directly after Files/material preparation when no reusable cache exists.
6. Subsequent inference carries `cachedContent=<cachedContents/...>` and does not resend cached Session-stable material.
7. Provider usage may report `cachedContentTokenCount`; that remains the primary hit evidence.

Do not log Signed URLs, cache handles in unrestricted logs, inline base64 payloads, or API credentials.
