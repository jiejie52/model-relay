import asyncio
import json
from types import SimpleNamespace
import unittest

from app.core.idempotency import request_identity
from app.core.raw_error import RawErrorRecorder
from app.providers.base import ProviderHTTPError
from app.providers.registry import ProviderRegistry
from app.v2_models import SessionRequestCreate


class FakeStorageBackend:
    storage_id = "supabase_shared"

    def __init__(self):
        self.objects = {}

    async def put_bytes(self, key, data, *, content_type):
        self.objects[key] = data
        return SimpleNamespace(storage_id=self.storage_id, bucket="bucket", key=key)


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
