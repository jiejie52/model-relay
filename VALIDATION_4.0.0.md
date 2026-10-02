# Model Relay 4.0.0 Validation Note

Validation performed against the packaged source tree:

```text
python -m compileall -q app tests
pytest -q
```

Result at packaging time:

```text
56 passed
```

New 4.0.0 regression coverage includes:

1. Gemini small materials still choose Files API first.
2. Files-failure fallback with total bytes `<70 MiB` selects all failed Session-stable materials for cache `inlineData` projection.
3. Files-failure fallback with total bytes `>=70 MiB` selects a deterministic small-file subset and keeps cumulative raw inline bytes strictly below 70 MiB.
4. Exact `70 MiB` aggregate enters the `>=70 MiB` small-subset branch.
5. Hybrid physical layout keeps selected small material in `CachedContent`, excludes Supabase URLs from `CachedContent`, and sends remaining large External URL materials only in the inference suffix.
6. Existing 3.2.0 cache/control tests remain green.

This is unit/contract validation. It does not claim live AIHubMix/Gemini/Supabase network E2E validation.
