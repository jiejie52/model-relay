"""One connection assembly path for API and Worker; implementation types are allowlisted."""
import json
from app.control_plane.connections import ConnectionRuntime, resolve_credential
from app.runtime.connection_scope import ScopedAdapter
from app.protocol_adapters.profiles.chat_completions import ChatCompletionsV2Adapter
from app.protocol_adapters.profiles.claude_messages import ClaudeMessagesV2Adapter
from app.protocol_adapters.profiles.openai_compatible import OpenAICompatibleResponsesProvider
from app.protocol_adapters.profiles.responses_v2 import ResponsesV2Adapter
from app.protocol_adapters.profiles.gemini_native import GeminiNativeAdapter
from app.protocol_adapters.profiles.gemini_genai_sdk import GeminiGenAIClient
from app.protocol_adapters.profiles.moonshot_chat import MoonshotChatAdapter
from app.materials.provider_files.gemini_files import GeminiFileAdapter
from app.materials.provider_files.kimi_official import KimiOfficialFileAdapter
from app.runtime.resource_adapters.gemini_stateful_cache import GeminiStatefulCacheAdapter

IMPLEMENTED_PROTOCOLS = frozenset({"responses", "chat_completions", "claude_messages", "gemini_native", "moonshot_chat"})
RESOURCE_MECHANISMS = {"gemini_native": {"stateful_resource", "implicit_prefix"},
    "claude_messages": {"breakpoint", "implicit_prefix"}, "responses": {"implicit_prefix"},
    "chat_completions": {"implicit_prefix"}, "moonshot_chat": {"implicit_prefix"}}

class ConnectionAssembly:
    def __init__(self): self.clients, self.connections = [], []
    async def close(self):
        for client in self.clients:
            await client.close()
        for connection in self.connections:
            await connection.pool.aclose()

def register_protocol_adapters(*, settings, control_plane, providers, materials, repo,
                               file_adapters=None, cache_resources=None, storage=None, retained_control_planes=None):
    from app.control_plane.definitions import ModelControlPlane
    assembly = ConnectionAssembly()
    planes = [ModelControlPlane._builtin(settings), *(retained_control_planes or [])] if retained_control_planes is not None else []
    # Retained releases are read-only, pinned, and never become routing catalogs.
    retained_releases = json.loads(getattr(settings, "retained_control_plane_files", "[]"))
    pointer = getattr(settings, "model_control_plane_release_pointer", None)
    if pointer:
        from pathlib import Path
        retained_releases += json.loads(Path(pointer).read_text()).get("retained", [])
    retained_releases = list({item["sha256"]: item for item in retained_releases}.values())
    for release in retained_releases:
        copy = settings.model_copy(update={"model_control_plane_json": None, "model_control_plane_release_pointer": None,
            "model_control_plane_file": release["path"], "model_control_plane_file_sha256": release["sha256"]})
        planes.append(ModelControlPlane.from_settings(copy))
    planes.append(control_plane)
    seen = {}
    for plane in planes:
        variants = []
        for cid, spec in sorted(plane.connections.items()):
            supplies = [item for item in plane.offerings.values() if item.connection_id == cid and item.enabled]
            groups = {}
            for supply in supplies:
                groups.setdefault(supply.protocol_profile_id, []).append(supply)
            for profile_id, group_supplies in groups.items():
                profile_object = plane.protocol_profile(profile_id)
                variants.append((cid, spec, [profile_object] if profile_object else [], group_supplies))
        for cid, spec, profiles, supplies in variants:
            if not settings.connection_is_enabled(cid): continue
            secret = resolve_credential(spec, settings)
            if not secret or not (spec.rest_base_url or spec.base_url): continue
            profile = profiles[0].canonical() if profiles else {}
            from app.core.idempotency import stable_hash
            runtime_key = (cid, spec.identity_hash, stable_hash(profile))
            runtime = seen.get(runtime_key)
            if runtime is None:
                runtime = ConnectionRuntime(spec, settings, secret, profile)
                seen[runtime_key] = runtime
                assembly.connections.append(runtime)
            kwargs = dict(base_url=runtime.rest_base, api_key=secret, connection_id=cid, channel_id=spec.channel_id)
            sdk = None
            if spec.protocol == "responses":
                adapter = ResponsesV2Adapter(OpenAICompatibleResponsesProvider(runtime.settings, **kwargs), materials)
            elif spec.protocol == "chat_completions": adapter = ChatCompletionsV2Adapter(runtime.settings, materials, **kwargs)
            elif spec.protocol == "claude_messages": adapter = ClaudeMessagesV2Adapter(runtime.settings, materials, **kwargs)
            elif spec.protocol == "gemini_native":
                adapter = GeminiNativeAdapter(runtime.settings, connection=runtime)
                if not spec.credential_ref: adapter.adapter_version = "gemini-native-aihubmix/8"
                sdk = GeminiGenAIClient(runtime.settings, connection=runtime)
                assembly.clients.append(sdk)
            elif spec.protocol == "moonshot_chat": adapter = MoonshotChatAdapter(runtime.settings, materials, repo, connection=runtime)
            else: raise ValueError("Unregistered protocol implementation")
            providers.register_v2(cid, ScopedAdapter(adapter, runtime), provider=None,
                protocol=spec.protocol, channel_id=spec.channel_id)
            contracts = [plane.contract(s.capability_contract_id) for s in supplies]
            need_files = any(s.requires_file_adapter for s in supplies) or any(
                "provider_files_api" in c.metadata.get("material_transport", {}).get("supported_material_transports", []) for c in contracts)
            if file_adapters is not None and need_files:
                if spec.protocol == "gemini_native": resource = GeminiFileAdapter(runtime.settings, connection=runtime, sdk=sdk)
                elif spec.protocol == "moonshot_chat": resource = KimiOfficialFileAdapter(runtime.settings, repo, storage, connection=runtime)
                else: raise ValueError("No file implementation for this connection")
                if not spec.credential_ref and spec.protocol == "gemini_native": resource.adapter_version = "gemini-aihubmix-files/2"
                file_adapters.register(cid, ScopedAdapter(resource, runtime))
            need_cache = any("stateful_resource" in c.cache.get("supported_mechanisms", []) for c in contracts)
            need_count = any(p.cache.get("token_count_connection_id") == cid for p in profiles)
            if cache_resources is not None and (need_cache or need_count):
                if spec.protocol != "gemini_native": raise ValueError("Stateful resource implementation unavailable")
                cache_resources.register(cid, ScopedAdapter(GeminiStatefulCacheAdapter(runtime.settings, connection=runtime, sdk=sdk), runtime))
    providers.connection_assembly = assembly
    return assembly
