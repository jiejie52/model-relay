# V20.25.23 Manual Re-entry Resume

This package intentionally keeps the Relay runtime code at the original user-provided baseline.
No Railway -> Dify callback dispatcher is included or required.

Manual resume contract:

- `POST /v1/dify/relay` `operation=execute` remains idempotent through the existing `Idempotency-Key` contract.
- Re-submitting the same Fusion model-stage request returns the existing Relay Job rather than creating a second model inference Job.
- `operation=status` and `operation=result` remain the only read paths.
- The Dify DSL handles the one special cross-Workflow case, `fusion_corpus_ingest`, by persisting the pending Job ID and later calling `operation=result` when the user repeats `/fusion analyze`.
- No `DIFY_CALLBACK_*` Railway variables are used.
- No callback SQL migration is required.

Database baseline remains `sql/001_relay_schema.sql` followed by `sql/002_fusion_runtime.sql`.
