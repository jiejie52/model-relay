# Generic Structured Output Passthrough — Model Relay v0.3

## Goal

Railway is transport/runtime infrastructure. It must not know Dify business fields such as candidate, conflict, support, coverage, or semantic-axis names.

This release adds one provider-neutral capability:

> Caller JSON Schema -> Relay -> provider-native structured output -> generic JSON-Schema validation.

The schema body is treated as opaque application data. Railway validates only that it is a valid JSON Schema and that the provider result conforms to it.

## Accepted request forms

### Preferred provider-neutral form

```json
{
  "structured_output": {
    "mode": "json_schema",
    "name": "AnySchemaName",
    "schema": {"type": "object"},
    "strict": true
  }
}
```

### Current Dify compatibility form

No Dify change is required for callers already sending:

```json
{
  "payload": {
    "output_schema_mode": "strict_json_schema",
    "output_schema": {"type": "object"}
  }
}
```

The Relay resolves these forms in this order:

1. `request.structured_output`
2. `request.payload.structured_output`
3. `request.payload.output_schema_mode + request.payload.output_schema`

## Provider mapping

For the OpenAI-compatible `/responses` adapter, a JSON Schema request maps to:

```json
{
  "text": {
    "format": {
      "type": "json_schema",
      "name": "AnySchemaName",
      "schema": {"type": "object"},
      "strict": true
    }
  }
}
```

The `schema` object is passed through unchanged.

If the caller explicitly requests `json_object`, the Responses adapter maps it to:

```json
{"text":{"format":{"type":"json_object"}}}
```

For the Moonshot/Kimi Chat Completions adapter, Relay maps the same provider-neutral contract to Chat Completions `response_format` while retaining the original canonical schema for post-response validation. The wire mapping is protocol-specific; the schema semantics remain caller-owned.

If the caller requests no structured output, the legacy Fusion fallback remains `json_object` for backward compatibility.

## Authority and conflict rules

When a caller schema is present:

- caller schema is authoritative for field names, required fields, enums, arrays/objects, and `additionalProperties`;
- Railway does not inject the legacy `canonical_output_contract` into the model prompt;
- Railway does not run stage-specific business normalization or stage-specific business-field validation;
- Railway runs generic JSON-Schema validation only;
- Railway never silently falls back from requested `json_schema` to `json_object`.

If the upstream provider rejects the provider-native schema request, the Job fails through the Raw Error Contract. The provider HTTP status/body are preserved; Relay does not weaken the schema or normalize the provider error.

## Caller instructions

`request.instructions`, when supplied, is now authoritative for Fusion model stages and is passed through instead of being replaced by Railway-owned stage instructions.

For legacy Fusion callers that do not supply caller instructions or structured output, the previous Railway stage prompts and legacy validators are retained as a compatibility fallback.

## Generic errors

- `STRUCTURED_OUTPUT_MODE_UNSUPPORTED`
- `STRUCTURED_OUTPUT_SCHEMA_MISSING`
- `STRUCTURED_OUTPUT_SCHEMA_INVALID`
- `STRUCTURED_OUTPUT_VALIDATION_FAILED`

These errors contain no Dify business semantics.

## Deployment

For an existing 0.2 deployment, apply `sql/003_core_v2_kimi.sql` before enabling `/v2` jobs. Redeploy API and Core Worker from the same revision. `/health` reports `0.3.0-session-core-kimi`.

## Regression command

```bash
python -m unittest discover -s tests -v
```

The package includes tests proving that an arbitrary caller schema can pass through a Fusion stage without being replaced or rejected by the legacy Global Adjudication business contract.
