# Model Relay 0.5.6 - Gemini Temperature Capability Hotfix

## Problem

Relay 0.5.5 introduced canonical model `options`, but its Gemini 3.5/3.6 capability profiles allowed only `max_output_tokens`. As a result, a valid provider-neutral request such as:

```json
{
  "options": {"temperature": 0, "max_output_tokens": 8192}
}
```

failed at Relay acceptance with `OPTION_UNSUPPORTED` before Gemini dispatch. This pushed provider/model capability knowledge back into Dify DSL, which violates the intended boundary.

## Fix

`app/model_options.py` now allows canonical `temperature` for `gemini-3.5-*` and `gemini-3.6-*`. The caller contract remains provider-neutral. The existing Gemini Native Adapter already performs the correct wire projection:

```text
options.temperature -> generationConfig.temperature
```

No caller-supplied dictionary is merged into Gemini wire JSON.

## Scope

- Added `temperature` to Gemini 3.5/3.6 supported canonical options.
- Kept `max_output_tokens` unchanged.
- Kept `top_p` fail-closed for Gemini 3.5/3.6 until separately validated.
- No change to think-level mapping.
- No change to Request snapshot/hash structure.
- No change to Session/Request/Job recovery semantics.
- No SQL migration.

## Capability revision

`CAPABILITY_PROFILE_REVISION` intentionally remains `relay-model-options/2026-09-23.1`. The 0.5.5 implementation document already described canonical temperature as part of the built-in Gemini option contract; 0.5.6 corrects an implementation mismatch. Keeping the revision avoids forcing existing Relay Sessions to be recreated solely because of this permissive bugfix.

## Expected request path

```text
Workflow
  options.temperature = 0
        |
        v
Relay capability validation
        |
        v
effective_options.temperature = 0.0
        |
        v
GeminiNativeAdapter
        |
        v
generationConfig.temperature = 0.0
```

## Regression coverage

Tests cover both `gemini-3.5-flash-lite` and `gemini-3.6-flash` accepting canonical temperature, while `top_p` remains rejected for those model families.
