import gzip
import unittest

from app.config import Settings
from app.models_v2 import extract_material_ids
from app.providers.base import ProviderHTTPError
from app.providers.moonshot_chat import MoonshotChatCompletionsAdapter
from app.providers.registry import ProviderRegistry
from app.providers.v2_common import decode_http_body
from app.v2_utils import request_fingerprint, scoped_job_idempotency_key


def settings(**kwargs):
    base = dict(
        relay_api_token="relay-token",
        supabase_url="https://example.supabase.co",
        supabase_secret_key="sb_secret_test",
        moonshot_api_key="moonshot-key",
        aihubmix_api_key="aihubmix-key",
        gemini_api_key="gemini-key",
    )
    base.update(kwargs)
    return Settings(**base)


class V2ContractTests(unittest.TestCase):
    def test_material_refs_are_extracted_in_stable_unique_order(self):
        context = [
            {"role": "user", "content": [
                {"type": "material_ref", "material_id": "mat_b"},
                {"type": "text", "text": "x"},
                {"type": "material_ref", "material_id": "mat_a"},
                {"type": "material_ref", "material_id": "mat_b"},
            ]}
        ]
        self.assertEqual(extract_material_ids(context), ["mat_b", "mat_a"])

    def test_request_fingerprint_is_canonical_but_content_sensitive(self):
        a = request_fingerprint({"x": 1, "y": [2, 3]})
        b = request_fingerprint({"y": [2, 3], "x": 1})
        c = request_fingerprint({"x": 1, "y": [3, 2]})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(scoped_job_idempotency_key("session_job:s1", "K1").startswith("v2:"))
        self.assertNotEqual(
            scoped_job_idempotency_key("session_job:s1", "K1"),
            scoped_job_idempotency_key("session_job:s2", "K1"),
        )

    def test_v2_registry_is_exact_and_has_no_unknown_fallback(self):
        registry = ProviderRegistry(settings())
        profile = registry.get_profile("moonshot-official-chat")
        self.assertEqual(profile.provider, "moonshot")
        self.assertEqual(profile.protocol, "chat_completions")
        self.assertIsInstance(registry.get_v2("moonshot-official-chat"), MoonshotChatCompletionsAdapter)
        with self.assertRaises(KeyError):
            registry.get_v2("does-not-exist")
        with self.assertRaises(ValueError):
            registry.validate_session_model("moonshot-official-chat", "grok", "kimi-k3")

    def test_unconfigured_provider_profile_is_disabled(self):
        registry = ProviderRegistry(settings(aihubmix_api_key=None, gemini_api_key=None))
        with self.assertRaises(KeyError):
            registry.get_profile("grok-aihubmix-responses")
        with self.assertRaises(KeyError):
            registry.get_profile("gemini-native")
        self.assertEqual(registry.get_profile("moonshot-official-chat").provider, "moonshot")

    def test_kimi_reasoning_mapping_is_model_specific(self):
        registry = ProviderRegistry(settings())
        adapter = registry.get_v2("moonshot-official-chat")
        payload = {}
        applied = adapter._apply_generation(payload, {"reasoning": {"effort": "max"}}, "kimi-k3")
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(applied["reasoning_effort"], "max")
        with self.assertRaises(Exception):
            adapter._apply_generation({}, {"reasoning": {"effort": "high"}}, "kimi-k2.6")

    def test_raw_http_error_bytes_and_compression_are_lossless(self):
        original = "中文错误-body".encode("utf-8")
        compressed = gzip.compress(original)
        exc = ProviderHTTPError(
            429,
            compressed,
            headers=[("x-a", "1"), ("x-a", "2")],
            content_type="application/json",
            content_encoding="gzip",
        )
        self.assertEqual(exc.body, compressed)
        self.assertEqual(exc.headers, [("x-a", "1"), ("x-a", "2")])
        self.assertEqual(decode_http_body(exc.body, exc.content_encoding), original)


if __name__ == "__main__":
    unittest.main()
