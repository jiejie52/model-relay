# Model Relay 3.2.0 Validation Note

## Automated validation

Run from the repository root:

```bash
pytest -q
python -m compileall -q app tests
```

The 3.2.0 package validates the following new invariants in addition to the existing 3.1 suite:

1. New Gemini Sessions freeze `gemini-physical-cache-layout/2`.
2. Session Material is present in `cached_prefix`, while request/stage instruction and current input remain outside it.
3. Historical Session-Material occurrences are removed from the provider-facing dynamic suffix without rewriting canonical history.
4. Gemini CacheSpec uses the exact physical `cached_prefix` and selects `measurement_order=before_lookup`.
5. New-layout Stateful Resource preparation performs Provider token measurement before cache resource lookup.
6. Stateful `generateContent` sends the exact `uncached_suffix` plus only the cache handle; finalized None sends the complete uncached context.
7. ProviderHTTPError evidence is sanitized and request IDs are pseudonymized before ledger/log persistence.
8. Missing cache handle becomes `unknown`, is logged, is not verified/published, and is not recreated.
9. Legacy 3.1 Session projection tests remain green.

## Manual smoke checks

For a newly created Gemini conversation Session containing a Session Material:

- inspect the `gemini_physical_cache_plan_frozen` log and correlate only its hashes/version fields;
- confirm `countTokens` contains the Session Material provider part and excludes the current question;
- when the prefix is below minimum, confirm the model request contains the complete uncached context and no `cachedContent`;
- when cache creation succeeds, confirm CachedContent create contains the same stable material and generateContent contains `cachedContent` plus the dynamic suffix only;
- force a cache-create 5xx body containing a dummy bearer/API key and confirm both structured logs and `relay_cache_operations.raw_result` contain redacted values, not the dummy secret;
- force a create response with no `name`/handle and confirm the operation ends `unknown` with no second create POST.
