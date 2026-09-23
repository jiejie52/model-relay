# Model Relay 0.5.5 - Canonical Model Options + Capability Guard

## Goal

Business Workflow sends provider-neutral model intent. Relay validates it against the frozen provider/model route, freezes the effective request semantics, and only the selected Provider Adapter constructs native wire fields.

```text
Caller / Workflow
  input + instructions + think_level + options + structured_output + execution + metadata
        |
        v
Relay Request acceptance
  frozen Session route + capability validation
        |
        v
Request snapshot
  options + effective_options + capability_revision
        |
        v
Provider Adapter
  Gemini Native / Grok Responses / Kimi Chat native wire
```

## Public v2.2 Request contract

```json
{
  "input": {},
  "instructions": "...",
  "think_level": "high",
  "options": {
    "temperature": 0.08,
    "top_p": 0.95,
    "max_output_tokens": 8000
  },
  "structured_output": {},
  "execution": {"mode": "async"},
  "metadata": {}
}
```

`options` is canonical intent. It is never merged directly into provider JSON.

## Capability resolution

`app/model_options.py` owns the canonical option namespace and strict value validation. Current profiles support `temperature`, `top_p`, and `max_output_tokens` for built-in Gemini/Grok/Kimi routes. Unknown options fail with `OPTION_UNSUPPORTED` before Provider dispatch.

`provider_payload` is deprecated for v2 Requests. The migration bridge recognizes only:

- `temperature -> options.temperature`
- `top_p -> options.top_p`
- `max_output_tokens -> options.max_output_tokens`
- `max_tokens -> options.max_output_tokens`

Unknown legacy provider wire keys fail with `LEGACY_PROVIDER_PAYLOAD_UNSUPPORTED`. Conflicting old/new values fail with `OPTION_CONFLICT`.


## Think-level capability

`think_level` is validated in the same capability layer. Current 0.5.5 behavior:

- Gemini: `auto/low/medium/high`; non-auto is projected to `generationConfig.thinkingConfig.thinkingLevel`.
- Grok: `auto/low/medium/high`; `grok-4.6` also supports `xhigh` and reuses the existing `reasoning.effort` mapping.
- Kimi: `auto` only in this release because the current Moonshot adapter has no explicit reasoning-effort wire mapping. Non-auto fails closed rather than being silently ignored.

## Native projection

### Gemini Native

```text
options.temperature       -> generationConfig.temperature
options.top_p             -> generationConfig.topP
options.max_output_tokens -> generationConfig.maxOutputTokens
```

This fixes the production failure where `provider_payload.temperature` was previously copied to the generateContent top level.

### Grok / OpenAI-compatible Responses

```text
options.temperature       -> temperature
options.top_p             -> top_p
options.max_output_tokens -> max_output_tokens
```

### Kimi / Moonshot Chat

```text
options.temperature       -> temperature
options.top_p             -> top_p
options.max_output_tokens -> max_tokens
```

## Freeze and resume

New Request snapshots use `relay-request/2.2` and persist:

- `options`
- `effective_options`
- `capability_revision`
- existing route/model/execution/structured-output fields

The request hash includes all three option/capability fields. Session RouteBinding also stores `capability_revision`. If a new Request is submitted against a Session frozen under another capability revision, Relay returns `CAPABILITY_PROFILE_CHANGED_RECREATE_SESSION` instead of silently changing semantics.

Existing `relay-request/2.1` snapshots remain executable. In particular, Gemini legacy `provider_payload.temperature` is translated to `generationConfig.temperature` during execution so an in-flight Request can resume after the upgrade.

## Database

No migration is required. Capability revision lives in existing Session metadata and Request snapshots stored in Object Storage.

## Verification

Executed against the 0.5.4 source baseline after the patch:

```text
python -m compileall app    PASS
import app.api              PASS
import app.worker           PASS
pytest -q                   82 passed
```
