from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import hashlib
import json
import os
from typing import Any, Iterable

from .config import Settings


CONTROL_PLANE_SCHEMA_VERSION = "relay-model-control-plane/3.0"


@dataclass(frozen=True)
class ConnectionSpec:
    connection_id: str
    channel_id: str
    protocol: str
    base_url: str | None = None
    credential_env: str | None = None
    account_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "channel_id": self.channel_id,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "credential_env": self.credential_env,
            "account_id": self.account_id,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class CapabilityContract:
    contract_id: str
    revision: str
    supported_options: dict[str, dict[str, Any]]
    thinking: dict[str, Any]
    structured_output: dict[str, Any]
    input_modalities: tuple[str, ...] = ("text",)
    features: tuple[str, ...] = ()
    provider_defaults: dict[str, Any] = field(default_factory=dict)
    # Cache capability is part of the Supply capability contract. It declares
    # what the exact model/channel offering may do; protocol projection and
    # policy remain separate frozen objects.
    cache: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "revision": self.revision,
            "supported_options": self.supported_options,
            "thinking": self.thinking,
            "structured_output": self.structured_output,
            "input_modalities": list(self.input_modalities),
            "features": list(self.features),
            "provider_defaults": self.provider_defaults,
            "cache": self.cache,
            "metadata": self.metadata,
        }

    @property
    def contract_hash(self) -> str:
        return _hash_json(self.canonical())

    def supports_requirements(self, requirements: dict[str, Any] | None) -> tuple[bool, str | None]:
        req = requirements or {}
        required_modalities = {
            str(x).strip().lower()
            for x in (req.get("input_modalities") or [])
            if str(x).strip()
        }
        available_modalities = {x.lower() for x in self.input_modalities}
        missing_modalities = sorted(required_modalities - available_modalities)
        if missing_modalities:
            return False, "unsupported_input_modalities:" + ",".join(missing_modalities)

        required_features = {
            str(x).strip().lower()
            for x in (req.get("required_features") or [])
            if str(x).strip()
        }
        available_features = {x.lower() for x in self.features}
        missing_features = sorted(required_features - available_features)
        if missing_features:
            return False, "unsupported_features:" + ",".join(missing_features)

        required_levels = {
            str(x).strip().lower()
            for x in (req.get("think_levels") or [])
            if str(x).strip()
        }
        accepted_levels = {
            str(x).strip().lower()
            for x in (self.thinking.get("accepted_levels") or [])
            if str(x).strip()
        }
        missing_levels = sorted(required_levels - accepted_levels)
        if missing_levels:
            return False, "unsupported_think_levels:" + ",".join(missing_levels)

        requested_structured = str(req.get("structured_output") or "none").strip().lower()
        mode = str(self.structured_output.get("mode") or "none").strip().lower()
        if requested_structured == "native" and mode not in {"native", "native_json_schema"}:
            return False, f"structured_output_native_required:{mode}"
        if requested_structured == "post_validate" and mode == "none":
            return False, "structured_output_unavailable"
        return True, None


@dataclass(frozen=True)
class ProtocolProfileSpec:
    profile_id: str
    revision: str
    protocol: str
    cache: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "revision": self.revision,
            "protocol": self.protocol,
            "cache": self.cache,
            "metadata": self.metadata,
        }

    @property
    def profile_hash(self) -> str:
        return _hash_json(self.canonical())


@dataclass(frozen=True)
class CachePolicySpec:
    policy_id: str
    revision: str
    scope: str = "session"
    mechanism_preference: tuple[str, ...] = ()
    auto_prepare_failure: str = "uncached_same_context"
    on_prepare_failure: str = "fail"
    cross_session_sharing: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "revision": self.revision,
            "scope": self.scope,
            "mechanism_preference": list(self.mechanism_preference),
            "auto_prepare_failure": self.auto_prepare_failure,
            "on_prepare_failure": self.on_prepare_failure,
            "cross_session_sharing": self.cross_session_sharing,
            "metadata": self.metadata,
        }

    @property
    def policy_hash(self) -> str:
        return _hash_json(self.canonical())


@dataclass(frozen=True)
class ModelOffering:
    offering_id: str
    provider: str
    model_pattern: str
    connection_id: str
    capability_contract_id: str
    protocol_profile_id: str | None = None
    cache_policy_id: str | None = None
    priority: int = 100
    deployment_id: str | None = None
    requires_file_adapter: bool = False
    observed_model_policy: str = "audit"  # ignore | audit | strict
    enabled: bool = True
    quota: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(self, *, provider: str, model: str, deployment_id: str) -> bool:
        if not self.enabled:
            return False
        if self.provider.lower() != provider.lower():
            return False
        if self.deployment_id not in (None, "*", deployment_id):
            return False
        return fnmatchcase(model.lower(), self.model_pattern.lower())

    def canonical(self) -> dict[str, Any]:
        return {
            "offering_id": self.offering_id,
            "provider": self.provider,
            "model_pattern": self.model_pattern,
            "connection_id": self.connection_id,
            "capability_contract_id": self.capability_contract_id,
            "protocol_profile_id": self.protocol_profile_id,
            "cache_policy_id": self.cache_policy_id,
            "priority": self.priority,
            "deployment_id": self.deployment_id,
            "requires_file_adapter": self.requires_file_adapter,
            "observed_model_policy": self.observed_model_policy,
            "enabled": self.enabled,
            "quota": self.quota,
            "metadata": self.metadata,
        }


class ModelControlPlane:
    """Immutable runtime snapshot for model/channel/capability governance.

    Secrets are never accepted in MODEL_CONTROL_PLANE_JSON. A connection may
    reference a credential by environment variable name. API and Worker build the
    same snapshot, hash it, and freeze the selected offering/contract into Session
    metadata so later config releases cannot silently rewrite an accepted Session.
    """

    def __init__(
        self,
        *,
        revision: str,
        connections: Iterable[ConnectionSpec],
        capability_contracts: Iterable[CapabilityContract],
        protocol_profiles: Iterable[ProtocolProfileSpec],
        cache_policies: Iterable[CachePolicySpec],
        offerings: Iterable[ModelOffering],
        status: str = "published",
        release_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.revision = str(revision)
        self.status = str(status).strip().lower()
        self.release_metadata = dict(release_metadata or {})
        self.connections = {x.connection_id: x for x in connections}
        self.capability_contracts = {x.contract_id: x for x in capability_contracts}
        self.protocol_profiles = {x.profile_id: x for x in protocol_profiles}
        self.cache_policies = {x.policy_id: x for x in cache_policies}
        self.offerings = {x.offering_id: x for x in offerings}
        self._validate()
        canonical = {
            "schema_version": CONTROL_PLANE_SCHEMA_VERSION,
            "revision": self.revision,
            "status": self.status,
            "release_metadata": self.release_metadata,
            "connections": [self.connections[k].canonical() for k in sorted(self.connections)],
            "capability_contracts": [
                self.capability_contracts[k].canonical() for k in sorted(self.capability_contracts)
            ],
            "protocol_profiles": [
                self.protocol_profiles[k].canonical() for k in sorted(self.protocol_profiles)
            ],
            "cache_policies": [
                self.cache_policies[k].canonical() for k in sorted(self.cache_policies)
            ],
            "offerings": [self.offerings[k].canonical() for k in sorted(self.offerings)],
        }
        self.control_plane_hash = _hash_json(canonical)

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelControlPlane":
        base = cls._builtin(settings)
        raw = str(getattr(settings, "model_control_plane_json", None) or "").strip()
        if not raw:
            return base
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("MODEL_CONTROL_PLANE_JSON must be a JSON object")
        status = str(parsed.get("status") or "").strip().lower()
        if status != "published":
            raise ValueError(
                "MODEL_CONTROL_PLANE_JSON must declare status=published; draft/review snapshots cannot enter runtime"
            )
        mode = str(parsed.get("mode") or "merge").strip().lower()
        if mode not in {"merge", "replace"}:
            raise ValueError("MODEL_CONTROL_PLANE_JSON mode must be merge or replace")

        if mode == "replace":
            connections: dict[str, ConnectionSpec] = {}
            contracts: dict[str, CapabilityContract] = {}
            profiles: dict[str, ProtocolProfileSpec] = {}
            policies: dict[str, CachePolicySpec] = {}
            offerings: dict[str, ModelOffering] = {}
        else:
            connections = dict(base.connections)
            contracts = dict(base.capability_contracts)
            profiles = dict(base.protocol_profiles)
            policies = dict(base.cache_policies)
            offerings = dict(base.offerings)

        for value in parsed.get("connections") or []:
            spec = cls._connection_from_mapping(value)
            connections[spec.connection_id] = spec
        for value in parsed.get("capability_contracts") or []:
            contract = cls._contract_from_mapping(value)
            contracts[contract.contract_id] = contract
        for value in parsed.get("protocol_profiles") or []:
            profile = cls._profile_from_mapping(value)
            profiles[profile.profile_id] = profile
        for value in parsed.get("cache_policies") or []:
            policy = cls._cache_policy_from_mapping(value)
            policies[policy.policy_id] = policy
        for value in parsed.get("offerings") or []:
            offering = cls._offering_from_mapping(value, settings.deployment_id)
            offerings[offering.offering_id] = offering

        revision = str(parsed.get("revision") or settings.model_control_plane_revision)
        return cls(
            revision=revision,
            connections=connections.values(),
            capability_contracts=contracts.values(),
            protocol_profiles=profiles.values(),
            cache_policies=policies.values(),
            offerings=offerings.values(),
            status=status,
            release_metadata=dict(parsed.get("release_metadata") or {}),
        )

    @classmethod
    def _builtin(cls, settings: Settings) -> "ModelControlPlane":
        deployment = settings.deployment_id
        connections = [
            ConnectionSpec(
                connection_id="aihubmix_default",
                channel_id="aihubmix",
                protocol="responses",
                base_url=settings.aihubmix_openai_base_url.rstrip("/"),
                account_id="aihubmix_default",
            ),
            ConnectionSpec(
                connection_id=settings.aihubmix_chat_connection_id,
                channel_id="aihubmix",
                protocol="chat_completions",
                base_url=settings.aihubmix_openai_base_url.rstrip("/"),
                account_id="aihubmix_default",
            ),
            ConnectionSpec(
                connection_id=settings.aihubmix_claude_connection_id,
                channel_id="aihubmix",
                protocol="claude_messages",
                base_url=settings.aihubmix_claude_base_url.rstrip("/"),
                account_id="aihubmix_default",
            ),
            ConnectionSpec(
                connection_id=settings.aihubmix_gemini_connection_id,
                channel_id="aihubmix",
                protocol="gemini_native",
                base_url=(settings.aihubmix_gemini_base_url.rstrip("/") if settings.aihubmix_gemini_base_url else None),
                account_id="aihubmix_default",
            ),
            ConnectionSpec(
                connection_id=settings.moonshot_connection_id,
                channel_id="moonshot_official",
                protocol="moonshot_chat",
                base_url=settings.moonshot_base_url.rstrip("/"),
                account_id="moonshot_default",
            ),
        ]

        contracts = _builtin_contracts()
        protocol_profiles = _builtin_protocol_profiles()
        cache_policies = _builtin_cache_policies()
        offerings = [
            # Existing routes remain available and keep their prior semantics.
            ModelOffering(
                offering_id="legacy-gemini-aihubmix-native",
                provider="gemini",
                model_pattern=settings.route_gemini_model_pattern,
                connection_id=settings.aihubmix_gemini_connection_id,
                capability_contract_id="gemini-legacy-native",
                protocol_profile_id="gemini-native-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=100,
                deployment_id=deployment,
                requires_file_adapter=True,
            ),
            ModelOffering(
                offering_id="legacy-grok-aihubmix-responses",
                provider="grok",
                model_pattern=settings.route_grok_model_pattern,
                connection_id="aihubmix_default",
                capability_contract_id="grok-legacy-responses",
                protocol_profile_id="responses-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=100,
                deployment_id=deployment,
            ),
            ModelOffering(
                offering_id="legacy-kimi-moonshot",
                provider="kimi",
                model_pattern=settings.route_kimi_model_pattern,
                connection_id=settings.moonshot_connection_id,
                capability_contract_id="kimi-legacy-moonshot",
                protocol_profile_id="moonshot-chat-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=100,
                deployment_id=deployment,
                requires_file_adapter=True,
            ),
            # AIHubMix multi-model rollout. Exact entries outrank generic legacy patterns.
            ModelOffering(
                offering_id="aihubmix-claude-opus-5-5",
                provider="anthropic",
                model_pattern="claude-opus-5-5",
                connection_id=settings.aihubmix_claude_connection_id,
                capability_contract_id="claude-opus-5-5-adaptive",
                protocol_profile_id="claude-messages-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
            ),
            ModelOffering(
                offering_id="aihubmix-claude-sonnet-5",
                provider="anthropic",
                model_pattern="claude-sonnet-5",
                connection_id=settings.aihubmix_claude_connection_id,
                capability_contract_id="claude-sonnet-5-adaptive",
                protocol_profile_id="claude-messages-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
            ),
            ModelOffering(
                offering_id="aihubmix-grok-4-7",
                provider="grok",
                model_pattern="grok-4.7",
                connection_id="aihubmix_default",
                capability_contract_id="grok-4-7-responses",
                protocol_profile_id="responses-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
            ),
            ModelOffering(
                offering_id="aihubmix-gpt-6-luna",
                provider="openai",
                model_pattern="gpt-6-luna",
                connection_id="aihubmix_default",
                capability_contract_id="gpt-6-reasoning",
                protocol_profile_id="responses-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
            ),
            ModelOffering(
                offering_id="aihubmix-gpt-6-sol",
                provider="openai",
                model_pattern="gpt-6-sol",
                connection_id="aihubmix_default",
                capability_contract_id="gpt-6-reasoning",
                protocol_profile_id="responses-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
            ),
            ModelOffering(
                offering_id="aihubmix-gpt-6-astra",
                provider="openai",
                model_pattern="gpt-6-astra",
                connection_id="aihubmix_default",
                capability_contract_id="gpt-6-astra-reasoning",
                protocol_profile_id="responses-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
            ),
            ModelOffering(
                offering_id="aihubmix-coding-glm-5-3-free",
                provider="glm",
                model_pattern="coding-glm-5.3-free",
                connection_id=settings.aihubmix_chat_connection_id,
                capability_contract_id="glm-5-3-free-chat",
                protocol_profile_id="chat-completions-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
                quota={"requests_per_minute": 5, "requests_per_day": 100, "tokens_per_day": 1_000_000, "enforcement": "declarative"},
            ),
            ModelOffering(
                offering_id="aihubmix-xiaomi-mimo-v2-6-pro-free",
                provider="xiaomi",
                model_pattern="xiaomi-mimo-v2.6-pro-free",
                connection_id=settings.aihubmix_chat_connection_id,
                capability_contract_id="mimo-v2-6-chat",
                protocol_profile_id="chat-completions-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
                quota={"requests_per_minute": 5, "requests_per_day": 100, "tokens_per_day": 1_000_000, "enforcement": "declarative"},
            ),
            ModelOffering(
                offering_id="aihubmix-gemini-3-8-flash",
                provider="gemini",
                model_pattern="gemini-3.8-flash",
                connection_id=settings.aihubmix_gemini_connection_id,
                capability_contract_id="gemini-3-8-native",
                protocol_profile_id="gemini-native-aihubmix-v1",
                cache_policy_id="session-private-cache-policy-v1",
                priority=200,
                deployment_id=deployment,
                observed_model_policy="strict",
                requires_file_adapter=True,
            ),
        ]
        return cls(
            revision=settings.model_control_plane_revision,
            connections=connections,
            capability_contracts=contracts,
            protocol_profiles=protocol_profiles,
            cache_policies=cache_policies,
            offerings=offerings,
            status="published",
            release_metadata={"source": "builtin", "release_id": settings.model_control_plane_revision},
        )

    def candidate_offerings(self, *, provider: str, model: str, deployment_id: str) -> list[ModelOffering]:
        matches = [
            item
            for item in self.offerings.values()
            if item.matches(provider=provider, model=model, deployment_id=deployment_id)
        ]
        return sorted(matches, key=lambda x: (-x.priority, x.offering_id, x.connection_id))

    def connection(self, connection_id: str) -> ConnectionSpec | None:
        return self.connections.get(connection_id)

    def contract(self, contract_id: str) -> CapabilityContract | None:
        return self.capability_contracts.get(contract_id)

    def protocol_profile(self, profile_id: str | None) -> ProtocolProfileSpec | None:
        return self.protocol_profiles.get(profile_id) if profile_id else None

    def cache_policy(self, policy_id: str | None) -> CachePolicySpec | None:
        return self.cache_policies.get(policy_id) if policy_id else None

    def credential(self, connection_id: str, settings: Settings) -> str | None:
        spec = self.connections.get(connection_id)
        if spec is None:
            return None
        if spec.channel_id == "aihubmix":
            return settings.aihubmix_api_key.get_secret_value() if settings.aihubmix_api_key else None
        if connection_id == settings.moonshot_connection_id:
            return settings.moonshot_api_key.get_secret_value() if settings.moonshot_api_key else None
        if spec.credential_env:
            return os.getenv(spec.credential_env) or None
        return None

    def connection_configuration(self, connection_id: str, settings: Settings) -> tuple[bool, str | None]:
        spec = self.connections.get(connection_id)
        if spec is None:
            return settings.connection_configuration(connection_id)
        if not spec.base_url:
            return False, "connection base_url is not configured"
        if spec.protocol in {"responses", "chat_completions", "claude_messages", "gemini_native", "moonshot_chat"}:
            if not self.credential(connection_id, settings):
                label = spec.credential_env or ("AIHUBMIX_API_KEY" if spec.channel_id == "aihubmix" else "credential")
                return False, f"{label} is not configured"
        return True, None

    def account_scope_hash(self, connection_id: str, settings: Settings) -> str:
        spec = self.connections.get(connection_id)
        if spec is None:
            return settings.connection_account_scope_hash(connection_id)
        secret = self.credential(connection_id, settings) or "unconfigured"
        material = "|".join(
            [
                spec.connection_id,
                spec.channel_id,
                spec.account_id or "",
                spec.base_url or "",
                secret,
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _validate(self) -> None:
        if self.status != "published":
            raise ValueError("Runtime control-plane snapshots must have status=published")
        for offering in self.offerings.values():
            if offering.connection_id not in self.connections:
                raise ValueError(
                    f"Offering {offering.offering_id!r} references unknown connection {offering.connection_id!r}"
                )
            if offering.capability_contract_id not in self.capability_contracts:
                raise ValueError(
                    f"Offering {offering.offering_id!r} references unknown capability contract {offering.capability_contract_id!r}"
                )
            if offering.protocol_profile_id and offering.protocol_profile_id not in self.protocol_profiles:
                raise ValueError(
                    f"Offering {offering.offering_id!r} references unknown protocol profile {offering.protocol_profile_id!r}"
                )
            if offering.cache_policy_id and offering.cache_policy_id not in self.cache_policies:
                raise ValueError(
                    f"Offering {offering.offering_id!r} references unknown cache policy {offering.cache_policy_id!r}"
                )
            policy = offering.observed_model_policy.lower()
            if policy not in {"ignore", "audit", "strict"}:
                raise ValueError(
                    f"Offering {offering.offering_id!r} observed_model_policy must be ignore, audit or strict"
                )
        for connection in self.connections.values():
            if connection.protocol not in {
                "responses",
                "chat_completions",
                "claude_messages",
                "gemini_native",
                "moonshot_chat",
            }:
                raise ValueError(
                    f"Connection {connection.connection_id!r} has unsupported protocol {connection.protocol!r}"
                )

    @staticmethod
    def _connection_from_mapping(value: Any) -> ConnectionSpec:
        if not isinstance(value, dict):
            raise ValueError("Each control-plane connection must be an object")
        connection_id = str(value.get("connection_id") or "").strip()
        channel_id = str(value.get("channel_id") or "").strip()
        protocol = str(value.get("protocol") or "").strip().lower()
        if not connection_id or not channel_id or not protocol:
            raise ValueError("Control-plane connections require connection_id, channel_id and protocol")
        return ConnectionSpec(
            connection_id=connection_id,
            channel_id=channel_id,
            protocol=protocol,
            base_url=(str(value.get("base_url")).rstrip("/") if value.get("base_url") else None),
            credential_env=(str(value.get("credential_env")) if value.get("credential_env") else None),
            account_id=(str(value.get("account_id")) if value.get("account_id") else None),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _contract_from_mapping(value: Any) -> CapabilityContract:
        if not isinstance(value, dict):
            raise ValueError("Each capability contract must be an object")
        contract_id = str(value.get("contract_id") or "").strip()
        revision = str(value.get("revision") or "").strip()
        if not contract_id or not revision:
            raise ValueError("Capability contracts require contract_id and revision")
        options = value.get("supported_options") or {}
        if isinstance(options, list):
            options = {str(name): {} for name in options}
        if not isinstance(options, dict):
            raise ValueError("supported_options must be an object or array")
        thinking = value.get("thinking") or {"mode": "none", "accepted_levels": ["auto"]}
        structured = value.get("structured_output") or {"mode": "none"}
        if not isinstance(thinking, dict) or not isinstance(structured, dict):
            raise ValueError("thinking and structured_output must be objects")
        return CapabilityContract(
            contract_id=contract_id,
            revision=revision,
            supported_options={str(k): dict(v or {}) for k, v in options.items()},
            thinking=dict(thinking),
            structured_output=dict(structured),
            input_modalities=tuple(str(x) for x in (value.get("input_modalities") or ["text"])),
            features=tuple(str(x) for x in (value.get("features") or [])),
            provider_defaults=dict(value.get("provider_defaults") or {}),
            cache=dict(value.get("cache") or {}),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _profile_from_mapping(value: Any) -> ProtocolProfileSpec:
        if not isinstance(value, dict):
            raise ValueError("Each protocol profile must be an object")
        profile_id = str(value.get("profile_id") or "").strip()
        revision = str(value.get("revision") or "").strip()
        protocol = str(value.get("protocol") or "").strip().lower()
        if not profile_id or not revision or not protocol:
            raise ValueError("Protocol profiles require profile_id, revision and protocol")
        return ProtocolProfileSpec(
            profile_id=profile_id,
            revision=revision,
            protocol=protocol,
            cache=dict(value.get("cache") or {}),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _cache_policy_from_mapping(value: Any) -> CachePolicySpec:
        if not isinstance(value, dict):
            raise ValueError("Each cache policy must be an object")
        policy_id = str(value.get("policy_id") or value.get("id") or "").strip()
        revision = str(value.get("revision") or "").strip()
        if not policy_id or not revision:
            raise ValueError("Cache policies require policy_id/id and revision")
        return CachePolicySpec(
            policy_id=policy_id,
            revision=revision,
            scope=str(value.get("scope") or "session"),
            mechanism_preference=tuple(str(x) for x in (value.get("mechanism_preference") or [])),
            auto_prepare_failure=str(value.get("auto_prepare_failure") or "uncached_same_context"),
            on_prepare_failure=str(value.get("on_prepare_failure") or "fail"),
            cross_session_sharing=bool(value.get("cross_session_sharing", False)),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _offering_from_mapping(value: Any, default_deployment: str) -> ModelOffering:
        if not isinstance(value, dict):
            raise ValueError("Each model offering must be an object")
        required = {
            "offering_id": str(value.get("offering_id") or "").strip(),
            "provider": str(value.get("provider") or "").strip().lower(),
            "model_pattern": str(value.get("model_pattern") or "").strip(),
            "connection_id": str(value.get("connection_id") or "").strip(),
            "capability_contract_id": str(value.get("capability_contract_id") or "").strip(),
        }
        if not all(required.values()):
            raise ValueError("Model offerings require offering_id, provider, model_pattern, connection_id and capability_contract_id")
        return ModelOffering(
            **required,
            protocol_profile_id=(str(value.get("protocol_profile_id")) if value.get("protocol_profile_id") else None),
            cache_policy_id=(str(value.get("cache_policy_id")) if value.get("cache_policy_id") else None),
            priority=int(value.get("priority", 100)),
            deployment_id=str(value.get("deployment_id") or default_deployment),
            requires_file_adapter=bool(value.get("requires_file_adapter", False)),
            observed_model_policy=str(value.get("observed_model_policy") or "audit").lower(),
            enabled=bool(value.get("enabled", True)),
            quota=dict(value.get("quota") or {}),
            metadata=dict(value.get("metadata") or {}),
        )


def _option_number(minimum: float, maximum: float, *, allowed_when: list[str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "number", "min": minimum, "max": maximum}
    if allowed_when:
        value["allowed_when_think_levels"] = list(allowed_when)
    return value


def _option_tokens(maximum: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "integer", "min": 1}
    if maximum is not None:
        value["max"] = maximum
    return value


def _contract(
    contract_id: str,
    revision: str,
    *,
    options: dict[str, dict[str, Any]],
    thinking: dict[str, Any],
    structured_mode: str,
    modalities: tuple[str, ...] = ("text",),
    features: tuple[str, ...] = (),
    provider_defaults: dict[str, Any] | None = None,
    cache: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> CapabilityContract:
    return CapabilityContract(
        contract_id=contract_id,
        revision=revision,
        supported_options=options,
        thinking=thinking,
        structured_output={"mode": structured_mode},
        input_modalities=modalities,
        features=features,
        provider_defaults=dict(provider_defaults or {}),
        cache=dict(cache or {}),
        metadata=dict(metadata or {}),
    )


def _builtin_protocol_profiles() -> list[ProtocolProfileSpec]:
    return [
        ProtocolProfileSpec(
            profile_id="responses-aihubmix-v1",
            revision="relay-protocol-profile/responses-aihubmix/2026-09-30.1",
            protocol="responses",
            cache={
                "supported_mechanisms": ["implicit_prefix"],
                "prompt_cache_key_field": "prompt_cache_key",
                "usage_mapping": "openai_compatible",
            },
        ),
        ProtocolProfileSpec(
            profile_id="claude-messages-aihubmix-v1",
            revision="relay-protocol-profile/claude-aihubmix/2026-09-30.1",
            protocol="claude_messages",
            cache={
                "supported_mechanisms": ["breakpoint"],
                "cache_control_field": "cache_control",
                "usage_mapping": "claude_messages",
            },
        ),
        ProtocolProfileSpec(
            profile_id="gemini-native-aihubmix-v1",
            revision="relay-protocol-profile/gemini-aihubmix/2026-09-30.1",
            protocol="gemini_native",
            cache={
                "supported_mechanisms": ["stateful_resource"],
                "resource_family": "cachedContents",
                "usage_mapping": "gemini_native",
            },
        ),
        ProtocolProfileSpec(
            profile_id="chat-completions-aihubmix-v1",
            revision="relay-protocol-profile/chat-aihubmix/2026-09-30.1",
            protocol="chat_completions",
            cache={"supported_mechanisms": [], "usage_mapping": "openai_compatible"},
        ),
        ProtocolProfileSpec(
            profile_id="moonshot-chat-v1",
            revision="relay-protocol-profile/moonshot/2026-09-30.1",
            protocol="moonshot_chat",
            cache={"supported_mechanisms": [], "usage_mapping": "openai_compatible"},
        ),
    ]


def _builtin_cache_policies() -> list[CachePolicySpec]:
    return [
        CachePolicySpec(
            policy_id="session-private-cache-policy-v1",
            revision="relay-cache-policy/session-private/2026-09-30.1",
            scope="session",
            mechanism_preference=("stateful_resource", "breakpoint", "implicit_prefix"),
            auto_prepare_failure="uncached_same_context",
            on_prepare_failure="fail",
            cross_session_sharing=False,
            metadata={
                "below_minimum": "none",
                "assessment_uncertain": "none",
            },
        )
    ]


def _builtin_contracts() -> list[CapabilityContract]:
    sampling = {
        "temperature": _option_number(0, 2),
        "top_p": _option_number(0, 1),
        "max_output_tokens": _option_tokens(),
    }
    return [
        _contract(
            "gemini-legacy-native",
            "relay-capability/gemini-legacy/2026-09-27.1",
            options=sampling,
            thinking={
                "mode": "effort",
                "accepted_levels": ["auto", "low", "medium", "high"],
                "level_map": {},
                "wire_strategy": "gemini_thinking_level",
            },
            cache={
                "version": "relay-cache-contract/1",
                "supported_mechanisms": ["stateful_resource"],
                "verification_status": "candidate",
                "mechanism_profiles": {
                    "stateful_resource": {"threshold_mode": "unknown"}
                },
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "document"),
        ),
        _contract(
            "gemini-3-8-native",
            "relay-capability/gemini-3.8-flash/2026-09-27.1",
            options=sampling,
            thinking={
                "mode": "effort",
                "accepted_levels": ["auto", "low", "medium", "high"],
                "level_map": {},
                "wire_strategy": "gemini_thinking_level",
            },
            cache={
                "version": "relay-cache-contract/1",
                "supported_mechanisms": ["stateful_resource"],
                "verification_status": "candidate",
                "mechanism_profiles": {
                    "stateful_resource": {"threshold_mode": "unknown"}
                },
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "document"),
        ),
        _contract(
            "grok-legacy-responses",
            "relay-capability/grok-legacy/2026-09-27.1",
            options=sampling,
            thinking={
                "mode": "effort",
                "accepted_levels": ["auto", "low", "medium", "high", "xhigh"],
                "level_map": {},
                "wire_strategy": "responses_reasoning_effort",
                "include_encrypted_reasoning": True,
            },
            cache={
                "version": "relay-cache-contract/1",
                "supported_mechanisms": ["implicit_prefix"],
                "verification_status": "legacy_verified",
                "mechanism_profiles": {
                    "implicit_prefix": {
                        "threshold_mode": "not_applicable",
                        "key_source": "legacy_session_prompt_cache_key",
                        "send_prompt_cache_key": True,
                    }
                },
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "document"),
        ),
        _contract(
            "grok-4-7-responses",
            "relay-capability/grok-4.7/2026-09-27.1",
            options={"max_output_tokens": _option_tokens()},
            thinking={
                "mode": "effort",
                "accepted_levels": ["auto", "low", "medium", "high", "xhigh"],
                "level_map": {},
                "wire_strategy": "responses_reasoning_effort",
                "include_encrypted_reasoning": True,
            },
            cache={
                "version": "relay-cache-contract/1",
                "supported_mechanisms": ["implicit_prefix"],
                "verification_status": "legacy_verified",
                "mechanism_profiles": {
                    "implicit_prefix": {
                        "threshold_mode": "not_applicable",
                        "key_source": "legacy_session_prompt_cache_key",
                        "send_prompt_cache_key": True,
                    }
                },
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "document"),
        ),
        _contract(
            "gpt-6-reasoning",
            "relay-capability/gpt-6-sol-luna/2026-09-27.1",
            options={
                "temperature": _option_number(0, 2, allowed_when=["none"]),
                "top_p": _option_number(0, 1, allowed_when=["none"]),
                "max_output_tokens": _option_tokens(),
            },
            thinking={
                "mode": "effort",
                "accepted_levels": ["auto", "none", "low", "medium", "high", "xhigh", "max"],
                "level_map": {"auto": "medium"},
                "wire_strategy": "responses_reasoning_effort",
            },
            cache={
                "version": "relay-cache-contract/1",
                "supported_mechanisms": ["implicit_prefix"],
                "verification_status": "candidate",
                "mechanism_profiles": {"implicit_prefix": {"threshold_mode": "unknown"}},
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "document"),
        ),
        _contract(
            "gpt-6-astra-reasoning",
            "relay-capability/gpt-6-astra/2026-09-27.1",
            options={"max_output_tokens": _option_tokens()},
            thinking={
                "mode": "effort",
                "accepted_levels": ["auto", "low", "medium", "high", "xhigh", "max"],
                "level_map": {"auto": "medium"},
                "wire_strategy": "responses_reasoning_effort",
            },
            cache={
                "version": "relay-cache-contract/1",
                "supported_mechanisms": ["implicit_prefix"],
                "verification_status": "candidate",
                "mechanism_profiles": {"implicit_prefix": {"threshold_mode": "unknown"}},
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "document"),
        ),
        _contract(
            "claude-opus-5-5-adaptive",
            "relay-capability/claude-opus-5.5/2026-09-27.1",
            options={"max_output_tokens": _option_tokens(128000)},
            thinking={
                "mode": "always_on_adaptive",
                "accepted_levels": ["auto", "low", "medium", "high", "xhigh", "max"],
                "level_map": {"auto": "medium"},
                "wire_strategy": "claude_adaptive_effort",
            },
            cache={
                "version": "relay-cache-contract/1",
                "supported_mechanisms": ["breakpoint"],
                "verification_status": "candidate",
                "mechanism_profiles": {
                    "breakpoint": {"threshold_mode": "unknown", "max_breakpoints": 1}
                },
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "document"),
            provider_defaults={"max_output_tokens": 8192},
        ),
        _contract(
            "claude-sonnet-5-adaptive",
            "relay-capability/claude-sonnet-5/2026-09-27.1",
            options={"max_output_tokens": _option_tokens(128000)},
            thinking={
                "mode": "adaptive_optional_disable",
                "accepted_levels": ["auto", "off", "low", "medium", "high", "xhigh", "max"],
                "level_map": {"auto": "high"},
                "wire_strategy": "claude_adaptive_effort",
            },
            cache={
                "version": "relay-cache-contract/1",
                "supported_mechanisms": ["breakpoint"],
                "verification_status": "candidate",
                "mechanism_profiles": {
                    "breakpoint": {"threshold_mode": "unknown", "max_breakpoints": 1}
                },
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "document"),
            provider_defaults={"max_output_tokens": 8192},
        ),
        _contract(
            "glm-5-3-free-chat",
            "relay-capability/coding-glm-5.3-free/2026-09-27.1",
            options={"max_output_tokens": _option_tokens()},
            thinking={
                "mode": "always_on_effort",
                "accepted_levels": ["auto", "low", "high", "max"],
                "level_map": {"auto": "max"},
                "wire_strategy": "chat_reasoning_effort_with_thinking_on",
            },
            structured_mode="json_object_post_validate",
            modalities=("text",),
            features=("free_tier",),
        ),
        _contract(
            "mimo-v2-6-chat",
            "relay-capability/xiaomi-mimo-v2.6-pro-free/2026-09-27.2",
            options={"max_output_tokens": _option_tokens()},
            thinking={
                # AIHubMix currently identifies MiMo V2.6 Pro as a reasoning model,
                # but the exact stable wire contract for caller-controlled effort
                # is not part of this release evidence. Keep upstream default
                # behavior instead of inventing an on/off or effort mapping.
                "mode": "upstream_default",
                "accepted_levels": ["auto"],
                "level_map": {},
                "wire_strategy": "",
            },
            structured_mode="json_object_post_validate",
            modalities=("text",),
            features=("free_tier",),
            metadata={"validation_status": "account_verification_required_for_thinking_controls"},
        ),
        _contract(
            "kimi-legacy-moonshot",
            "relay-capability/kimi-legacy/2026-09-27.1",
            options=sampling,
            thinking={
                "mode": "model_specific_legacy",
                "accepted_levels": ["auto", "low", "medium", "high", "xhigh", "max", "on", "off"],
                "level_map": {},
                "wire_strategy": "moonshot_legacy",
            },
            structured_mode="native_json_schema",
            modalities=("text", "image", "video", "document"),
        ),
    ]


def _hash_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
