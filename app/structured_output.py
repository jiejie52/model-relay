from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class StructuredOutputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StructuredOutputSpec:
    mode: str
    schema: dict[str, Any] | None
    name: str | None
    strict: bool
    source: str
    schema_hash: str | None = None


_SCHEMA_MODES = {"json_schema", "strict_json_schema", "schema"}
_OBJECT_MODES = {"json_object", "object"}
_DISABLED_MODES = {"", "none", "text", "plain_text", "disabled"}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(schema)).hexdigest()


def _safe_name(value: Any, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    raw = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")
    if not raw:
        raw = "structured_output"
    return raw[:64]


def _parse_candidate(
    candidate: Any,
    *,
    source: str,
    fallback_name: str,
) -> StructuredOutputSpec | None:
    if not isinstance(candidate, dict) or not candidate:
        return None

    mode_raw = candidate.get("mode", candidate.get("type", ""))
    schema = candidate.get("schema")
    if schema is None:
        schema = candidate.get("output_schema")

    mode = str(mode_raw or "").strip().lower()
    if not mode and isinstance(schema, dict):
        mode = "json_schema"

    if mode in _DISABLED_MODES:
        return None
    if mode in _OBJECT_MODES:
        return StructuredOutputSpec(
            mode="json_object",
            schema=None,
            name=None,
            strict=False,
            source=source,
            schema_hash=None,
        )
    if mode not in _SCHEMA_MODES:
        raise StructuredOutputError(
            "STRUCTURED_OUTPUT_MODE_UNSUPPORTED",
            f"Unsupported structured-output mode: {mode or '<empty>'}",
        )
    if not isinstance(schema, dict) or not schema:
        raise StructuredOutputError(
            "STRUCTURED_OUTPUT_SCHEMA_MISSING",
            "json_schema mode requires a non-empty schema object",
        )

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise StructuredOutputError(
            "STRUCTURED_OUTPUT_SCHEMA_INVALID",
            f"Invalid JSON Schema: {exc.message}",
        ) from exc

    strict = candidate.get("strict")
    if strict is None:
        # JSON-Schema requests default to strict transport. Callers that need a
        # non-strict provider mode must opt out explicitly with strict=false.
        strict = True

    title = candidate.get("name") or schema.get("title") or fallback_name
    return StructuredOutputSpec(
        mode="json_schema",
        schema=schema,
        name=_safe_name(title, fallback_name),
        strict=bool(strict),
        source=source,
        schema_hash=schema_hash(schema),
    )


def resolve_structured_output(
    request_snapshot: dict[str, Any],
    *,
    fallback_name: str = "structured_output",
) -> StructuredOutputSpec | None:
    """Resolve a provider-neutral structured-output request.

    Precedence is intentionally generic and business-agnostic:
      1. request.structured_output
      2. request.payload.structured_output
      3. compatibility form used by current Dify builders:
         payload.output_schema_mode + payload.output_schema

    The schema body is never interpreted for business fields; it is validated only
    as JSON Schema and then passed to the provider adapter unchanged.
    """

    explicit = _parse_candidate(
        request_snapshot.get("structured_output"),
        source="request.structured_output",
        fallback_name=fallback_name,
    )
    if explicit:
        return explicit

    payload = request_snapshot.get("payload")
    if not isinstance(payload, dict):
        return None

    nested = _parse_candidate(
        payload.get("structured_output"),
        source="request.payload.structured_output",
        fallback_name=fallback_name,
    )
    if nested:
        return nested

    if "output_schema" in payload or "output_schema_mode" in payload:
        compat = {
            "mode": payload.get("output_schema_mode") or "json_schema",
            "schema": payload.get("output_schema"),
            "name": payload.get("output_schema_name"),
            "strict": payload.get("output_schema_strict"),
        }
        return _parse_candidate(
            compat,
            source="request.payload.output_schema",
            fallback_name=fallback_name,
        )

    return None


def openai_responses_text_format(spec: StructuredOutputSpec) -> dict[str, Any]:
    if spec.mode == "json_object":
        return {"type": "json_object"}
    if spec.mode != "json_schema" or not isinstance(spec.schema, dict):
        raise StructuredOutputError(
            "STRUCTURED_OUTPUT_MODE_UNSUPPORTED",
            f"Cannot map structured-output mode to Responses API: {spec.mode}",
        )
    return {
        "type": "json_schema",
        "name": spec.name or "structured_output",
        "schema": spec.schema,
        "strict": bool(spec.strict),
    }


def apply_openai_responses_structured_output(
    provider_payload: dict[str, Any],
    request_snapshot: dict[str, Any],
    *,
    fallback_name: str = "structured_output",
) -> StructuredOutputSpec | None:
    """Apply caller-requested structured output to an OpenAI-compatible payload.

    Only the transport-level `text.format` field is changed. Existing unrelated
    `text` options are preserved. A resolved caller schema is authoritative over a
    legacy/fallback json_object transport setting.
    """

    spec = resolve_structured_output(request_snapshot, fallback_name=fallback_name)
    if spec is None:
        return None

    text = provider_payload.get("text")
    if not isinstance(text, dict):
        text = {}
    else:
        text = dict(text)
    text["format"] = openai_responses_text_format(spec)
    provider_payload["text"] = text
    return spec


def validate_against_schema(value: Any, spec: StructuredOutputSpec) -> None:
    if spec.mode != "json_schema" or not isinstance(spec.schema, dict):
        return
    validator = Draft202012Validator(spec.schema)
    errors = sorted(validator.iter_errors(value), key=lambda err: list(err.absolute_path))
    if not errors:
        return
    first: ValidationError = errors[0]
    path = "$"
    for part in first.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += "." + str(part)
    raise StructuredOutputError(
        "STRUCTURED_OUTPUT_VALIDATION_FAILED",
        f"Provider output does not match caller JSON Schema at {path}: {first.message}",
    )
