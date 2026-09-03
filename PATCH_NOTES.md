# Railway Generic Structured Output Passthrough Patch Notes

Release: `0.2.2-structured-output-passthrough`

## Scope

Only the Railway/model-relay source is changed. No Dify DSL and no `relay_job_tool` changes are included.

## Changed files

- `app/structured_output.py` — new provider-neutral schema resolver, mapping helpers, and generic JSON-Schema validator.
- `app/models.py` — adds optional top-level `structured_output` request field while preserving the existing generic `payload` form.
- `app/providers/openai_compatible.py` — maps caller schema to Responses `text.format` after provider payload merge.
- `app/providers/base.py` — adds generic `ProviderRequestError`.
- `app/worker.py` — reports provider request contract errors without turning them into generic worker failures.
- `app/fusion_runtime.py` — caller schema becomes authoritative; current Dify `payload.output_schema*` is recognized; legacy Railway output contracts/normalizers/validators are bypassed when a caller structured-output request exists; caller `instructions` is honored when supplied.
- `requirements.txt` — adds `jsonschema`.
- `app/api.py` — version bump only.
- `tests/test_structured_output.py` — generic passthrough and integration regression tests.
- `README.md`, `DEPLOYMENT_FUSION.md`, `STRUCTURED_OUTPUT_PASSTHROUGH.md` — deployment and contract documentation.

## Compatibility

- Existing requests with no structured-output request keep the old Fusion `json_object` + legacy validation path.
- Current Dify requests using `payload.output_schema_mode=strict_json_schema` and `payload.output_schema={...}` require no request-shape change.
- A future generic caller may use top-level `structured_output` instead.
- No SQL migration is required.

## Validation performed

- `python -m compileall -q app tests`
- `python -m unittest discover -s tests -v` — 17 tests passed.
- Replayed the uploaded captured request from `输入.txt` locally and confirmed:
  - schema is resolved from `payload.output_schema`;
  - provider payload uses `text.format.type=json_schema`;
  - the schema object is byte-equivalent after JSON parsing / structurally unchanged;
  - the legacy Railway `canonical_output_contract` is not injected when caller schema is present.

## Deployment

Redeploy both Railway services from this same source revision:

- relay-api
- relay-worker

No database migration and no new environment variable are required.
