# Model Relay 3.0.0 Validation Note

## Local validation completed

Environment used only placeholder Relay/Supabase values; no real Provider or database network calls were performed.

```text
python -m compileall -q app                    PASS
import app.api / app.worker                    PASS
API_VERSION / WORKER_VERSION                   3.0.0 / 3.0.0
pytest -q                                      32 passed
```

The tests cover the pre-existing local archive tests plus the 3.0 cache-control contract, request identity compatibility, off/auto/on decisions, Grok compatibility gate, cache usage normalization, cache operation singleflight identity, expired resource reuse protection, and static SQL contract presence.

## Validation still required before production

1. Apply `sql/005_relay_cache_control.sql` to a staging PostgreSQL/Supabase database and execute RPC-level concurrency/fencing tests. This environment has no PostgreSQL server, so the migration was not executed here.
2. Run real Provider certification separately for each exact Model Supply/channel/account/profile before changing cache `verification_status` from `candidate` to `verified`/`published`.
3. In particular, validate Gemini cachedContents CRUD/reference/expiry/delete, Claude block cache_control placement/usage, and any GPT/compatible prompt cache hint against the exact production route.
4. Run failure injection around cache create unknown, pre-dispatch seal, stale async lease, sync executor timeout, result_stored recovery and cancellation/GC competition.
5. Capture golden outbound payloads for Grok 2.1/2.2 and 3.0 on/auto/off before rollout.

## Known first-version boundary

`prepared_payload_hash` currently seals the frozen pre-dispatch execution projection (Request snapshot + history + Material Binding + Cache Binding), not the final byte-for-byte Provider-native HTTP JSON. Splitting each Adapter into a native builder plus `execute_prepared()` is intentionally left for a processing-method enhancement version rather than weakening the 3.0.0 dispatch gate.
