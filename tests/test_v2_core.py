import asyncio
import base64
import json
from types import SimpleNamespace
import unittest

from app.core.idempotency import request_identity
from app.core.raw_error import RawErrorRecorder
from app.providers.base import ProviderHTTPError
from app.providers.registry import ProviderRegistry
from app.api_v2.router import _request_envelope, _request_envelope_hydrated
from app.v2_models import RawErrorMeta, SessionRequestCreate


class FakeStorageBackend:
    storage_id = "supabase_shared"

    def __init__(self):
        self.objects = {}

    async def put_bytes(self, key, data, *, content_type):
        self.objects[key] = data
        return SimpleNamespace(storage_id=self.storage_id, bucket="bucket", key=key)

    async def get_bytes(self, location):
        return self.objects[location.key]


class FakeStorageRegistry:
    def __init__(self):
        self.backend = FakeStorageBackend()

    def get(self, storage_id):
        assert storage_id == "supabase_shared"
        return self.backend


class FakeRepo:
    def __init__(self):
        self.rows = []

    async def create_object(self, row):
        self.rows.append(row)
        return row

    async def get_object(self, object_id, *, tenant_id=None, conversation_hash=None):
        for row in self.rows:
            if row.get("id") != object_id:
                continue
            if tenant_id is not None and row.get("tenant_id") != tenant_id:
                continue
            if conversation_hash is not None and row.get("conversation_hash") != conversation_hash:
                continue
            return row
        return None


class RelayV2CoreTests(unittest.TestCase):
    def test_request_identity_changes_when_execution_relevant_input_changes(self):
        base = {
            "session_id": "s1",
            "tenant_id": "t",
            "conversation_hash": "c",
            "input": "hello",
            "material_ids": ["mat1"],
            "provider": "kimi",
            "connection_id": "moonshot_official",
            "model": "kimi-k2",
            "execution": {"mode": "sync"},
            "structured_output": {},
            "provider_payload": {},
        }
        self.assertEqual(request_identity(base), request_identity(dict(base)))
        changed = dict(base)
        changed["input"] = "different"
        self.assertNotEqual(request_identity(base), request_identity(changed))

    def test_session_request_allows_sync_without_job_semantics(self):
        req = SessionRequestCreate(input="q", execution={"mode": "sync"})
        self.assertEqual(req.execution.mode, "sync")

    def test_session_request_accepts_provider_neutral_options(self):
        req = SessionRequestCreate(
            input="q",
            execution={"mode": "sync"},
            options={"temperature": 0.08},
        )
        self.assertEqual(req.options["temperature"], 0.08)

    def test_v22_request_identity_includes_effective_options_and_capability_revision(self):
        base = {
            "schema_version": "relay-request/2.2",
            "session_id": "s1",
            "tenant_id": "t",
            "conversation_hash": "c",
            "input": "hello",
            "material_ids": [],
            "provider": "gemini",
            "connection_id": "aihubmix_gemini_native",
            "model": "gemini-3.1-flash-lite",
            "execution": {"mode": "sync"},
            "structured_output": {},
            "options": {"temperature": 0.08},
            "effective_options": {"temperature": 0.08},
            "capability_revision": "cap/1",
        }
        changed = dict(base)
        changed["effective_options"] = {"temperature": 0.1}
        self.assertNotEqual(request_identity(base), request_identity(changed))

    def test_raw_provider_error_is_archived_without_truncation(self):
        body = (b"x" * 20000) + b"END"
        repo = FakeRepo()
        storage = FakeStorageRegistry()
        settings = SimpleNamespace(
            default_storage_id="supabase_shared",
            relay_storage_prefix="relay",
        )
        recorder = RawErrorRecorder(repo, storage, settings)
        error = asyncio.run(
            recorder.record(
                exc=ProviderHTTPError(
                    429,
                    body,
                    content_type="text/plain",
                    request_id="up-1",
                ),
                source="provider",
                tenant_id="t",
                conversation_hash="c",
                session_id="00000000-0000-0000-0000-000000000001",
                request_id="00000000-0000-0000-0000-000000000002",
            )
        )
        self.assertEqual(error["body_size"], len(body))
        self.assertEqual(error["upstream_http_status"], 429)
        self.assertEqual(error["upstream_request_id"], "up-1")
        self.assertTrue(any(value == body for value in storage.backend.objects.values()))

    def test_360_byte_json_provider_error_is_returned_inline_losslessly(self):
        prefix = b'{"error":"'
        suffix = b'"}'
        body = prefix + (b"x" * (360 - len(prefix) - len(suffix))) + suffix
        self.assertEqual(len(body), 360)

        repo = FakeRepo()
        storage = FakeStorageRegistry()
        settings = SimpleNamespace(
            default_storage_id="supabase_shared",
            relay_storage_prefix="relay",
        )
        recorder = RawErrorRecorder(repo, storage, settings)
        error = asyncio.run(
            recorder.record(
                exc=ProviderHTTPError(
                    400,
                    body,
                    content_type="application/json; charset=utf-8",
                ),
                source="provider",
                tenant_id="t",
                conversation_hash="c",
                session_id="00000000-0000-0000-0000-000000000001",
                request_id="00000000-0000-0000-0000-000000000002",
            )
        )

        self.assertEqual(error["body_size"], 360)
        self.assertEqual(error["body_encoding"], "utf-8")
        self.assertEqual(error["body_text"], body.decode("utf-8"))
        self.assertEqual(error["message"], body.decode("utf-8"))
        self.assertEqual(base64.b64decode(error["body_base64"]), body)
        self.assertEqual(len(error["body_text"].encode("utf-8")), 360)

        # Pydantic/API envelope must not filter the new raw-body fields.
        parsed = RawErrorMeta.model_validate(error)
        self.assertEqual(parsed.body_text, body.decode("utf-8"))
        envelope = _request_envelope(
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "id": "00000000-0000-0000-0000-000000000002",
                "job_id": None,
                "execution_mode": "sync",
                "status": "failed",
                "expected_history_version": 0,
                "compact_result": None,
                "error": error,
            }
        ).model_dump(mode="json")
        self.assertEqual(envelope["error"]["body_text"], body.decode("utf-8"))
        self.assertEqual(base64.b64decode(envelope["error"]["body_base64"]), body)

    def test_030_archived_error_is_hydrated_for_idempotent_replay(self):
        body = b'{"error":{"message":"original upstream error"}}'
        repo = FakeRepo()
        storage = FakeStorageRegistry()
        object_id = "obj_old"
        object_key = "relay/t/c/s/r/raw-error.bin"
        storage.backend.objects[object_key] = body
        repo.rows.append(
            {
                "id": object_id,
                "tenant_id": "t",
                "conversation_hash": "c",
                "storage_id": "supabase_shared",
                "bucket": "bucket",
                "object_key": object_key,
                "content_type": "application/json",
            }
        )
        fake_request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(v2_repo=repo, storage_registry=storage)
            )
        )
        row = {
            "session_id": "00000000-0000-0000-0000-000000000001",
            "id": "00000000-0000-0000-0000-000000000002",
            "job_id": None,
            "tenant_id": "t",
            "conversation_hash": "c",
            "execution_mode": "sync",
            "status": "failed",
            "expected_history_version": 0,
            "error": {
                "source": "provider",
                "upstream_http_status": 400,
                "content_type": "application/json; charset=utf-8",
                "body_encoding": "binary",
                "body_size": len(body),
                "body_object_id": object_id,
                "message": "Upstream provider request failed",
            },
        }
        env = asyncio.run(_request_envelope_hydrated(fake_request, row)).model_dump(mode="json")
        self.assertEqual(env["error"]["body_encoding"], "utf-8")
        self.assertEqual(env["error"]["body_text"], body.decode("utf-8"))
        self.assertEqual(env["error"]["message"], body.decode("utf-8"))
        self.assertEqual(base64.b64decode(env["error"]["body_base64"]), body)

    def test_binary_provider_error_keeps_binary_encoding_and_exact_base64(self):
        body = bytes([0, 255, 1, 2, 3])
        repo = FakeRepo()
        storage = FakeStorageRegistry()
        settings = SimpleNamespace(
            default_storage_id="supabase_shared",
            relay_storage_prefix="relay",
        )
        recorder = RawErrorRecorder(repo, storage, settings)
        error = asyncio.run(
            recorder.record(
                exc=ProviderHTTPError(
                    502,
                    body,
                    content_type="application/octet-stream",
                ),
                source="provider",
                tenant_id="t",
                conversation_hash="c",
                session_id="00000000-0000-0000-0000-000000000001",
                request_id="00000000-0000-0000-0000-000000000002",
            )
        )
        self.assertEqual(error["body_encoding"], "binary")
        self.assertIsNone(error["body_text"])
        self.assertEqual(base64.b64decode(error["body_base64"]), body)

    def test_provider_registry_is_connection_scoped(self):
        settings = SimpleNamespace(
            enabled_connection_set={"moonshot_official"},
            aihubmix_api_key=None,
        )
        registry = ProviderRegistry(settings)
        marker = object()
        registry.register_v2("moonshot_official", marker)
        self.assertIs(registry.get_v2("moonshot_official"), marker)
        with self.assertRaises(KeyError):
            registry.get_v2("aihubmix_default")


if __name__ == "__main__":
    unittest.main()
