from ..config import Settings
from .openai_compatible import OpenAICompatibleResponsesProvider


class ProviderRegistry:
    """Provider registry.

    The initial implementation routes AIHubMix/OpenAI-compatible providers through
    one adapter. Add dedicated adapters here later without changing the Job API,
    database schema or Railway topology.
    """

    def __init__(self, settings: Settings) -> None:
        self.openai_compatible = OpenAICompatibleResponsesProvider(settings)

    def get(self, provider: str):
        # Current deployment uses AIHubMix /responses. Provider-specific behavior
        # belongs in adapters, not in relay_jobs/relay_sessions naming.
        return self.openai_compatible
