# Model Relay 4.0.0 Deployment

## Database

No new SQL migration is introduced by 4.0.0. Deployments using Gemini stateful cache must already have:

```text
sql/005_relay_cache_control.sql
sql/006_gemini_stateful_cache.sql
```

## Environment

New setting:

```text
GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES=73400320
```

`GEMINI_FILES_THRESHOLD_BYTES` is retained for configuration compatibility but no longer selects Gemini ingress transport.

## Rollout

1. Deploy the same 4.0.0 build to API and Worker.
2. Confirm `/health` reports `4.0.0` on both processes.
3. Create new Gemini Sessions to opt into `gemini-physical-cache-layout/3`. Existing Sessions keep their frozen earlier physical layout.
4. Verify a normal upload first attempts Gemini Files API.
5. For a forced Files upload failure, verify the material becomes `gemini_external_url` for inference fallback and is marked as an inline-cache candidate.
6. With `requested_cache_mode=on`, verify `CachedContent.create` receives either Gemini Files URI parts or selected `inlineData`, never a Supabase External URL.
7. For aggregate material size `>=70 MiB`, verify unselected large fallback files appear in `uncached_suffix` and are absent from the cache-create payload.

## Operational logs

Relevant 4.0.0 events include:

```text
gemini_files_fallback_external_url_activated
gemini_files_preferred_binding_failed
gemini_cache_material_projection_prepared
gemini_physical_cache_plan_frozen
```

Do not log Signed URLs, provider handles, or inline base64 payloads.
