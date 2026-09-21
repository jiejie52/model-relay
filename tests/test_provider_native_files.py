import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest

from app.materials.binding_resolver import BindingResolver
from app.materials.ingress import MaterialIngress, MaterialIngressError
from app.materials.provider_files.base import ProviderFileResult
from app.materials.provider_files.kimi_official import KimiOfficialFileAdapter
from app.materials.provider_files.registry import ProviderFileRegistry
from app.providers.base import ProviderHTTPError, ProviderRequestError
from app.providers.moonshot_chat import MoonshotChatAdapter


class FakeRepo:
    def __init__(self):
        self.materials = {}
        self.bindings = []
        self.fallbacks = {}
        self.attempts = {}

    async def find_material_by_idempotency(self, tenant_id, key):
        for row in self.materials.values():
            if row.get("tenant_id") == tenant_id and row.get("idempotency_key") == key:
                return deepcopy(row)
        return None

    async def create_material(self, row):
        self.materials[row["id"]] = deepcopy(row)
        return deepcopy(row)

    async def update_material(self, material_id, values):
        self.materials[material_id].update(deepcopy(values))
        return deepcopy(self.materials[material_id])

    async def get_material(self, material_id, *, tenant_id=None, conversation_hash=None):
        row = self.materials.get(material_id)
        if not row:
            return None
        if tenant_id is not None and row.get("tenant_id") != tenant_id:
            return None
        if conversation_hash is not None and row.get("conversation_hash") != conversation_hash:
            return None
        return deepcopy(row)

    async def upsert_provider_binding(self, row):
        identity = (
            row["material_id"], row["connection_id"], row.get("account_scope_hash"),
            row["purpose"], row["representation"], row["adapter_version"],
            int(row.get("generation") or 1),
        )
        for idx, old in enumerate(self.bindings):
            old_identity = (
                old["material_id"], old["connection_id"], old.get("account_scope_hash"),
                old["purpose"], old["representation"], old["adapter_version"],
                int(old.get("generation") or 1),
            )
            if old_identity == identity:
                self.bindings[idx] = deepcopy(row)
                return deepcopy(row)
        self.bindings.append(deepcopy(row))
        return deepcopy(row)

    async def get_provider_binding(
        self, *, material_id, connection_id, purpose=None, representation=None,
        adapter_version=None, account_scope_hash=None,
    ):
        rows = []
        for row in self.bindings:
            if row.get("material_id") != material_id or row.get("connection_id") != connection_id:
                continue
            if purpose is not None and row.get("purpose") != purpose:
                continue
            if representation is not None and row.get("representation") != representation:
                continue
            if adapter_version is not None and row.get("adapter_version") != adapter_version:
                continue
            if account_scope_hash is not None and row.get("account_scope_hash") != account_scope_hash:
                continue
            rows.append(row)
        rows.sort(key=lambda x: int(x.get("generation") or 0), reverse=True)
        return deepcopy(rows[0]) if rows else None

    async def get_material_fallback(self, material_id):
        row = self.fallbacks.get(material_id)
        return deepcopy(row) if row else None

    async def create_binding_attempt(self, row):
        self.attempts[row["attempt_id"]] = deepcopy(row)
        return deepcopy(row)

    async def update_binding_attempt(self, attempt_id, values):
        self.attempts.setdefault(attempt_id, {}).update(deepcopy(values))
        return deepcopy(self.attempts[attempt_id])

    async def create_object(self, row):
        return deepcopy(row)


class FakeArtifactBackend:
    async def put_bytes(self, key, data, *, content_type):
        return SimpleNamespace(storage_id="supabase_shared", bucket="relay", key=key)


class FakeArtifactStorageRegistry:
    def __init__(self):
        self.backend = FakeArtifactBackend()

    def get(self, storage_id):
        return self.backend


class FakeFallback:
    def __init__(self, repo):
        self.repo = repo
        self.store_calls = []
        self.storage = FakeArtifactStorageRegistry()

    async def store(self, **kwargs):
        self.store_calls.append(deepcopy(kwargs))
        row = {
            "material_id": kwargs["material_id"],
            "object_id": f"obj_{len(self.store_calls)}",
            "storage_id": "supabase_shared",
            "bucket": "relay",
            "object_key": f"fallback/{kwargs['material_id']}",
            "sha256": "fake",
            "size_bytes": len(kwargs["data"]),
            "retention_policy": kwargs["retention_policy"],
        }
        self.repo.fallbacks[kwargs["material_id"]] = deepcopy(row)
        return row

    async def read(self, row):
        # Tests that need refresh put exact bytes here.
        return row.get("_data", b"fallback-bytes")


class FakeNativeAdapter:
    provider = "gemini"
    adapter_version = "fake-native/1"
    connection_id = "native-conn"
    account_scope_hash = "scope-a"

    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    async def prepare(self, material, *, generation):
        self.calls.append((material, generation))
        if self.failure:
            raise self.failure
        return ProviderFileResult(
            binding={
                "provider": self.provider,
                "connection_id": self.connection_id,
                "account_scope_hash": self.account_scope_hash,
                "purpose": "file",
                "representation": "gemini_file_uri",
                "adapter_version": self.adapter_version,
                "external_file_id": "files/abc",
                "external_uri": "https://provider/files/abc",
                "state": "active",
                "processing_state": "active",
                "generation": generation,
                "expires_at": None,
                "metadata": {},
            },
            # Deliberately omitted so unit test never writes Relay artifact storage.
            raw_response=None,
            phase="provider_file_ready",
        )

    async def probe(self, binding):
        return {"state": "active"}

    async def delete(self, binding):
        return None


class FakeMaterials:
    def __init__(self, objects):
        self.objects = objects

    async def read_object_id(self, object_id):
        return self.objects[object_id]


class ProviderNativeFileTests(unittest.TestCase):
    def _settings(self):
        return SimpleNamespace(
            material_default_durability_policy="native_first",
            material_default_fallback_policy="on_provider_unavailable",
            material_ingress_max_bytes=10 * 1024 * 1024,
            material_ingress_timeout_seconds=30,
            material_allow_http=False,
            default_storage_id="supabase_shared",
            relay_storage_prefix="relay",
        )

    def test_native_success_does_not_store_input_file_in_fallback(self):
        repo = FakeRepo()
        fallback = FakeFallback(repo)
        registry = ProviderFileRegistry()
        adapter = FakeNativeAdapter()
        registry.register("native-conn", adapter)
        ingress = MaterialIngress(repo, fallback, registry, self._settings())

        row = asyncio.run(
            ingress.create(
                tenant_id="t",
                conversation_hash="c",
                idempotency_key="m1",
                filename="report.pdf",
                content_type="application/pdf",
                data=b"provider-native-bytes",
                target_connection_id="native-conn",
                durability_policy="native_first",
                fallback_policy="on_provider_unavailable",
            )
        )

        self.assertEqual(row["status"], "ready_provider")
        self.assertEqual(row["durability"], "provider_bound")
        self.assertIsNone(row["object_id"])
        self.assertEqual(fallback.store_calls, [])
        self.assertEqual(len(repo.bindings), 1)
        self.assertEqual(repo.bindings[0]["external_file_id"], "files/abc")
        self.assertEqual(repo.bindings[0]["account_scope_hash"], "scope-a")

    def test_provider_temporary_failure_uses_fallback_only_when_policy_allows(self):
        repo = FakeRepo()
        fallback = FakeFallback(repo)
        registry = ProviderFileRegistry()
        adapter = FakeNativeAdapter(
            ProviderRequestError("PROVIDER_FILE_PROCESSING_TIMEOUT", "provider processing timeout")
        )
        registry.register("native-conn", adapter)
        ingress = MaterialIngress(repo, fallback, registry, self._settings())

        row = asyncio.run(
            ingress.create(
                tenant_id="t",
                conversation_hash="c",
                idempotency_key="m2",
                filename="report.pdf",
                content_type="application/pdf",
                data=b"fallback-me",
                target_connection_id="native-conn",
                fallback_policy="on_provider_unavailable",
            )
        )
        self.assertEqual(row["status"], "fallback_stored")
        self.assertEqual(row["durability"], "relay_backed")
        self.assertEqual(len(fallback.store_calls), 1)
        self.assertEqual(fallback.store_calls[0]["retention_policy"], "provider-unavailable")

    def test_provider_failure_does_not_silently_change_retention_when_fallback_disabled(self):
        repo = FakeRepo()
        fallback = FakeFallback(repo)
        registry = ProviderFileRegistry()
        adapter = FakeNativeAdapter(
            ProviderRequestError("PROVIDER_FILE_PROCESSING_TIMEOUT", "provider processing timeout")
        )
        registry.register("native-conn", adapter)
        ingress = MaterialIngress(repo, fallback, registry, self._settings())

        with self.assertRaises(MaterialIngressError):
            asyncio.run(
                ingress.create(
                    tenant_id="t",
                    conversation_hash="c",
                    idempotency_key="m3",
                    filename="report.pdf",
                    content_type="application/pdf",
                    data=b"do-not-store",
                    target_connection_id="native-conn",
                    fallback_policy="never",
                )
            )
        self.assertEqual(fallback.store_calls, [])
        row = next(iter(repo.materials.values()))
        self.assertEqual(row["status"], "failed")


    def test_provider_429_is_returned_raw_instead_of_silently_stored_as_fallback(self):
        repo = FakeRepo()
        fallback = FakeFallback(repo)
        registry = ProviderFileRegistry()
        body = b'{"error":{"message":"quota exceeded"}}'
        adapter = FakeNativeAdapter(
            ProviderHTTPError(
                429, body, content_type="application/json; charset=utf-8", phase="provider_file_upload"
            )
        )
        registry.register("native-conn", adapter)
        ingress = MaterialIngress(repo, fallback, registry, self._settings())

        with self.assertRaises(MaterialIngressError) as ctx:
            asyncio.run(
                ingress.create(
                    tenant_id="t",
                    conversation_hash="c",
                    idempotency_key="m429",
                    filename="report.pdf",
                    content_type="application/pdf",
                    data=b"do-not-auto-store-on-quota",
                    target_connection_id="native-conn",
                    fallback_policy="on_provider_unavailable",
                )
            )
        self.assertEqual(fallback.store_calls, [])
        self.assertEqual(ctx.exception.detail["upstream_http_status"], 429)
        self.assertEqual(ctx.exception.detail["body_text"], body.decode("utf-8"))

    def test_binding_scope_mismatch_without_fallback_requires_reupload(self):
        repo = FakeRepo()
        repo.materials["mat_1"] = {
            "id": "mat_1",
            "tenant_id": "t",
            "conversation_hash": "c",
            "status": "ready_provider",
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "sha256": "abc",
            "binding_generation": 1,
        }
        repo.bindings.append(
            {
                "material_id": "mat_1",
                "connection_id": "native-conn",
                "account_scope_hash": "old-key-scope",
                "purpose": "file",
                "representation": "gemini_file_uri",
                "adapter_version": "fake-native/1",
                "state": "active",
                "generation": 1,
            }
        )
        fallback = FakeFallback(repo)
        registry = ProviderFileRegistry()
        registry.register("native-conn", FakeNativeAdapter())
        resolver = BindingResolver(repo, fallback, registry)

        with self.assertRaises(ProviderRequestError) as ctx:
            asyncio.run(
                resolver.freeze_for_request(
                    material_ids=["mat_1"],
                    connection_id="native-conn",
                    tenant_id="t",
                    conversation_hash="c",
                )
            )
        self.assertEqual(ctx.exception.code, "MATERIAL_REUPLOAD_REQUIRED")
        self.assertEqual(repo.materials["mat_1"]["status"], "reupload_required")

    def test_kimi_mime_mapping_keeps_text_and_visual_semantics_distinct(self):
        self.assertEqual(KimiOfficialFileAdapter._purpose("application/pdf")[0], "file-extract")
        self.assertEqual(KimiOfficialFileAdapter._purpose("text/plain")[0], "file-extract")
        self.assertEqual(KimiOfficialFileAdapter._purpose("image/png")[0], "image")
        self.assertEqual(KimiOfficialFileAdapter._purpose("video/mp4")[0], "video")
        # SVG is deliberately not treated as Kimi image input.
        self.assertEqual(KimiOfficialFileAdapter._purpose("image/svg+xml")[0], "file-extract")

    def test_kimi_model_uses_extracted_text_and_ms_file_uris_not_raw_bytes(self):
        materials = FakeMaterials({"obj_extract": "hello from document".encode("utf-8")})
        adapter = MoonshotChatAdapter(SimpleNamespace(), materials, SimpleNamespace())
        context = SimpleNamespace(
            material_ids=["m_text", "m_image", "m_video"],
            material_bindings=[
                {
                    "material_id": "m_text",
                    "filename": "a.pdf",
                    "purpose": "file-extract",
                    "metadata": {"extraction_object_id": "obj_extract"},
                },
                {
                    "material_id": "m_image",
                    "filename": "b.png",
                    "purpose": "image",
                    "external_uri": "ms://img123",
                },
                {
                    "material_id": "m_video",
                    "filename": "c.mp4",
                    "purpose": "video",
                    "external_uri": "ms://vid123",
                },
            ],
        )
        system_messages, visual_parts = asyncio.run(adapter._material_parts(context))
        self.assertIn("[SOURCE_FILE:a.pdf]", system_messages[0]["content"])
        self.assertIn("hello from document", system_messages[0]["content"])
        self.assertEqual(visual_parts[0], {"type": "image_url", "image_url": {"url": "ms://img123"}})
        self.assertEqual(visual_parts[1], {"type": "video_url", "video_url": {"url": "ms://vid123"}})


if __name__ == "__main__":
    unittest.main()
