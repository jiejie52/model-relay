# Model Relay 3.2.0 Change Summary

## Version decision

`3.1.0 -> 3.2.0`. The public cache requirement is unchanged, but Gemini's provider-facing physical layout changes: Session-stable Material is now projected into CachedContent rather than being resent in the dynamic user message. This is a processing/layout-version change and therefore increments YY.

## Gemini physical cache projection

- Added `app/providers/gemini_physical.py` and a frozen per-Session Gemini projection/layout version.
- New Gemini Sessions project one `GeminiPhysicalCachePlan` from canonical Session / Material / History after Material Binding is frozen.
- `cached_prefix` contains Session-stable Material using the frozen Gemini provider representation. Request/stage instruction, projected dynamic history and current input remain in `uncached_suffix`.
- Canonical Relay history is not rewritten. New Gemini history entries record Material occurrence metadata so later physical projection can remove only Session-stable occurrences from the dynamic wire suffix.
- `countTokens`, CacheSpec fingerprinting, CachedContent create and generateContent now consume the same frozen physical plan. New-layout Sessions run `countTokens(exact cached_prefix)` before resource lookup/create.
- Below Provider minimum (or any finalized `None` cache resolution), generateContent receives `full_uncached_payload`; it does not lose Session Material or logical history.
- Stateful execution sends `cachedContent=<handle>` plus the exact frozen `uncached_suffix`; Session Material is not duplicated after it has moved into CachedContent.
- Existing pre-3.2 Sessions do not receive the new layout marker and keep the 3.1 committed-history cache projection.

## Cache error observability / no-recreate behavior

- Added `app/core/provider_error_observation.py`.
- Cache `ProviderHTTPError.body`, `request_id`, `status` and `phase` are sanitized before structured logging and before error evidence is written to the cache operation ledger. Provider request IDs are pseudonymized; credential-like JSON fields, bearer tokens and common API-key patterns are redacted.
- Cache-create and post-create verification failures retain sanitized diagnostic evidence in `relay_cache_operations.raw_result`.
- A create response without a usable cache handle is finalized as `unknown`, emits `cache_handle_unavailable_no_recreate`, and does not perform GET/publish/re-create.
- Create exceptions and `create_unknown` claim conflicts emit explicit `recreate_attempted=false` observability. This release intentionally does **not** add reconciliation or automatic CachedContent recreation.
- Model-inference `ProviderHTTPError` structured logs now include the same sanitized body/status/phase/request-id observation instead of logging the raw upstream request ID.

## Compatibility / database

- No new SQL migration. `sql/006_gemini_stateful_cache.sql` remains required.
- The existing cache-operation state machine continues to make `unknown` non-reclaimable for create; 3.2.0 does not change that database contract.
- API/Worker version is `3.2.0`; Gemini inference adapter is `gemini-native-aihubmix/4`; Gemini cache resource adapter is `gemini-cache-aihubmix/2`.
