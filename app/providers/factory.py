from __future__ import annotations

from ..config import Settings
from ..control_plane import ModelControlPlane
from ..materials.resolver import MaterialResolver
from ..v2_repository import RelayV2Repository
from .chat_completions import ChatCompletionsV2Adapter
from .claude_messages import ClaudeMessagesV2Adapter
from .openai_compatible import OpenAICompatibleResponsesProvider
from .registry import ProviderRegistry
from .responses_v2 import ResponsesV2Adapter


def register_protocol_adapters(
    *,
    settings: Settings,
    control_plane: ModelControlPlane,
    providers: ProviderRegistry,
    materials: MaterialResolver,
    repo: RelayV2Repository,
) -> None:
    """Register every configured reusable protocol connection in one place.

    API and Worker call this same function so a published connection/protocol
    snapshot cannot be interpreted differently merely because the process role is
    different. Provider-native connections that need bespoke file/runtime state
    (Gemini Native and Moonshot official) remain registered by their existing
    integration code.
    """

    del repo  # Reserved for protocol adapters that need repository-backed state.
    for connection_id in sorted(control_plane.connections):
        spec = control_plane.connection(connection_id)
        if spec is None or spec.protocol not in {"responses", "chat_completions", "claude_messages"}:
            continue
        if not settings.connection_is_enabled(connection_id):
            continue
        configured, _ = control_plane.connection_configuration(connection_id, settings)
        if not configured:
            continue
        api_key = control_plane.credential(connection_id, settings)
        if not api_key or not spec.base_url:
            continue

        if spec.protocol == "responses":
            client = OpenAICompatibleResponsesProvider(
                settings,
                base_url=spec.base_url,
                api_key=api_key,
                connection_id=connection_id,
                channel_id=spec.channel_id,
            )
            adapter = ResponsesV2Adapter(client, materials)
        elif spec.protocol == "chat_completions":
            adapter = ChatCompletionsV2Adapter(
                settings,
                materials,
                base_url=spec.base_url,
                api_key=api_key,
                connection_id=connection_id,
                channel_id=spec.channel_id,
            )
        else:
            adapter = ClaudeMessagesV2Adapter(
                settings,
                materials,
                base_url=spec.base_url,
                api_key=api_key,
                connection_id=connection_id,
                channel_id=spec.channel_id,
            )

        providers.register_v2(
            connection_id,
            adapter,
            provider=None,
            protocol=spec.protocol,
            channel_id=spec.channel_id,
        )
