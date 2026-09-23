# Model Relay 0.5.7 Deployment

## Scope

0.5.7 is a code-only capability release over 0.5.6. It adds provider-neutral Kimi K3 thinking-effort support for `low`, `high`, and `max` and projects the selected level to the official Moonshot/Kimi Chat Completions top-level `reasoning_effort` field.

## Deploy

1. Build and deploy the same source/image to Relay API and Worker.
2. Keep the existing environment variables and secrets unchanged.
3. No SQL migration is required.
4. Parent / Analyze / Finalize should continue sending canonical `think_level`; do not send `reasoning_effort` through `provider_payload`.
5. After deployment, verify `/health` reports `0.5.7-kimi-reasoning-effort` and capability revision `relay-model-options/2026-09-23.2`.
6. Create a new Kimi Session after deployment. Existing Sessions freeze the previous capability revision and intentionally return `CAPABILITY_PROFILE_CHANGED_RECREATE_SESSION` for new Requests.
7. For `provider=kimi` + `model=kimi-k3*`, verify `think_level=low`, `high`, and `max` all pass Relay capability validation. `auto` remains supported and omits `reasoning_effort`, leaving the provider/model default in effect.

## Compatibility and model boundary

- Kimi K3 supports canonical `auto/low/high/max` in Relay. `low/high/max` map 1:1 to top-level `reasoning_effort`.
- Other `kimi-*` models remain `auto`-only unless they have a separately verified upstream effort contract.
- Kimi K2.7 Code remains `auto`-only: it is always-thinking but does not expose the same `low/high/max` effort control on the official model contract.
- `CAPABILITY_PROFILE_REVISION` is bumped to `relay-model-options/2026-09-23.2` because this release changes frozen model-option semantics.
- Request schema remains `relay-request/2.2`.
- `provider_payload` remains deprecated/migration-only; native `reasoning_effort` and `thinking` cannot override Relay's frozen canonical semantics.
- No database migration is required.

## Verification performed

- `python -m compileall app`: PASS
- API import: PASS
- Worker import: PASS
- Kimi capability smoke for `auto/low/high/max`: PASS
- Moonshot adapter wire projection smoke: PASS
- The deploy-runtime baseline does not bundle the historical pytest suite; release verification therefore uses compile/import plus focused capability/wire smoke checks.

This verification is local/unit-level; it does not claim a live Moonshot/Kimi network call was executed from this build environment.
