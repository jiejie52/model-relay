from types import SimpleNamespace
import unittest

from app.model_options import (
    CAPABILITY_PROFILE_REVISION,
    ModelOptionError,
    resolve_model_options,
)
from app.providers.gemini_native import GeminiNativeAdapter
from app.providers.moonshot_chat import MoonshotChatAdapter
from app.providers.openai_compatible import OpenAICompatibleResponsesProvider


class CanonicalModelOptionsTests(unittest.TestCase):
    def test_temperature_is_canonicalized_for_gemini(self):
        resolved = resolve_model_options(
            provider="gemini",
            model="gemini-3.1-flash-lite",
            options={"temperature": 0},
        )
        self.assertEqual(resolved.effective_options, {"temperature": 0.0})
        self.assertEqual(resolved.capability_revision, CAPABILITY_PROFILE_REVISION)

    def test_legacy_provider_payload_temperature_migrates_to_options(self):
        resolved = resolve_model_options(
            provider="gemini",
            model="gemini-3.1-flash-lite",
            options={},
            provider_payload={"temperature": 0.08},
        )
        self.assertEqual(resolved.effective_options, {"temperature": 0.08})
        self.assertTrue(any("deprecated" in item for item in resolved.warnings))

    def test_conflicting_legacy_and_canonical_option_fails_closed(self):
        with self.assertRaises(ModelOptionError) as ctx:
            resolve_model_options(
                provider="grok",
                model="grok-4.6",
                options={"temperature": 0.1},
                provider_payload={"temperature": 0.2},
            )
        self.assertEqual(ctx.exception.code, "OPTION_CONFLICT")

    def test_unknown_legacy_wire_field_is_rejected(self):
        with self.assertRaises(ModelOptionError) as ctx:
            resolve_model_options(
                provider="gemini",
                model="gemini-3.1-flash-lite",
                options={},
                provider_payload={"arbitrary_provider_field": True},
            )
        self.assertEqual(ctx.exception.code, "LEGACY_PROVIDER_PAYLOAD_UNSUPPORTED")

    def test_unknown_canonical_option_is_rejected(self):
        with self.assertRaises(ModelOptionError) as ctx:
            resolve_model_options(
                provider="kimi",
                model="kimi-k2",
                options={"unknown_knob": 1},
            )
        self.assertEqual(ctx.exception.code, "OPTION_UNSUPPORTED")

    def test_gemini_v22_projects_temperature_into_generation_config(self):
        payload = {"contents": []}
        GeminiNativeAdapter._apply_model_options(
            payload,
            {
                "schema_version": "relay-request/2.2",
                "think_level": "low",
                "effective_options": {
                    "temperature": 0.08,
                    "top_p": 0.9,
                    "max_output_tokens": 4096,
                },
            },
        )
        self.assertNotIn("temperature", payload)
        self.assertEqual(
            payload["generationConfig"],
            {
                "temperature": 0.08,
                "topP": 0.9,
                "maxOutputTokens": 4096,
                "thinkingConfig": {"thinkingLevel": "LOW"},
            },
        )

    def test_grok_46_allows_xhigh_but_45_rejects_it(self):
        supported = resolve_model_options(
            provider="grok",
            model="grok-4.6",
            options={},
            think_level="xhigh",
        )
        self.assertEqual(supported.effective_think_level, "xhigh")
        with self.assertRaises(ModelOptionError) as ctx:
            resolve_model_options(
                provider="grok",
                model="grok-4.5",
                options={},
                think_level="xhigh",
            )
        self.assertEqual(ctx.exception.code, "THINK_LEVEL_UNSUPPORTED")

    def test_kimi_non_auto_think_level_fails_closed(self):
        with self.assertRaises(ModelOptionError) as ctx:
            resolve_model_options(
                provider="kimi",
                model="kimi-k2",
                options={},
                think_level="high",
            )
        self.assertEqual(ctx.exception.code, "THINK_LEVEL_UNSUPPORTED")

    def test_gemini_35_and_36_allow_canonical_temperature(self):
        for model in ("gemini-3.5-flash-lite", "gemini-3.6-flash"):
            with self.subTest(model=model):
                resolved = resolve_model_options(
                    provider="gemini",
                    model=model,
                    options={"temperature": 0.2, "max_output_tokens": 2048},
                    think_level="medium",
                )
                self.assertEqual(
                    resolved.effective_options,
                    {"max_output_tokens": 2048, "temperature": 0.2},
                )

    def test_gemini_35_and_36_top_p_remains_fail_closed(self):
        for model in ("gemini-3.5-flash-lite", "gemini-3.6-flash"):
            with self.subTest(model=model):
                with self.assertRaises(ModelOptionError) as ctx:
                    resolve_model_options(
                        provider="gemini",
                        model=model,
                        options={"top_p": 0.9},
                        think_level="medium",
                    )
                self.assertEqual(ctx.exception.code, "OPTION_UNSUPPORTED")

    def test_gemini_v21_temperature_resume_is_translated_not_top_level(self):
        payload = {"contents": []}
        GeminiNativeAdapter._apply_model_options(
            payload,
            {
                "schema_version": "relay-request/2.1",
                "provider_payload": {"temperature": 0},
            },
        )
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["generationConfig"]["temperature"], 0)

    def test_responses_v22_projects_canonical_options(self):
        payload = {"model": "grok-4.6", "input": []}
        OpenAICompatibleResponsesProvider._apply_model_options(
            payload,
            {
                "schema_version": "relay-request/2.2",
                "effective_options": {
                    "temperature": 0.1,
                    "top_p": 0.95,
                    "max_output_tokens": 6000,
                },
            },
        )
        self.assertEqual(payload["temperature"], 0.1)
        self.assertEqual(payload["top_p"], 0.95)
        self.assertEqual(payload["max_output_tokens"], 6000)

    def test_moonshot_v22_maps_max_output_tokens_to_native_max_tokens(self):
        payload = {"model": "kimi-k2", "messages": []}
        MoonshotChatAdapter._apply_model_options(
            payload,
            {
                "schema_version": "relay-request/2.2",
                "effective_options": {"temperature": 0.2, "max_output_tokens": 2048},
            },
        )
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertNotIn("max_output_tokens", payload)


if __name__ == "__main__":
    unittest.main()
