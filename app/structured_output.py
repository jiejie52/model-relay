from __future__ import annotations

from copy import deepcopy
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


@dataclass(frozen=True)
class ProviderSchemaProjection:
    """Provider-facing projection of the caller's canonical JSON Schema.

    `schema` is only the schema sent upstream. The caller's full schema remains in
    StructuredOutputSpec and is therefore still used for deterministic post-call
    validation. Dropping a provider-unsupported keyword never weakens Relay's
    canonical validation contract.
    """

    schema: dict[str, Any]
    provider_family: str
    dropped_keywords: tuple[str, ...] = ()
    rewritten_keywords: tuple[str, ...] = ()


_SCHEMA_MODES = {"json_schema", "strict_json_schema", "schema"}
_OBJECT_MODES = {"json_object", "object"}
_DISABLED_MODES = {"", "none", "text", "plain_text", "disabled"}

# AIHubMix currently translates OpenAI-compatible Responses `text.format.schema`
# for Gemini models to Gemini native GenerationConfig.responseSchema. That field
# accepts the Gemini `Schema` object (OpenAPI 3.0 subset), not arbitrary Draft
# 2020-12 JSON Schema. Keep this list intentionally aligned to the native Schema
# object rather than trying to infer Dify/Fusion business semantics.
_GEMINI_RESPONSE_SCHEMA_KEYS = {
    "type",
    "format",
    "title",
    "description",
    "nullable",
    "enum",
    "maxItems",
    "minItems",
    "properties",
    "required",
    "minProperties",
    "maxProperties",
    "minLength",
    "maxLength",
    "pattern",
    "example",
    "anyOf",
    "propertyOrdering",
    "default",
    "items",
    "minimum",
    "maximum",
}


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

    The canonical schema body is never interpreted for business fields. It is
    validated as Draft 2020-12 here, projected only for provider transport, and
    retained unchanged for deterministic post-call validation.
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


def _provider_family(provider: Any, model: Any) -> str:
    provider_name = str(provider or "").strip().lower()
    model_name = str(model or "").strip().lower()
    if provider_name in {"gemini", "google", "google_ai", "google-ai"} or model_name.startswith(
        "gemini-"
    ):
        return "gemini"
    if provider_name in {"grok", "xai"} or model_name.startswith("grok-"):
        return "grok"
    if provider_name in {"openai", "openai_compatible", "openai-compatible"}:
        return "openai_compatible"
    return provider_name or "openai_compatible"


def _json_pointer_get(root: Any, ref: str) -> Any:
    if not ref.startswith("#/"):
        raise StructuredOutputError(
            "STRUCTURED_OUTPUT_PROVIDER_SCHEMA_UNSUPPORTED",
            f"Gemini responseSchema projection cannot dereference external JSON Schema ref: {ref}",
        )
    current = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise StructuredOutputError(
                "STRUCTURED_OUTPUT_SCHEMA_INVALID",
                f"Local JSON Schema ref does not resolve: {ref}",
            )
        current = current[part]
    return current


def _merge_description(existing: Any, notes: list[str]) -> str | None:
    clean_notes = [str(note).strip() for note in notes if str(note).strip()]
    prefix = str(existing or "").strip()
    if not clean_notes:
        return prefix or None
    suffix = "Provider projection guidance: " + " ".join(clean_notes)
    return f"{prefix}\n\n{suffix}" if prefix else suffix


def _project_gemini_response_schema(
    schema: dict[str, Any],
) -> ProviderSchemaProjection:
    """Project Draft 2020-12 JSON Schema to Gemini native responseSchema.

    AIHubMix's current Gemini `/responses` compatibility layer maps the schema into
    `generation_config.response_schema`, whose wire type is Gemini's `Schema`
    object. Unsupported JSON-Schema keywords are removed only from the provider
    copy. The full caller schema is still enforced after generation.

    A few common constraints are converted into equivalent/best-effort native
    guidance (`const` -> `enum`, nullable type arrays -> `nullable`, `oneOf` ->
    `anyOf`). Unsupported enforcement-only constraints such as `uniqueItems` and
    `additionalProperties` are represented as generic description guidance while
    remaining authoritative in post-call validation.
    """

    dropped: set[str] = set()
    rewritten: set[str] = set()
    resolving_refs: set[str] = set()

    class _GeminiProjectionUnsupported(Exception):
        def __init__(self, path: str, reason: str) -> None:
            super().__init__(reason)
            self.path = path
            self.reason = reason

    def project(node: Any, path: str = "$", root: dict[str, Any] | None = None) -> Any:
        root = schema if root is None else root

        if isinstance(node, bool):
            # Gemini native Schema has no boolean-schema form. `true` imposes no
            # native constraint; `false` cannot be represented and therefore only
            # remains enforceable by the canonical post-call validator.
            dropped.add(f"{path}:boolean_schema")
            return {}
        if not isinstance(node, dict):
            return deepcopy(node)

        # Resolve local refs before filtering because native responseSchema has no
        # $ref/$defs mechanism. Sibling keywords in Draft 2020-12 are preserved by
        # overlaying them on the referenced target before projection.
        if isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if ref in resolving_refs:
                raise StructuredOutputError(
                    "STRUCTURED_OUTPUT_PROVIDER_SCHEMA_UNSUPPORTED",
                    f"Gemini responseSchema projection cannot inline cyclic ref: {ref}",
                )
            resolving_refs.add(ref)
            target = _json_pointer_get(root, ref)
            if not isinstance(target, dict):
                raise StructuredOutputError(
                    "STRUCTURED_OUTPUT_SCHEMA_INVALID",
                    f"JSON Schema ref must resolve to an object for Gemini projection: {ref}",
                )
            merged = deepcopy(target)
            for key, value in node.items():
                if key != "$ref":
                    merged[key] = value
            rewritten.add(f"{path}:$ref->inline")
            try:
                return project(merged, path, root)
            finally:
                resolving_refs.remove(ref)

        out: dict[str, Any] = {}
        guidance: list[str] = []

        raw_type = node.get("type")
        if isinstance(raw_type, list):
            types = [str(item) for item in raw_type]
            non_null = [item for item in types if item != "null"]
            has_null = len(non_null) != len(types)
            if len(non_null) == 1:
                out["type"] = non_null[0]
                if has_null:
                    out["nullable"] = True
                    rewritten.add(f"{path}:type-nullable")
            elif non_null:
                out["anyOf"] = [{"type": item} for item in non_null]
                if has_null:
                    out["nullable"] = True
                rewritten.add(f"{path}:type-array->anyOf")
            else:
                dropped.add(f"{path}:type")
        elif isinstance(raw_type, str):
            out["type"] = raw_type

        # Native structural recursion. Property names are data, not schema
        # keywords, so they are retained exactly. Gemini native responseSchema
        # requires every ARRAY schema to declare `items`. Canonical JSON Schema
        # allows an unconstrained array (`{"type":"array"}`), so an optional
        # property with that shape is omitted only from the provider-facing copy.
        # A required unprojectable property fails closed instead of inventing an
        # item type that could change the caller's contract.
        properties = node.get("properties")
        required_raw = node.get("required")
        required_names = {
            str(name)
            for name in required_raw
            if isinstance(name, str)
        } if isinstance(required_raw, list) else set()
        if isinstance(properties, dict):
            projected_properties: dict[str, Any] = {}
            for name, child in properties.items():
                property_name = str(name)
                child_path = f"{path}.properties.{property_name}"
                try:
                    projected_child = project(child, child_path, root)
                except _GeminiProjectionUnsupported as exc:
                    if property_name in required_names:
                        raise
                    dropped.add(f"{child_path}:optional_property")
                    dropped.add(f"{exc.path}:provider_unrepresentable")
                    continue
                projected_properties[property_name] = projected_child
            if projected_properties:
                out["properties"] = projected_properties

            if isinstance(required_raw, list):
                projected_required = [
                    str(name)
                    for name in required_raw
                    if isinstance(name, str) and str(name) in projected_properties
                ]
                if projected_required:
                    out["required"] = projected_required

        if "items" in node:
            out["items"] = project(node.get("items"), f"{path}.items", root)

        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            out["anyOf"] = [
                project(child, f"{path}.anyOf[{index}]", root)
                for index, child in enumerate(any_of)
            ]

        one_of = node.get("oneOf")
        if isinstance(one_of, list):
            projected = [
                project(child, f"{path}.oneOf[{index}]", root)
                for index, child in enumerate(one_of)
            ]
            if "anyOf" in out:
                out["anyOf"].extend(projected)
            else:
                out["anyOf"] = projected
            rewritten.add(f"{path}:oneOf->anyOf")

        prefix_items = node.get("prefixItems")
        if isinstance(prefix_items, list) and prefix_items:
            projected_prefix = [
                project(child, f"{path}.prefixItems[{index}]", root)
                for index, child in enumerate(prefix_items)
            ]
            if "items" not in out:
                out["items"] = (
                    projected_prefix[0]
                    if len(projected_prefix) == 1
                    else {"anyOf": projected_prefix}
                )
            guidance.append(
                "Tuple-position constraints from prefixItems remain subject to canonical post-validation."
            )
            rewritten.add(f"{path}:prefixItems->items")

        if out.get("type") == "array" and "items" not in out:
            raise _GeminiProjectionUnsupported(
                path,
                "Gemini native responseSchema requires ARRAY schemas to declare items",
            )

        # Copy scalar/list keywords that Gemini's native Schema object accepts.
        handled = {
            "type",
            "properties",
            "required",
            "items",
            "anyOf",
            "oneOf",
            "prefixItems",
            "$ref",
            "description",
        }
        for key in _GEMINI_RESPONSE_SCHEMA_KEYS - handled:
            if key in node:
                out[key] = deepcopy(node[key])

        if "const" in node:
            if "enum" not in out:
                out["enum"] = [deepcopy(node["const"])]
                rewritten.add(f"{path}:const->enum")
            else:
                dropped.add(f"{path}:const")

        if node.get("uniqueItems") is True:
            guidance.append("Array items must be unique.")
            dropped.add(f"{path}:uniqueItems")
        elif "uniqueItems" in node:
            dropped.add(f"{path}:uniqueItems")

        if "additionalProperties" in node:
            additional = node.get("additionalProperties")
            if additional is False and isinstance(properties, dict):
                guidance.append("Do not emit properties other than the named properties in this object.")
            elif isinstance(additional, dict):
                guidance.append(
                    "Additional-property values remain subject to the caller's canonical schema."
                )
            dropped.add(f"{path}:additionalProperties")

        # Track every unsupported keyword at schema nodes. `$defs`/`definitions`
        # are intentionally not emitted after any local refs have been inlined.
        for key in node:
            if key in handled or key in _GEMINI_RESPONSE_SCHEMA_KEYS:
                continue
            if key in {"const", "uniqueItems", "additionalProperties"}:
                continue
            dropped.add(f"{path}:{key}")

        description = _merge_description(node.get("description"), guidance)
        if description:
            out["description"] = description

        return out

    try:
        projected = project(schema)
    except _GeminiProjectionUnsupported as exc:
        raise StructuredOutputError(
            "STRUCTURED_OUTPUT_PROVIDER_SCHEMA_UNSUPPORTED",
            f"Gemini responseSchema cannot represent caller schema at {exc.path}: {exc.reason}",
        ) from exc
    if not isinstance(projected, dict) or not projected:
        raise StructuredOutputError(
            "STRUCTURED_OUTPUT_PROVIDER_SCHEMA_UNSUPPORTED",
            "Caller JSON Schema cannot be projected to Gemini native responseSchema",
        )

    return ProviderSchemaProjection(
        schema=projected,
        provider_family="gemini",
        dropped_keywords=tuple(sorted(dropped)),
        rewritten_keywords=tuple(sorted(rewritten)),
    )


def project_schema_for_provider(
    schema: dict[str, Any],
    *,
    provider: Any = None,
    model: Any = None,
) -> ProviderSchemaProjection:
    """Return the provider-facing schema without changing the canonical schema.

    Gemini is projected to the native `responseSchema` subset because the current
    AIHubMix compatibility layer targets that field. Other OpenAI-compatible
    providers keep the caller schema unchanged; dedicated projections can be added
    here later without changing Dify's request contract.
    """

    family = _provider_family(provider, model)
    if family == "gemini":
        return _project_gemini_response_schema(schema)
    return ProviderSchemaProjection(
        schema=deepcopy(schema),
        provider_family=family,
    )


def openai_responses_text_format(
    spec: StructuredOutputSpec,
    *,
    provider: Any = None,
    model: Any = None,
) -> dict[str, Any]:
    if spec.mode == "json_object":
        return {"type": "json_object"}
    if spec.mode != "json_schema" or not isinstance(spec.schema, dict):
        raise StructuredOutputError(
            "STRUCTURED_OUTPUT_MODE_UNSUPPORTED",
            f"Cannot map structured-output mode to Responses API: {spec.mode}",
        )

    projection = project_schema_for_provider(
        spec.schema,
        provider=provider,
        model=model,
    )
    return {
        "type": "json_schema",
        "name": spec.name or "structured_output",
        "schema": projection.schema,
        "strict": bool(spec.strict),
    }


def apply_openai_responses_structured_output(
    provider_payload: dict[str, Any],
    request_snapshot: dict[str, Any],
    *,
    fallback_name: str = "structured_output",
    provider: Any = None,
    model: Any = None,
) -> StructuredOutputSpec | None:
    """Apply caller structured output to an OpenAI-compatible `/responses` payload.

    The request contract remains provider-neutral. The provider adapter may project
    the canonical JSON Schema to a provider-native subset before transport, but it
    never interprets business property names. The original StructuredOutputSpec is
    returned so downstream validation always uses the full caller schema.
    """

    spec = resolve_structured_output(request_snapshot, fallback_name=fallback_name)
    if spec is None:
        return None

    provider = request_snapshot.get("provider") if provider is None else provider
    model = request_snapshot.get("model") if model is None else model

    text = provider_payload.get("text")
    if not isinstance(text, dict):
        text = {}
    else:
        text = dict(text)
    text["format"] = openai_responses_text_format(
        spec,
        provider=provider,
        model=model,
    )
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
