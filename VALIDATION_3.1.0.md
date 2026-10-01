# Model Relay 3.1.0 Validation Note

## Local validation performed

```text
Python compileall                              PASS
pytest                                        PASS (43 tests)
Gemini Cache CRUD/countTokens mock wire        PASS
Gemini cachedContent inference projection      PASS
Gemini cachedContentTokenCount observation     PASS
Exact verified vs generic candidate routing    PASS
```

## What these tests prove

- The 3.1 cache plan can remain frozen while final provider token measurement occurs before cache creation.
- CachedContent creation payload contains system instruction + committed history, not current input.
- A frozen resource binding removes the cached history prefix and system instruction from `generateContent`, adds `cachedContent`, and preserves the complete current user content for logical history.
- The adapter implements count/create/get/patch/delete REST shapes and validates relative handles.
- Cache create has its own dispatch fence: once an operation enters `running`, lease expiry becomes `unknown` and can never authorize a blind duplicate create.
- Provider create handles are persisted to the operation ledger before the verification GET/publish step.
- Final-threshold plans remain `pending` at Request acceptance and become `finalized` only when the fenced physical binding is installed.
- Cached token evidence is surfaced from Gemini `usageMetadata.cachedContentTokenCount`.

## Environment-dependent validation still required

This build environment does not contain the production Supabase database or the production AIHubMix account/key. Therefore the following must be run in staging before broad rollout:

1. Execute SQL 006 against the actual Postgres/Supabase instance and verify PostgREST sees all v3.1 RPC signatures.
2. Run real AIHubMix `countTokens -> cachedContents.create -> get -> generateContent(cachedContent) -> delete` against each published exact Model Supply/account.
3. Inject worker loss around cache create and around the model dispatch seal to validate `unknown` cache-operation behavior and Request fencing.
4. Verify provider billing/usage semantics for the account actually used in production.

Do not interpret unit/mock success as proof that a particular paid upstream account has CachedContent enabled; the exact production channel/account remains part of the certified Supply boundary.
