# Model Relay 3.1.0 Change Summary

## Version decision

`3.0.0 -> 3.1.0`. The cache requirement already exists in 3.0.0; this release completes and strengthens the Gemini Stateful Cache processing method, so YY increments and ZZ resets to zero.

## Gemini Stateful Cache

- Added `app/cache/providers/gemini_aihubmix.py` with `countTokens`, CachedContent create/get/renew/delete and strict relative-handle validation.
- Added `app/providers/gemini_wire.py` as the shared semantic projector for system instruction, committed Gemini history and current user/material content.
- Added exact verified cache-enabled supplies for `gemini-3.1-flash-lite` and `gemini-3.8-flash`. Generic `gemini-*` remains candidate.
- Added provider-side final token threshold guard before any cache create side effect.
- Added compatible-prefix reuse: a resource created from committed history prefix N remains usable after history grows, provided the exact N-turn prefix fingerprint still matches.
- Changed `GeminiNativeAdapter` to consume frozen Stateful bindings and send `cachedContent` plus only the uncached suffix. It never duplicates the cached prefix or system instruction on the inference wire.
- Preserved complete logical transport history in Relay so expired/off cache execution can reconstruct full context.
- Surfaces `usageMetadata.cachedContentTokenCount` as cached token evidence.
- API and Worker now register the same Gemini cache resource adapter.

## Reliability / database

- Added `sql/006_gemini_stateful_cache.sql`.
- Added resource reuse identity: `reuse_key`, `prefix_version`, `token_count`, `spec_hash`.
- Added fenced `start_cache_operation_v31`, `record_cache_operation_observation_v31`, `publish_cache_resource_v31`, `finish_cache_operation_v31`, and `invalidate_cache_resource_v31` RPCs.
- Tightened cache-operation claim semantics: `leased` is pre-dispatch, `running` means Provider create may have been sent, expired `running` becomes `unknown`, and only known-no-side-effect `failed` operations may retry.
- Provider create success is recorded immediately in the operation ledger, then verified with `cachedContents.get` before the resource becomes `ready` in Relay.
- Final provider-count guards now remain `pending` during Request acceptance and are marked `finalized` by the fenced binding installation.
- `cachedContents/<id>` is the only accepted provider handle form; caller-injected native `cachedContent` is reserved/ignored on legacy provider-payload projection.

## Preserved invariants

- Request identity, Session RouteBinding, Material canonical identity, and `dispatch_started -> no speculative inference retry` remain unchanged.
- `requested_cache_mode=on` may still resolve to no physical cache when the provider-counted prefix is below the model minimum.
- Cache resource side effects remain separate from model inference dispatch state.
