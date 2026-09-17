import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from app.fusion_runtime import FusionRuntime, _normalize_stage_output
from app.models import JobSubmitRequest


class FakeBackend:
    def __init__(self):
        self.objects = {}

    async def storage_put(self, path, data, content_type="application/json", upsert=True):
        self.objects[path] = data
        return path

    async def storage_get(self, path):
        return self.objects[path]

    async def storage_get_json(self, path):
        import json
        return json.loads(self.objects[path].decode("utf-8"))


class FakeRepo:
    def __init__(self):
        self.corpora = {}
        self.materials = []

    async def get_fusion_corpus(self, corpus_id, **kwargs):
        return self.corpora.get(corpus_id)

    async def create_fusion_corpus(self, row):
        self.corpora[row["id"]] = row
        return row

    async def create_fusion_material(self, row):
        self.materials = [x for x in self.materials if x.get("id") != row.get("id")]
        self.materials.append(row)
        return row

    async def update_fusion_corpus(self, corpus_id, values):
        self.corpora[corpus_id].update(values)
        return self.corpora[corpus_id]

    def default_job_expiry(self):
        return datetime.now(timezone.utc) + timedelta(days=7)


class FusionContractTests(unittest.TestCase):
    def test_current_dsl_corpus_contract_is_accepted(self):
        req = JobSubmitRequest(
            tenant_id="tenant",
            conversation_hash="hash",
            stage="fusion_corpus_ingest",
            provider="gemini",
            model="gemini-3.1-flash-lite",
            think_level="medium",
            mode="new_fusion_corpus",
            fusion_corpus_id="fcor_test",
            payload={"materials": [{"material_id": "MAT01"}]},
        )
        self.assertEqual(req.mode, "new_fusion_corpus")
        self.assertIsNone(req.current_query)

    def test_fusion_model_stage_does_not_require_current_query(self):
        req = JobSubmitRequest(
            tenant_id="tenant",
            conversation_hash="hash",
            stage="global_adjudication",
            provider="gemini",
            model="gemini-3.1-flash-lite",
            fusion_corpus_id="fcor_test",
        )
        self.assertEqual(req.mode, "stateless")

    def test_normal_stage_still_requires_current_query(self):
        with self.assertRaises(Exception):
            JobSubmitRequest(
                tenant_id="tenant",
                conversation_hash="hash",
                stage="normal_inference",
                provider="grok",
                model="grok-4.6",
            )

    def test_corpus_ingest_is_immutable_and_idempotent(self):
        backend = FakeBackend()
        repo = FakeRepo()
        runtime = FusionRuntime(backend, repo, SimpleNamespace(), SimpleNamespace())
        job = {
            "tenant_id": "tenant",
            "conversation_hash": "conv",
            "stage": "fusion_corpus_ingest",
        }
        snapshot = {
            "fusion_corpus_id": "fcor_test",
            "payload": {
                "corpus_version": 1,
                "business_question": "q",
                "materials": [
                    {
                        "material_id": "MAT01",
                        "role": "candidate_answer",
                        "candidate_id": "A",
                        "filename": "a.md",
                        "full_text": "[a.md#B90001] hello",
                        "parse_status": "complete",
                    }
                ],
            },
        }
        first = asyncio.run(runtime.execute(job, snapshot))
        second = asyncio.run(runtime.execute(job, snapshot))
        self.assertEqual(first.payload["fusion_corpus_id"], "fcor_test")
        self.assertEqual(first.payload["candidate_count"], 1)
        self.assertTrue(second.payload["idempotent_reuse"])


    def test_global_adjudication_singleton_container_normalization(self):
        obj = {
            "candidate_overview": [
                {
                    "candidate_id": "A",
                    "material_alignment": {"alignment_id": "A-MA01"},
                    "evidence_coverage": {"evidence_id": "A-EV01"},
                    "proposal_compatibility": {"compatibility_id": "A-PC01"},
                }
            ],
            "resolution_evidence_registry": {"resolution_evidence_id": "RE01"},
            "conflicts": {"conflict_id": "C01"},
        }
        normalized, notes = _normalize_stage_output("global_adjudication", obj)
        candidate = normalized["candidate_overview"][0]
        self.assertIsInstance(candidate["material_alignment"], list)
        self.assertIsInstance(candidate["evidence_coverage"], list)
        self.assertIsInstance(candidate["proposal_compatibility"], list)
        self.assertIsInstance(normalized["resolution_evidence_registry"], list)
        self.assertIsInstance(normalized["conflicts"], list)
        self.assertTrue(any("material_alignment" in x for x in notes))

    def test_fusion_provider_payload_does_not_encode_provider_wire_format(self):
        runtime = FusionRuntime(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
        payload = runtime._provider_payload("global_adjudication", {})
        self.assertEqual(payload, {"temperature": 0})
        self.assertNotIn("text", payload)
        self.assertNotIn("response_format", payload)

    def test_corpus_hash_ignores_volatile_signed_urls(self):
        backend = FakeBackend()
        repo = FakeRepo()
        runtime = FusionRuntime(backend, repo, SimpleNamespace(), SimpleNamespace())
        job = {"tenant_id": "tenant", "conversation_hash": "conv", "stage": "fusion_corpus_ingest"}
        base = {
            "fusion_corpus_id": "fcor_url",
            "payload": {
                "corpus_version": 1,
                "business_question": "q",
                "materials": [{
                    "material_id": "MAT01",
                    "role": "candidate_answer",
                    "candidate_id": "A",
                    "filename": "a.md",
                    "full_text": "hello",
                    "original_object_path": "fusion/x/a.md",
                    "original_signed_url": "https://example/a?token=one",
                    "projection": {"ordered_items": [], "transport": {"visual_url": "one"}},
                }],
            },
        }
        first = asyncio.run(runtime.execute(job, base))
        second_req = {**base, "payload": {**base["payload"], "materials": [dict(base["payload"]["materials"][0], original_signed_url="https://example/a?token=two", projection={"ordered_items": [], "transport": {"visual_url": "two"}})]}}
        second = asyncio.run(runtime.execute(job, second_req))
        self.assertEqual(first.payload["corpus_hash"], second.payload["corpus_hash"])
        self.assertTrue(second.payload["idempotent_reuse"])


if __name__ == "__main__":
    unittest.main()
