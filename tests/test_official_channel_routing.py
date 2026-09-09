from types import SimpleNamespace
import unittest

from app.providers.base import ProviderRequestError
from app.providers.openai_compatible import OpenAICompatibleResponsesProvider


class OfficialChannelRoutingTests(unittest.TestCase):
    def _provider(self, *, model_channels=None, provider_channels=None):
        settings = SimpleNamespace(
            aihubmix_root="https://aihubmix.com/v1",
            aihubmix_official_model_channels=model_channels or {},
            aihubmix_official_provider_channels=provider_channels or {},
        )
        return OpenAICompatibleResponsesProvider(settings)

    def test_exact_model_channel_wins_over_provider_channel(self):
        provider = self._provider(
            model_channels={"grok-4.6": 101},
            provider_channels={"grok": 202},
        )
        self.assertEqual(provider._official_channel_id("grok", "grok-4.6"), 101)

    def test_provider_channel_is_used_for_unlisted_model(self):
        provider = self._provider(provider_channels={"gemini": 303})
        self.assertEqual(
            provider._official_channel_id("gemini", "gemini-3.6-flash"),
            303,
        )

    def test_missing_official_channel_fails_closed(self):
        provider = self._provider()
        with self.assertRaises(ProviderRequestError) as ctx:
            provider._official_channel_id("grok", "grok-4.6")
        self.assertEqual(ctx.exception.code, "OFFICIAL_UPSTREAM_CHANNEL_NOT_CONFIGURED")

    def test_non_aihubmix_upstream_override_is_rejected(self):
        provider = self._provider(provider_channels={"grok": 101})
        with self.assertRaises(ProviderRequestError) as ctx:
            provider._validate_upstream_hint(
                {"upstream": {"base_url": "https://example.invalid/v1"}}
            )
        self.assertEqual(ctx.exception.code, "UPSTREAM_OVERRIDE_FORBIDDEN")

    def test_same_aihubmix_base_hint_is_allowed(self):
        provider = self._provider(provider_channels={"grok": 101})
        provider._validate_upstream_hint(
            {"upstream": {"base_url": "https://aihubmix.com/v1/"}}
        )


if __name__ == "__main__":
    unittest.main()
