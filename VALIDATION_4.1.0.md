# Model Relay 4.1.0 Validation Note

Validation targets the packaged source tree and the Relay-side contract/wire behavior. It does not claim live AIHubMix/Gemini/Supabase network E2E validation.

Commands:

```text
python -m compileall -q app tests
pytest -q
```

4.1.0 regression coverage includes:

1. New Gemini physical layout freezes `measurement_order=provider_create`.
2. Frozen 4.0.0 layout/3 still freezes `measurement_order=before_lookup` and keeps its legacy `countTokens` behavior.
3. The current cache resolver finalizes `stateful_resource` without a pre-create token threshold guard.
4. Stateful resource creation for layout/4 never invokes Adapter `measure()` / `countTokens`.
5. `cachedContents.create` success can supply `usageMetadata.totalTokenCount`, which is persisted as cache resource token metadata without another count request.
6. Cache-create fencing, immediate handle observation, post-create GET verification and publish order remain intact.
7. Gemini inference with a cache binding sends `cachedContent` plus the exact frozen uncached suffix and does not resend the cached Session-stable file.
8. Existing 4.0.0 Files-first and `<70 MiB / >=70 MiB` cache material fallback tests remain green.

The final test count and archive checksum are recorded at packaging time.

## Packaging-time result

```text
compileall: passed
pytest: 60 passed
```
