# Model Relay 0.5.6 Deployment

## Scope

0.5.6 is a code-only hotfix over 0.5.5. It corrects Gemini 3.5/3.6 canonical temperature capability handling.

## Deploy

1. Build and deploy the same source/image to Relay API and Worker.
2. Keep the existing environment variables and secrets unchanged.
3. No SQL migration is required.
4. Parent / Analyze / Finalize should keep using provider-neutral `options`; do not add model-specific capability tables to DSL.
5. After deployment, verify `/health` reports `0.5.6-gemini-temperature-capability-hotfix`.
6. Re-run the Axis Discovery request. For `gemini-3.5-flash-lite`, `options.temperature=0` should pass Relay capability validation and be projected by Gemini Native Adapter to `generationConfig.temperature`.

## Compatibility

- `CAPABILITY_PROFILE_REVISION` remains `relay-model-options/2026-09-23.1`, so existing 0.5.5 Sessions are not forced to recreate solely for this permissive bugfix.
- v2.1 Request resume compatibility is unchanged.
- Request schema remains `relay-request/2.2`.
- `provider_payload` remains deprecated/migration-only.

## Verification performed

- `python -m compileall app`: PASS
- API import: PASS
- Worker import: PASS
- `pytest -q`: 83 passed, 4 subtests passed
- Gemini 3.5/3.6 canonical temperature smoke: PASS

This verification is local/unit-level; it does not claim a live AIHubMix/Gemini network call was executed from this build environment.
