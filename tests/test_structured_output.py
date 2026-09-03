import json
from types import SimpleNamespace
import unittest

from app.fusion_runtime import FusionRuntime
from app.models import JobSubmitRequest
from app.structured_output import (
    StructuredOutputError,
    apply_openai_responses_structured_output,
    project_schema_for_provider,
    resolve_structured_output,
    validate_against_schema,
)


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ArbitraryCallerSchema",
    "type": "object",
    "required": ["alpha", "items"],
    "properties": {
        "alpha": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["n"],
                "properties": {"n": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


class StructuredOutputTests(unittest.TestCase):
    def test_dify_payload_output_schema_is_resolved_without_business_inspection(self):
        request = {
            "stage": "global_adjudication",
            "payload": {
                "output_schema_mode": "strict_json_schema",
                "output_schema": SCHEMA,
            },
        }
        spec = resolve_structured_output(request, fallback_name="global_adjudication")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.mode, "json_schema")
        self.assertTrue(spec.strict)
        self.assertEqual(spec.schema, SCHEMA)
        self.assertEqual(spec.name, "ArbitraryCallerSchema")
        self.assertEqual(spec.source, "request.payload.output_schema")

    def test_top_level_provider_neutral_contract_is_accepted(self):
        req = JobSubmitRequest(
            tenant_id="tenant",
            conversation_hash="hash",
            stage="normal_inference",
            provider="grok",
            model="grok-4.6",
            current_query="q",
            structured_output={
                "mode": "json_schema",
                "schema": SCHEMA,
                "strict": True,
            },
        )
        spec = resolve_structured_output(req.model_dump(mode="json"))
        self.assertEqual(spec.schema, SCHEMA)

    def test_non_gemini_responses_mapping_preserves_schema_exactly(self):
        request = {
            "provider": "grok",
            "model": "grok-4.6",
            "payload": {
                "output_schema_mode": "strict_json_schema",
                "output_schema": SCHEMA,
            },
        }
        provider_payload = {
            "temperature": 0,
            "text": {"verbosity": "low", "format": {"type": "json_object"}},
        }
        spec = apply_openai_responses_structured_output(
            provider_payload, request, fallback_name="stage"
        )
        self.assertEqual(spec.mode, "json_schema")
        self.assertEqual(provider_payload["text"]["verbosity"], "low")
        fmt = provider_payload["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["name"], "ArbitraryCallerSchema")
        self.assertEqual(fmt["schema"], SCHEMA)
        self.assertTrue(fmt["strict"])

    def test_gemini_projection_removes_native_response_schema_unsupported_keywords(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "GeminiProjection",
            "type": "object",
            "required": ["ids", "name"],
            "properties": {
                "ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^SS-[A-Z0-9]+$"},
                },
                "name": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        }
        projection = project_schema_for_provider(
            schema, provider="gemini", model="gemini-3.1-flash-lite"
        )
        provider_schema = projection.schema
        rendered = json.dumps(provider_schema, ensure_ascii=False)
        self.assertEqual(projection.provider_family, "gemini")
        self.assertNotIn('"uniqueItems"', rendered)
        self.assertNotIn('"$schema"', rendered)
        self.assertNotIn('"additionalProperties"', rendered)
        self.assertEqual(provider_schema["properties"]["ids"]["minItems"], 1)
        self.assertEqual(
            provider_schema["properties"]["ids"]["items"]["pattern"],
            "^SS-[A-Z0-9]+$",
        )
        self.assertIn("Array items must be unique", provider_schema["properties"]["ids"]["description"])
        self.assertIn("Do not emit properties", provider_schema["description"])

    def test_gemini_transport_uses_projected_schema_but_returns_canonical_spec(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "GeminiTransport",
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string"},
                }
            },
            "additionalProperties": False,
        }
        request = {
            "provider": "gemini",
            "model": "gemini-3.1-flash-lite",
            "payload": {
                "output_schema_mode": "strict_json_schema",
                "output_schema": schema,
            },
        }
        provider_payload = {}
        spec = apply_openai_responses_structured_output(
            provider_payload, request, fallback_name="stage"
        )
        self.assertEqual(spec.schema, schema)
        sent_schema = provider_payload["text"]["format"]["schema"]
        self.assertNotEqual(sent_schema, schema)
        self.assertNotIn("uniqueItems", sent_schema["properties"]["ids"])

    def test_fusion_provider_payload_uses_caller_schema_instead_of_json_object(self):
        runtime = FusionRuntime(
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
        )
        snapshot = {
            "provider": "grok",
            "model": "grok-4.6",
            "payload": {
                "output_schema_mode": "strict_json_schema",
                "output_schema": SCHEMA,
            },
        }
        payload = runtime._provider_payload("global_adjudication", snapshot)
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertEqual(payload["text"]["format"]["schema"], SCHEMA)

    def test_legacy_fusion_transport_still_defaults_to_json_object(self):
        runtime = FusionRuntime(
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
        )
        payload = runtime._provider_payload("global_adjudication", {})
        self.assertEqual(payload["text"], {"format": {"type": "json_object"}})

    def test_caller_schema_disables_legacy_global_output_contract_in_prompt(self):
        runtime = FusionRuntime(
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
        )
        stage_payload = {
            "output_schema_mode": "strict_json_schema",
            "output_schema": SCHEMA,
            "caller_requirement": "use the caller schema",
        }
        spec = resolve_structured_output({"payload": stage_payload})
        instructions, query = runtime._build_stage_prompt(
            "global_adjudication",
            {"business_question": "q", "candidate_manifest": []},
            stage_payload,
            {},
            structured_spec=spec,
        )
        context = json.loads(query)
        self.assertNotIn("canonical_output_contract", context)
        self.assertIn("caller-provided structured-output schema is authoritative", instructions)
        self.assertNotIn("security_risk", instructions)

    def test_explicit_caller_instructions_are_not_replaced(self):
        runtime = FusionRuntime(
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
        )
        instructions, _ = runtime._build_stage_prompt(
            "global_adjudication",
            {},
            {},
            {},
            caller_instructions="CALLER_INSTRUCTIONS_EXACT",
        )
        self.assertEqual(instructions, "CALLER_INSTRUCTIONS_EXACT")

    def test_generic_schema_validation_accepts_and_rejects_without_domain_logic(self):
        spec = resolve_structured_output(
            {"structured_output": {"mode": "json_schema", "schema": SCHEMA}}
        )
        validate_against_schema({"alpha": "x", "items": [{"n": 1}]}, spec)
        with self.assertRaises(StructuredOutputError) as ctx:
            validate_against_schema({"alpha": "x", "items": [{"n": "bad"}]}, spec)
        self.assertEqual(ctx.exception.code, "STRUCTURED_OUTPUT_VALIDATION_FAILED")


if __name__ == "__main__":
    unittest.main()

class _ExecBackend:
    def __init__(self, manifest):
        self.objects = {"manifest.json": json.dumps(manifest).encode("utf-8")}

    async def storage_get_json(self, path):
        return json.loads(self.objects[path].decode("utf-8"))

    async def storage_get(self, path):
        return self.objects[path]

    async def storage_put(self, path, data, content_type="application/json", upsert=True):
        self.objects[path] = data
        return path


class _ExecRepo:
    def __init__(self):
        self.artifacts = []

    async def get_fusion_corpus(self, corpus_id, **kwargs):
        return {
            "id": corpus_id,
            "manifest_object_path": "manifest.json",
            "version": 1,
            "corpus_hash": "hash",
        }

    async def next_fusion_artifact_version(self, corpus_id, artifact_type):
        return 1

    async def create_fusion_artifact(self, row):
        self.artifacts.append(row)
        return row

    async def get_fusion_artifact(self, artifact_id, corpus_id=None):
        return None


class _ExecProvider:
    async def execute(self, request_snapshot, **kwargs):
        from app.providers.base import ProviderResult
        # Deliberately does not match the legacy global_adjudication contract.
        # If Railway still applies legacy business validation, this test fails.
        body = {"alpha": "ok", "items": [{"n": 7}]}
        raw = json.dumps({"output_text": json.dumps(body)}).encode("utf-8")
        return ProviderResult(
            raw_bytes=raw,
            raw_json={"output_text": json.dumps(body)},
            text=json.dumps(body),
            response_id="resp_test",
            usage={},
            cached_tokens=0,
            response_output=[],
        )


class _ExecProviders:
    def get(self, provider):
        return _ExecProvider()


class StructuredOutputExecutionTests(unittest.TestCase):
    def test_fusion_execution_accepts_arbitrary_caller_schema_and_skips_legacy_business_validation(self):
        manifest = {
            "fusion_corpus_id": "fcor_test",
            "business_question": "q",
            "candidate_manifest": [],
            "materials": [],
        }
        runtime = FusionRuntime(
            _ExecBackend(manifest),
            _ExecRepo(),
            _ExecProviders(),
            SimpleNamespace(relay_storage_prefix="relay"),
        )
        job = {
            "tenant_id": "tenant",
            "conversation_hash": "conv",
            "stage": "global_adjudication",
            "provider": "gemini",
            "model": "model",
            "think_level": "medium",
        }
        snapshot = {
            "stage": "global_adjudication",
            "provider": "gemini",
            "model": "model",
            "think_level": "medium",
            "fusion_corpus_id": "fcor_test",
            "provider_payload": {},
            "payload": {
                "output_schema_mode": "strict_json_schema",
                "output_schema": SCHEMA,
            },
        }
        import asyncio
        result = asyncio.run(runtime.execute(job, snapshot))
        self.assertEqual(result.payload, {"alpha": "ok", "items": [{"n": 7}]})

class _HTTPResponse:
    status_code = 200

    async def aread(self):
        return json.dumps({"id": "resp", "output_text": '{"alpha":"ok","items":[]}', "output": [], "usage": {}}).encode("utf-8")


class _HTTPClient:
    captured_json = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).captured_json = json
        return _HTTPResponse()


class ProviderStructuredOutputIntegrationTests(unittest.TestCase):
    def test_provider_execute_sends_json_schema_to_responses(self):
        import asyncio
        from unittest.mock import patch
        from pydantic import SecretStr
        from app.providers.openai_compatible import OpenAICompatibleResponsesProvider

        settings = SimpleNamespace(
            aihubmix_root="https://example.invalid/v1",
            aihubmix_api_key=SecretStr("secret"),
            upstream_connect_timeout_seconds=1.0,
            upstream_write_timeout_seconds=1.0,
            upstream_pool_timeout_seconds=1.0,
        )
        provider = OpenAICompatibleResponsesProvider(settings)
        request = {
            "stage": "any_stage",
            "provider": "gemini",
            "model": "any-model",
            "think_level": "medium",
            "mode": "stateless",
            "current_query": "q",
            "payload": {
                "output_schema_mode": "strict_json_schema",
                "output_schema": SCHEMA,
            },
            "provider_payload": {"text": {"format": {"type": "json_object"}}},
        }
        with patch("app.providers.openai_compatible.httpx.AsyncClient", _HTTPClient):
            asyncio.run(provider.execute(request, session=None, material_prefix=None, history=[]))
        fmt = _HTTPClient.captured_json["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        rendered = json.dumps(fmt["schema"], ensure_ascii=False)
        self.assertNotEqual(fmt["schema"], SCHEMA)
        self.assertNotIn('"$schema"', rendered)
        self.assertNotIn('"additionalProperties"', rendered)
        self.assertTrue(fmt["strict"])
