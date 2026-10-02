# Model Relay 4.1.0 Change Summary

## Version decision

`4.0.0 -> 4.1.0`.

This release corrects/enhances the Gemini explicit-cache handling method introduced in 4.0.0. Under the project `XX.YY.ZZ` rule this increments **YY** and resets `ZZ` to zero.

## AIHubMix / Gemini explicit cache flow

New Gemini Sessions now freeze `gemini-physical-cache-layout/4`. For this layout Relay follows the provider flow used by the AIHubMix/Google GenAI cache examples:

```text
Material
  -> Gemini Files API first
  -> cachedContents.create (caches.create semantics)
  -> generateContent(cachedContent=<cache.name>)
```

The current layout no longer calls `models.countTokens` before cache creation. Cache creation itself is the Provider-authoritative gate for whether the exact prefix can be cached.

### Why the SDK package is not embedded in Relay

The Provider call semantics and Native JSON wire are aligned with the official SDK flow, but Relay continues to issue the Native REST calls from its own Adapter instead of delegating cache lifecycle calls to `google-genai`. This preserves Relay's explicit cache-operation ledger, lease/fencing, Provider request-id capture, sanitized error observation and ambiguous-side-effect handling. It also prevents an SDK-owned retry policy from silently replaying a `cachedContents.create` side effect.

## Threshold handling

- Removed the current Gemini contracts' hard-coded `minimum_cacheable_tokens=1024` pre-create guard.
- Current stateful-cache contracts use `threshold_mode=provider_create` and `precreate_measurement=none`.
- A successful `cachedContents.create` is authoritative evidence that the Provider accepted the cache prefix.
- If create returns `usageMetadata.totalTokenCount`, Relay records it on the cache resource; absence of that field does not trigger `countTokens`.
- A deterministic Provider 4xx during cache creation is handled by the existing cache-prepare policy (`auto` may continue uncached in the same model context; `on` follows its fail-closed contract). An ambiguous create failure remains `unknown` and does not authorize blind recreation.

## Files-first and 70 MiB behavior retained

4.0.0 material transport behavior is unchanged:

- every Gemini material first attempts Gemini Files API;
- successful Provider file URIs are eligible for `CachedContent`;
- Files upload failure falls back to Supabase only for inference transport;
- if total material bytes are `<70 MiB`, failed Session-stable files can be injected into cache as `inlineData`;
- if total is `>=70 MiB`, a deterministic small-file subset with cumulative raw bytes strictly `<70 MiB` is cache-injected and remaining larger files stay inference-only External URLs;
- Supabase External URLs never enter `CachedContent`.

## Compatibility and frozen semantics

- New Sessions: `gemini-physical-cache-layout/4`, `gemini-physical-projector/4`.
- Existing 4.0.0 layout/3 Sessions are **not** silently rewritten. Their frozen path still uses the legacy `countTokens` preflight.
- Existing layout/2 and older Sessions retain their prior projection semantics.
- Gemini protocol profile revision: `relay-protocol-profile/gemini-aihubmix/2026-10-02.1`.
- Gemini cache resource adapter: `gemini-cache-aihubmix/4`.
- API / Worker: `4.1.0`.
- Built-in control-plane revision: `relay-model-control-plane/2026-10-02.1`.
- No new SQL migration is required.
