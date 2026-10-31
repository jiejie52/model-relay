# Model Relay 6.2.0

## Baseline

Built from the uploaded `model-relay-6.1.0-source-final.zip`. No 5.0.0 or 5.1.0 source was used as a substitute.

## Cache strategy

- New Cache Contract v3 Requests use `relay-cache-resolver/6`.
- Fixed relative prices: Cache Read is `0.1 × Input`; explicit resource creation is `1.0 × Input`; ordinary Input and implicit misses are `1.0 × Input`.
- Explicit creation is compared with implicit caching using measured same-Session usage, the frozen expected reuse count, TTL-bounded request cadence, and a safety margin.
- If implicit support is certified but has no measured hit rate yet, the strategy does not invent one; `auto` selects the implicit path until actual usage is available.
- A certified `implicit_prefix` fallback is frozen into the CacheIntentPlan when explicit is selected. An explicit economic or hard-budget denial can switch to that fallback before the first model dispatch.
- Implicit cache misses remain ordinary full-context Input requests; they do not trigger a second model call.
- Existing exact-compatible explicit resources are reused before any new-resource economics are evaluated.

## Preserved boundaries

- Session isolation remains mandatory; no cross-Session resource sharing was introduced.
- Request plans and fallback candidates are frozen at acceptance.
- The final CacheExecutionBinding is sealed before `dispatch_started`; no mechanism switching or automatic model resend is allowed after dispatch.
- Gemini physical layout/9 and prior layout semantics are unchanged.
- Previously accepted resolver/3, /4, and /5 Requests retain their frozen recovery semantics.
- No SQL migration is required; the existing `relay_requests.cache_usage` JSONB field carries the additional usage evidence.

## Deployment prerequisite

The built-in AIHubMix Gemini contract is deliberately **not** marked as supporting `implicit_prefix`. The fallback is enabled only after the specific account/channel has been verified end to end and both the frozen Supply contract and Protocol Profile declare `implicit_prefix` as supported. The implicit profile must include a positive verified minimum-token threshold and the required final threshold guard. This release adds the runtime decision and fallback path; it does not fabricate provider certification.

## Additional safety fix

Removed base64 copies of raw Provider HTTP response bodies from the cache-operation ledger. The ledger retains the redacted Provider error observation and pseudonymized request identifiers instead.

## Validation

Run the repository test suite with:

```bash
pytest -q
```

Validation result for this source package: `125 passed` (`pytest -q`), `compileall` passed, and `git diff --check` passed.
