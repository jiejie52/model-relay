# Model Relay 4.0.0 Change Summary

## Version decision

`3.2.0 -> 4.0.0`.

This release implements a new material/cache handling requirement, so it increments **XX** under the project `XX.YY.ZZ` rule. `YY` and `ZZ` reset to zero.

## Gemini Files-first material policy

- Gemini `inference_input` now attempts the configured Gemini Files API first for every file. The legacy 99 MiB selector is retained only as a compatibility setting and no longer chooses ingress transport.
- A successful Files binding remains `gemini_file_uri`. Session-stable Files URIs are eligible for `CachedContent` and are referenced there directly.
- If a Files upload fails, Relay stores the original bytes in the existing fallback store and creates a Supabase Signed External URL **only as an inference fallback**. The binding is marked `files_api_fallback=true` so a normal request does not repeat the provider-file upload side effect on every turn.

## 70 MiB cache fallback policy

The exact frozen Request material set is measured by Relay. The default raw-byte fallback limit is `GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES=73400320`.

- If total material bytes are `< 70 MiB`, every Session-stable material that fell back from Files API to External URL is projected once into `CachedContent` as Gemini `inlineData`.
- If total material bytes are `>= 70 MiB`, failed Session-stable materials are sorted deterministically by `(size_bytes, material_id)`. Relay selects the smallest subset whose cumulative raw bytes stays **strictly below 70 MiB** and projects only that subset into `CachedContent` as `inlineData`.
- Remaining failed Session-stable materials stay `gemini_external_url` and are sent only in the uncached inference suffix. They are not included in `CachedContent`.
- Session materials with a successful `gemini_file_uri` are still cache-eligible regardless of the inline fallback budget because their raw bytes are not embedded in the cache-create JSON.

## Physical projection / recovery boundaries

- New Gemini Sessions freeze `gemini-physical-cache-layout/3` and `gemini-physical-projector/3`.
- Layout/3 separates the frozen inference binding snapshot from the cache-only material projection. Canonical Material identity is unchanged.
- `cached_prefix` may contain only provider-owned Gemini Files URIs and selected `inlineData`; Supabase External URLs are excluded from `CachedContent`.
- `uncached_suffix` carries dynamic history/input plus Session-stable inference-only External URLs. With a cache handle, cached Session materials are not resent.
- `full_uncached_payload` still contains all inference bindings so `off`, below-threshold, or pre-dispatch cache degradation preserves complete model semantics.
- Existing layout/2 Sessions are not silently rewritten and retain their frozen projection.
- No new SQL migration is required. Existing `005` and `006` cache schemas remain in use.

## Component versions

- API / Worker: `4.0.0`
- Gemini inference adapter: `gemini-native-aihubmix/5`
- Gemini cache resource adapter: `gemini-cache-aihubmix/3`
- Gemini physical layout: `gemini-physical-cache-layout/3`
