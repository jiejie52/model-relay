# Model Relay 3.0.0 Change Summary

## Version decision

`2.0.0 -> 3.0.0` because cache execution control is a new modification requirement (XX). Per version rules, YY and ZZ reset to zero.

## Primary changes

- Cache contract and policy frozen into Model Supply / RouteBinding.
- Request public cache intent: off / auto / on.
- Request identity contract `relay-request/2.3` plus caller-intent replay hash.
- Provider-neutral ContextPlan / CacheIntentPlan / CacheExecutionBinding.
- Three physical mechanisms: stateful_resource / breakpoint / implicit_prefix.
- SQL 005 with cache resource/operation/binding/pin/task and v3 execution fences.
- Atomic pre-dispatch seal; v3 result-store/commit/failure fencing.
- Grok backward-compatible cache gate; no key algorithm or reasoning/history rewrite.
- Unified cache usage normalization and read-only Request cache summary.
- Built-in unverified provider cache capabilities remain candidate until real Supply certification.

## Compatibility

2.1 / 2.2 Request identity and execution behavior remain version-dispatched. Unknown request minor versions fail closed. Old Session is not silently upgraded to the 3.0 cache contract.

## Validation and hardening in this archive

- Cache resource create singleflight key is scope/content/operation based, not Request based.
- Expired Stateful cache generations are not reused.
- `caller_intent_hash` includes legacy `provider_payload` migration input so same-key input changes conflict.
- Local validation: compile/import PASS; `pytest -q = 32 passed`.
- Staging PostgreSQL execution of SQL 005 and real Provider cache certification remain deployment gates.
