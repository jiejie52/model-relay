# Model Relay 8.0.0

Baseline: `model-relay-7.0.0-source-final.zip`.

## Architecture

- Replaced the hard single-cache interpretation with `Primary Cache + Additional Cache Bindings`. The primary remains the default, not a uniqueness constraint.
- Kept mutually-exclusive `fallback_mechanisms` separate from concurrent additional bindings; each fallback freezes its own projection (defaulting to the full stable prefix) rather than inheriting a material-specific primary projection.
- Added frozen per-binding projections, mechanism configuration, required/optional semantics, and projection fingerprints.
- Added exact Provider composition gates: a mechanism combination must be declared by both the Supply Capability Contract and Protocol Profile, fit both binding limits, use mutually certified projection kinds, and include a Protocol Profile wire strategy.
- Added pre-seal Adapter validation. Adapters without a verified binding-set projection fail closed before `dispatch_started`.

## Projection and execution

- ContextPlan v2 records material filename, MIME type and byte size so cache policies can select stable material sets without reading or interpreting business content.
- Added `full_stable_prefix`, `stable_segments` and `material_set` projection selection. The projection preserves original segment order and only selects existing stable segments.
- Stateful cache preparation receives the frozen binding plan. Provider adapters may provide binding-specific physical projections through `cache_binding_projections[binding_id]`; unsupported projections are rejected or skipped according to the binding's required flag.
- Per-binding execution records include creation/reuse/attach state, resource identity, logical projection hash and physical wire projection hash.

## Persistence and observation

- Added `sql/007_multi_cache_bindings.sql` to validate and pin every active stateful resource in the frozen binding set atomically at Dispatch Seal. Legacy scalar binding rows continue to work.
- Kept the scalar `final_mechanism` and `resource_id` columns as Primary Cache compatibility aliases; the JSON `metadata.cache_bindings` array is authoritative for the full set.
- Added per-binding observation summaries. Aggregate Provider usage is not falsely attributed to individual bindings; binding-level hit status remains `unknown` unless the Provider reports binding-specific evidence.

## Compatibility and safety

- API and Worker versions are `8.0.0`.
- Cache resolver is `relay-cache-resolver/8`.
- Cache binding plan and execution set schemas are `relay-cache-binding-plan/2` and `relay-cache-binding-set/2`.
- Gemini physical layout/10 is unchanged. Built-in Gemini Native and Responses adapters do not claim multi-binding composition until a Provider-specific wire strategy is implemented and certified.
- Session isolation, Request idempotency, immutable accepted plans, resource-operation fencing, and the no-switch-after-`dispatch_started` boundary remain in force.
- No new test files were created. Existing tests are retained; the resolver-version assertion is updated for `/8`.

## Versioning

`8.0.0` is used because multi-binding cache planning and coordinated execution are a new modification requirement under the repository's `XX.YY.ZZ` version rule. API and Worker versions are both `8.0.0`.
