# Fusion Runtime deployment

1. In Supabase SQL Editor, run `sql/002_fusion_runtime.sql` after the existing `001_relay_schema.sql`.
2. Deploy this source to the existing Railway project. Redeploy both services:
   - `relay-api`: existing uvicorn command / Dockerfile default.
   - `relay-worker`: `python -m app.worker`.
3. No new secret is required. Keep the existing AIHubMix, Supabase and Relay variables.
4. Confirm `GET /health` returns version `0.2.0-fusion`.
5. Import `AI对话助手-Chatflow_V20260828_FusionCorpusRelay_V20.20.3_FusionRuntimeSync.yml` in Dify.
6. Re-run the small travel-expense Fusion test.

Expected first successful chain:

```text
fusion_corpus_ingest -> succeeded + fusion_corpus_id
DIRECT_GLOBAL_ADJUDICATION -> global_adjudication job
comparison schema/post-validation -> AWAITING_DECISION
```

If a Supabase table/migration is missing, Relay status now exposes `SUPABASE_ERROR` with the downstream HTTP body excerpt. If a Fusion model stage returns malformed JSON/schema, Relay returns `FUSION_OUTPUT_INVALID_JSON` or `FUSION_SCHEMA_INVALID` and archives the raw provider response in the Relay job storage path.

The normal `normal_inference` session contract is unchanged: `new_session / continue_session / stateless` still require `current_query`; only Fusion stages use the new `fusion_corpus_id + payload` contract.

## v0.2.1 schema-shape fix

After deployment, `/health` should report `0.2.1-fusion-schema`.
This release keeps the SQL schema unchanged from `002_fusion_runtime.sql`; no new migration is required.
It strengthens `global_adjudication` structured-output transport and bounded normalization for canonical array fields such as `material_alignment`.


## v0.2.2 generic structured-output passthrough

No SQL migration is required. Redeploy both `relay-api` and `relay-worker` from the same source revision.

After deployment, `/health` must report `0.2.2-structured-output-passthrough`.

The Relay now maps arbitrary caller JSON Schema to provider-native Responses `text.format.type=json_schema`; it does not inspect Dify business property names. Current Dify requests using `payload.output_schema_mode=strict_json_schema` and `payload.output_schema` are supported without changing the request shape.

When such a schema is present, legacy Railway-owned output-shape contracts and stage-specific output validators are bypassed, and only generic JSON-Schema validation is applied. If the upstream provider rejects the schema transport, the Job fails rather than silently weakening to `json_object`.

See `STRUCTURED_OUTPUT_PASSTHROUGH.md` for the exact request and provider mapping.
