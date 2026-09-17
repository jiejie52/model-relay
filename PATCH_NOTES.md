# Patch Notes — 0.3.0-session-core-kimi

## Added

- `/v2/sessions` provider-neutral Session API and `relay-envelope/2.0`.
- Official Moonshot/Kimi Chat Completions adapter and native history codec.
- Raw Provider Error contract and authenticated raw-error byte endpoint.
- Owner-scoped request fingerprints for idempotency replay validation.
- Session append reservation, Lease fencing and atomic history+job success commit.
- `execution_engine` routing for Core vs legacy Fusion workers.
- Migration `sql/003_core_v2_kimi.sql`.
- Compatibility package under `app/compat/` and legacy Fusion runtime under `app/application/`.
- Kimi smoke test and migration/operation docs.

## Changed

- Relay Core no longer depends on Fusion stage execution.
- Provider Registry rejects unknown providers instead of silently returning one adapter.
- AIHubMix credential is optional for Kimi-only deployments.
- Provider errors are no longer normalized to `UPSTREAM_*` codes or truncated summaries.
- Structured Output supports Chat Completions `response_format` projection in addition to existing Responses projection.

## Compatibility

- `/v1/jobs` and `/v1/dify/relay` remain available.
- Existing `job_id` values are preserved by the migration.
- Existing logic-job recovery semantics remain `job_id -> status/result`; no execute is introduced into resume.

## Known limitation

The package intentionally does not implement a generic Kimi file-upload/file-extract preparation pipeline. Unsupported legacy/remote-media transports fail closed rather than silently degrading material fidelity.
