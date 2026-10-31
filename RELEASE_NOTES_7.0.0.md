# Model Relay 7.0.0

## Baseline

Built from the uploaded `model-relay-6.2.0-source-final.zip` as the sole source baseline. The 4.3.2 architecture document and V5 change plan were used as design references, not as code parents.

## DOCX explicit-cache priority

- New Cache Contract v3 Requests use `relay-cache-resolver/7`.
- A stable Session DOCX becomes an explicit-resource priority candidate only when the concrete Model Supply and Protocol Profile have verified `stateful_resource` support.
- An exact-compatible explicit resource is reused first. Otherwise DOCX skips the future-reuse, historical-use, implicit-hit-rate and explicit-versus-implicit ROI calculation and proceeds to create a resource.
- A separately configured `max_new_resource_cost` hard cap remains mandatory. If the cap is configured, the Relay measures the prefix and checks frozen creation and storage prices before creating a new resource. Missing evidence fails closed.
- New Gemini Sessions freeze `gemini-physical-cache-layout/10`. Within the existing 70 MiB inline cache ceiling, stable DOCX materials are selected before other materials. Materials excluded by the byte ceiling remain in the full inference context.
- Request-only DOCX that shares one Gemini User Content with dynamic request text is not split into a separate cached turn. A single DOCX larger than the inline cache limit is not forced into CachedContent.

## Explicit / implicit dual-path governance

- Explicit resource lifecycle and Provider implicit prefix behavior are separate decisions.
- A frozen, certified implicit candidate can be used before first model dispatch when explicit preparation is skipped for economics, the explicit projection is ineligible, or an explicit preparation failure is known to be definitive.
- `cache_create_unknown`, cache verification failure, missing handles, operation-claim ambiguity and fencing failures do not trigger a mechanism switch or a second resource creation attempt. Those side effects must be reconciled.
- The fallback remains limited to the accepted CacheIntentPlan and to `requested_cache_mode=auto`. No mechanism switching occurs after `dispatch_started`.
- Both the Supply contract and Protocol Profile must explicitly declare `implicit_prefix`; a missing profile declaration is not treated as certification.

## Cache identity diagnostics

- Context plans expose a Session-stable material fingerprint separately from the complete request-stable prefix.
- Gemini physical plans expose the actual wire-projected request/cached system-instruction fingerprints, stable material projection fingerprint, identity component hashes and complete cache identity fingerprint.
- Runtime diagnostics now compare like-for-like wire projections. They no longer compare a hash of the raw instruction string with a hash of the projected `systemInstruction` object.
- Material identity is diagnostic evidence, not a substitute for the complete resource reuse key. Different system instructions, models, semantic dependencies, history prefixes or layouts remain distinct cache identities.
- No business prompt, message role, stage responsibility or system-instruction ordering was changed to manufacture cross-stage cache hits.

## Provider cache observation

The existing `cache_usage` JSONB now also reports:

- `explicit_cache_action` and `explicit_cache_skip_reason`
- `implicit_prefix_capability`, `implicit_prefix_eligible`, `implicit_prefix_attempted`
- `cache_resource_create_tokens` and its evidence source when returned by CachedContent creation
- material, instruction and full cache identity fingerprints
- the Provider-reported `actual_cache_hit_status`, preserving `unknown` when usage evidence is absent

Gemini `cache_write_tokens` is not fabricated from `countTokens` or `totalTokenCount`; the create-token evidence is exposed separately.

## Compatibility and release boundaries

- Existing resolver/3–/6 Request plans keep their frozen behavior. The expanded pre-dispatch explicit-failure fallback applies only to resolver/7 plans.
- Existing Gemini layout/3–/9 Sessions keep their frozen wire and material projection semantics. New Sessions use layout/10.
- No SQL migration was added. No new test files were generated. One existing assertion was updated to expect the new frozen resolver version; no new test cases or test files were added.
- The built-in AIHubMix Gemini contract/profile is not marked as implicitly cached. Account/channel end-to-end certification and a governed configuration release are still required before the implicit fallback can activate.

## Version

`7.0.0` is used because DOCX explicit-cache priority is a new modification requirement under the repository's `XX.YY.ZZ` version rule. API and Worker versions are both `7.0.0`.
