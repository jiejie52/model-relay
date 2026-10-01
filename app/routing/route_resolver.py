from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from typing import Any

from ..config import Settings
from ..control_plane import ModelControlPlane
from ..materials.provider_files.registry import ProviderFileRegistry
from ..model_options import CAPABILITY_PROFILE_REVISION
from ..observability import error as log_error, info as log_info, warning as log_warning
from ..providers.registry import ProviderRegistry
from .catalog import RouteCatalog, RouteEntry


logger = logging.getLogger("model-relay-routing")

_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "google": "gemini",
    "gpt": "openai",
    "zhipu": "glm",
}


@dataclass(frozen=True)
class RouteIntent:
    provider: str
    model: str
    purpose: str
    deployment_id: str
    requirements: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteBinding:
    provider: str
    model: str
    connection_id: str
    route_revision: str
    route_binding_hash: str
    account_scope_hash: str
    inference_adapter_version: str
    file_adapter_version: str | None
    execution_pool: str
    purpose: str
    capability_revision: str = CAPABILITY_PROFILE_REVISION
    offering_id: str | None = None
    channel_id: str | None = None
    protocol: str | None = None
    capability_contract_id: str | None = None
    capability_contract_hash: str | None = None
    capability_contract: dict[str, Any] | None = None
    protocol_profile_id: str | None = None
    protocol_profile_hash: str | None = None
    protocol_profile: dict[str, Any] | None = None
    cache_policy_id: str | None = None
    cache_policy_hash: str | None = None
    cache_policy: dict[str, Any] | None = None
    cache_contract_hash: str | None = None
    cache_contract: dict[str, Any] | None = None
    control_plane_hash: str | None = None
    capability_requirements: dict[str, Any] = field(default_factory=dict)
    observed_model_policy: str = "audit"
    quota: dict[str, Any] = field(default_factory=dict)

    def internal_metadata(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "connection_id": self.connection_id,
            "route_revision": self.route_revision,
            "route_binding_hash": self.route_binding_hash,
            "account_scope_hash": self.account_scope_hash,
            "inference_adapter_version": self.inference_adapter_version,
            "file_adapter_version": self.file_adapter_version,
            "execution_pool": self.execution_pool,
            "capability_revision": self.capability_revision,
            "offering_id": self.offering_id,
            "channel_id": self.channel_id,
            "protocol": self.protocol,
            "capability_contract_id": self.capability_contract_id,
            "capability_contract_hash": self.capability_contract_hash,
            "capability_contract": self.capability_contract,
            "protocol_profile_id": self.protocol_profile_id,
            "protocol_profile_hash": self.protocol_profile_hash,
            "protocol_profile": self.protocol_profile,
            "cache_policy_id": self.cache_policy_id,
            "cache_policy_hash": self.cache_policy_hash,
            "cache_policy": self.cache_policy,
            "cache_contract_hash": self.cache_contract_hash,
            "cache_contract": self.cache_contract,
            "control_plane_hash": self.control_plane_hash,
            "capability_requirements": self.capability_requirements,
            "observed_model_policy": self.observed_model_policy,
            "quota": self.quota,
        }


class RouteResolutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str,
        model: str,
        purpose: str,
        internal_connection_id: str | None = None,
        internal_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.model = model
        self.purpose = purpose
        self.internal_connection_id = internal_connection_id
        self.internal_reason = internal_reason

    def public_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "purpose": self.purpose,
        }


class RouteResolver:
    def __init__(
        self,
        *,
        settings: Settings,
        catalog: RouteCatalog,
        providers: ProviderRegistry,
        provider_files: ProviderFileRegistry,
        control_plane: ModelControlPlane | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.providers = providers
        self.provider_files = provider_files
        self.control_plane = control_plane

    def _connection_is_enabled(self, connection_id: str) -> bool:
        checker = getattr(self.settings, "connection_is_enabled", None)
        if callable(checker):
            return bool(checker(connection_id))
        enabled = getattr(self.settings, "enabled_connection_set", set())
        return connection_id in enabled

    def _connection_configuration(self, connection_id: str) -> tuple[bool, str | None]:
        if self.control_plane is not None:
            spec = self.control_plane.connection(connection_id)
            if spec is not None:
                return self.control_plane.connection_configuration(connection_id, self.settings)
        checker = getattr(self.settings, "connection_configuration", None)
        if callable(checker):
            return checker(connection_id)
        return True, None

    def _account_scope_hash(self, connection_id: str) -> str:
        if self.control_plane is not None and self.control_plane.connection(connection_id) is not None:
            return self.control_plane.account_scope_hash(connection_id, self.settings)
        return self.settings.connection_account_scope_hash(connection_id)

    def _connection_policy(self) -> str:
        return str(getattr(self.settings, "connection_availability_mode", "all"))

    def _registered_provider_connections(self) -> list[str]:
        getter = getattr(self.providers, "registered_connections", None)
        return getter() if callable(getter) else []

    def _registered_file_connections(self) -> list[str]:
        getter = getattr(self.provider_files, "registered_connections", None)
        return getter() if callable(getter) else []

    def validate_catalog(self) -> None:
        unavailable: list[dict[str, Any]] = []
        for entry in self.catalog.entries:
            if not self._connection_is_enabled(entry.connection_id):
                unavailable.append(
                    {
                        "provider": entry.provider,
                        "model_pattern": entry.model_pattern,
                        "offering_id": entry.offering_id,
                        "connection_id": entry.connection_id,
                        "reason": "disabled_by_connection_policy",
                    }
                )
                continue

            configured, config_reason = self._connection_configuration(entry.connection_id)
            if not configured:
                unavailable.append(
                    {
                        "provider": entry.provider,
                        "model_pattern": entry.model_pattern,
                        "offering_id": entry.offering_id,
                        "connection_id": entry.connection_id,
                        "reason": config_reason or "connection_not_configured",
                    }
                )
                continue

            provider_meta = self.providers.describe(entry.connection_id)
            if provider_meta is None:
                unavailable.append(
                    {
                        "provider": entry.provider,
                        "model_pattern": entry.model_pattern,
                        "offering_id": entry.offering_id,
                        "connection_id": entry.connection_id,
                        "reason": "inference_adapter_not_registered",
                    }
                )
                continue
            self._assert_adapter_contract(entry, provider_meta)

            file_meta = self.provider_files.describe(entry.connection_id)
            if entry.requires_file_adapter and file_meta is None:
                unavailable.append(
                    {
                        "provider": entry.provider,
                        "model_pattern": entry.model_pattern,
                        "offering_id": entry.offering_id,
                        "connection_id": entry.connection_id,
                        "reason": "provider_file_adapter_not_registered",
                    }
                )
                continue
            if file_meta is not None:
                file_provider = str(file_meta.get("provider") or "").lower()
                if file_provider and file_provider != entry.provider.lower():
                    raise RuntimeError(
                        f"Route catalog provider {entry.provider!r} does not match file adapter "
                        f"provider {file_provider!r} for {entry.connection_id!r}"
                    )

        for row in unavailable:
            log_warning(
                logger,
                "route_catalog_connection_unavailable",
                deployment_id=self.settings.deployment_id,
                route_revision=self.catalog.revision,
                route_catalog_hash=self.catalog.catalog_hash,
                control_plane_hash=self.catalog.control_plane_hash,
                connection_policy=self._connection_policy(),
                **row,
            )

        log_info(
            logger,
            "route_catalog_validated",
            deployment_id=self.settings.deployment_id,
            route_revision=self.catalog.revision,
            route_catalog_hash=self.catalog.catalog_hash,
            control_plane_hash=self.catalog.control_plane_hash,
            route_count=len(self.catalog.entries),
            route_providers=self.catalog.providers(),
            connection_policy=self._connection_policy(),
            registered_inference_connections=self._registered_provider_connections(),
            registered_file_connections=self._registered_file_connections(),
            unavailable_route_count=len(unavailable),
        )

    def resolve(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        requirements: dict[str, Any] | None = None,
    ) -> RouteBinding:
        provider_raw = str(provider or "").strip().lower()
        provider_n = _PROVIDER_ALIASES.get(provider_raw, provider_raw)
        model_n = str(model or "").strip()
        purpose_n = str(purpose or "").strip() or "request"
        req = dict(requirements or {})
        intent = RouteIntent(provider_n, model_n, purpose_n, self.settings.deployment_id, req)
        selected: RouteEntry | None = None
        rejections: list[tuple[RouteEntry, str, str | None]] = []

        try:
            if not provider_n or not model_n:
                raise RouteResolutionError(
                    "ROUTE_INTENT_INVALID",
                    "provider and model are required for Relay route resolution",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                )

            candidates = self.catalog.candidates(
                provider=provider_n,
                model=model_n,
                deployment_id=self.settings.deployment_id,
            )
            if not candidates:
                if self.catalog.has_provider(provider_n, deployment_id=self.settings.deployment_id):
                    raise RouteResolutionError(
                        "ROUTE_MODEL_UNSUPPORTED",
                        "Relay has a route for this provider, but the selected model does not match any supported model pattern",
                        provider=provider_n,
                        model=model_n,
                        purpose=purpose_n,
                    )
                raise RouteResolutionError(
                    "ROUTE_NOT_FOUND",
                    "Relay has no route definition for the selected provider on this deployment",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                )

            channel_allowlist = {
                str(x).strip().lower()
                for x in (req.get("channel_allowlist") or [])
                if str(x).strip()
            }

            for entry in candidates:
                if channel_allowlist and str(entry.channel_id or "").lower() not in channel_allowlist:
                    rejections.append((entry, "channel_not_allowed", None))
                    continue

                capability_ok, capability_reason = entry.supports_requirements(req)
                if not capability_ok:
                    rejections.append((entry, "capability_requirement_not_met", capability_reason))
                    continue

                if not self._connection_is_enabled(entry.connection_id):
                    rejections.append((entry, "connection_disabled", "disabled_by_connection_policy"))
                    continue

                configured, config_reason = self._connection_configuration(entry.connection_id)
                if not configured:
                    rejections.append((entry, "connection_not_configured", config_reason))
                    continue

                provider_meta = self.providers.describe(entry.connection_id)
                if provider_meta is None:
                    rejections.append((entry, "adapter_not_registered", "inference_adapter_not_registered"))
                    continue
                self._assert_adapter_contract(entry, provider_meta)

                file_meta = self.provider_files.describe(entry.connection_id)
                if entry.requires_file_adapter and file_meta is None:
                    rejections.append((entry, "file_adapter_not_registered", "provider_file_adapter_not_registered"))
                    continue
                if file_meta is not None:
                    file_provider = str(file_meta.get("provider") or "").lower()
                    if file_provider and file_provider != provider_n:
                        raise RouteResolutionError(
                            "ROUTE_CONFIG_INVALID",
                            "Relay route provider does not match the registered Provider File Adapter",
                            provider=provider_n,
                            model=model_n,
                            purpose=purpose_n,
                            internal_connection_id=entry.connection_id,
                            internal_reason=f"file_adapter_provider={file_provider}",
                        )

                selected = entry
                break

            if selected is None:
                raise self._no_eligible_candidate_error(
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    rejections=rejections,
                )

            provider_meta = self.providers.describe(selected.connection_id)
            assert provider_meta is not None
            file_meta = self.provider_files.describe(selected.connection_id)
            try:
                scope_hash = self._account_scope_hash(selected.connection_id)
            except RuntimeError as exc:
                raise RouteResolutionError(
                    "ROUTE_CONNECTION_NOT_CONFIGURED",
                    "Relay resolved the provider/model route, but its server-side account configuration is incomplete",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    internal_connection_id=selected.connection_id,
                    internal_reason=str(exc),
                ) from exc

            capability_contract = dict(selected.capability_contract or {}) or None
            capability_revision = str(
                (capability_contract or {}).get("revision") or CAPABILITY_PROFILE_REVISION
            )
            capability_hash = (
                hashlib.sha256(
                    json.dumps(capability_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if capability_contract
                else None
            )
            protocol_profile = dict(selected.protocol_profile or {}) or None
            cache_policy = dict(selected.cache_policy or {}) or None
            cache_contract = (
                dict((capability_contract or {}).get("cache") or {})
                if isinstance((capability_contract or {}).get("cache"), dict)
                else None
            )
            protocol_profile_hash = (
                hashlib.sha256(json.dumps(protocol_profile, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                if protocol_profile else None
            )
            cache_policy_hash = (
                hashlib.sha256(json.dumps(cache_policy, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                if cache_policy else None
            )
            cache_contract_hash = (
                hashlib.sha256(json.dumps(cache_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                if cache_contract else None
            )
            canonical = {
                "provider": provider_n,
                "model": model_n,
                "connection_id": selected.connection_id,
                "offering_id": selected.offering_id,
                "channel_id": selected.channel_id,
                "protocol": selected.protocol,
                "route_revision": self.catalog.revision,
                "control_plane_hash": self.catalog.control_plane_hash,
                "account_scope_hash": scope_hash,
                "inference_adapter_version": provider_meta.get("adapter_version"),
                "file_adapter_version": file_meta.get("adapter_version") if file_meta else None,
                "execution_pool": self.settings.execution_pool,
                "capability_revision": capability_revision,
                "capability_contract_id": selected.capability_contract_id,
                "capability_contract_hash": capability_hash,
                "protocol_profile_id": selected.protocol_profile_id,
                "protocol_profile_hash": protocol_profile_hash,
                "cache_policy_id": selected.cache_policy_id,
                "cache_policy_hash": cache_policy_hash,
                "cache_contract_hash": cache_contract_hash,
                "capability_requirements": req,
                "observed_model_policy": selected.observed_model_policy,
            }
            binding_hash = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            binding = RouteBinding(
                provider=provider_n,
                model=model_n,
                connection_id=selected.connection_id,
                route_revision=self.catalog.revision,
                route_binding_hash=binding_hash,
                account_scope_hash=scope_hash,
                inference_adapter_version=str(provider_meta.get("adapter_version") or "unknown"),
                file_adapter_version=(str(file_meta.get("adapter_version")) if file_meta else None),
                execution_pool=self.settings.execution_pool,
                purpose=purpose_n,
                capability_revision=capability_revision,
                offering_id=selected.offering_id,
                channel_id=selected.channel_id,
                protocol=selected.protocol,
                capability_contract_id=selected.capability_contract_id,
                capability_contract_hash=capability_hash,
                capability_contract=capability_contract,
                protocol_profile_id=selected.protocol_profile_id,
                protocol_profile_hash=protocol_profile_hash,
                protocol_profile=protocol_profile,
                cache_policy_id=selected.cache_policy_id,
                cache_policy_hash=cache_policy_hash,
                cache_policy=cache_policy,
                cache_contract_hash=cache_contract_hash,
                cache_contract=cache_contract,
                control_plane_hash=self.catalog.control_plane_hash,
                capability_requirements=req,
                observed_model_policy=selected.observed_model_policy,
                quota=dict(selected.quota),
            )
            log_info(
                logger,
                "route_resolved",
                provider=provider_n,
                model=model_n,
                purpose=purpose_n,
                deployment_id=self.settings.deployment_id,
                execution_pool=self.settings.execution_pool,
                route_revision=binding.route_revision,
                route_catalog_hash=self.catalog.catalog_hash,
                control_plane_hash=binding.control_plane_hash,
                connection_policy=self._connection_policy(),
                connection_id=binding.connection_id,
                offering_id=binding.offering_id,
                channel_id=binding.channel_id,
                protocol=binding.protocol,
                adapter_version=binding.inference_adapter_version,
                file_adapter_version=binding.file_adapter_version,
                capability_revision=binding.capability_revision,
                capability_contract_id=binding.capability_contract_id,
                protocol_profile_id=binding.protocol_profile_id,
                cache_policy_id=binding.cache_policy_id,
                cache_contract_hash=binding.cache_contract_hash,
                rejected_candidate_count=len(rejections),
            )
            return binding
        except RouteResolutionError as exc:
            connection_id = exc.internal_connection_id or (selected.connection_id if selected else None)
            configured = None
            configuration_reason = exc.internal_reason
            if connection_id:
                configured, detected_reason = self._connection_configuration(connection_id)
                configuration_reason = configuration_reason or detected_reason
            log_error(
                logger,
                "route_resolution_failed",
                provider=intent.provider,
                model=intent.model,
                purpose=intent.purpose,
                capability_requirements=intent.requirements,
                deployment_id=intent.deployment_id,
                route_revision=self.catalog.revision,
                route_catalog_hash=self.catalog.catalog_hash,
                control_plane_hash=self.catalog.control_plane_hash,
                reason=exc.code,
                reason_detail=exc.message,
                connection_id=connection_id,
                connection_policy=self._connection_policy(),
                connection_enabled=(self._connection_is_enabled(connection_id) if connection_id else None),
                connection_configured=configured,
                configuration_reason=configuration_reason,
                provider_model_patterns=self.catalog.patterns_for_provider(
                    intent.provider, deployment_id=intent.deployment_id
                ),
                catalog_providers=self.catalog.providers(),
                registered_inference_connections=self._registered_provider_connections(),
                registered_file_connections=self._registered_file_connections(),
                candidate_rejections=[
                    {
                        "offering_id": entry.offering_id,
                        "channel_id": entry.channel_id,
                        "reason": reason,
                        "detail": detail,
                    }
                    for entry, reason, detail in rejections
                ],
                failure_class=(
                    "client"
                    if exc.code in {
                        "ROUTE_INTENT_INVALID",
                        "ROUTE_MODEL_UNSUPPORTED",
                        "ROUTE_CAPABILITY_UNAVAILABLE",
                        "LEGACY_CONNECTION_HINT_MISMATCH",
                    }
                    else "relay_configuration"
                ),
            )
            raise

    def _assert_adapter_contract(self, entry: RouteEntry, provider_meta: dict[str, Any]) -> None:
        registered_provider = str(provider_meta.get("provider") or "").lower()
        if registered_provider not in {"", "*", entry.provider.lower()}:
            raise RuntimeError(
                f"Route catalog provider {entry.provider!r} does not match inference adapter "
                f"provider {registered_provider!r} for {entry.connection_id!r}"
            )
        registered_protocol = str(provider_meta.get("protocol") or "").lower()
        if entry.protocol and registered_protocol and registered_protocol != entry.protocol.lower():
            raise RuntimeError(
                f"Route catalog protocol {entry.protocol!r} does not match inference adapter "
                f"protocol {registered_protocol!r} for {entry.connection_id!r}"
            )

    def _no_eligible_candidate_error(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        rejections: list[tuple[RouteEntry, str, str | None]],
    ) -> RouteResolutionError:
        if not rejections:
            return RouteResolutionError(
                "ROUTE_NOT_FOUND",
                "Relay has no eligible route for the selected provider/model",
                provider=provider,
                model=model,
                purpose=purpose,
            )
        first, reason, detail = rejections[0]
        if all(item[1] in {"capability_requirement_not_met", "channel_not_allowed"} for item in rejections):
            return RouteResolutionError(
                "ROUTE_CAPABILITY_UNAVAILABLE",
                "No configured channel offering can satisfy the Session capability/channel requirements",
                provider=provider,
                model=model,
                purpose=purpose,
                internal_connection_id=first.connection_id,
                internal_reason=detail or reason,
            )
        mapping = {
            "connection_disabled": (
                "ROUTE_CONNECTION_DISABLED",
                "All otherwise eligible routes are disabled by the current connection policy",
            ),
            "connection_not_configured": (
                "ROUTE_CONNECTION_NOT_CONFIGURED",
                "No eligible model offering has complete server-side connection configuration",
            ),
            "adapter_not_registered": (
                "ROUTE_ADAPTER_NOT_REGISTERED",
                "No eligible model offering has an inference adapter registered on this process",
            ),
            "file_adapter_not_registered": (
                "ROUTE_FILE_ADAPTER_NOT_REGISTERED",
                "No eligible model offering has its required native file adapter registered on this process",
            ),
        }
        code, message = mapping.get(
            reason,
            (
                "ROUTE_CAPABILITY_UNAVAILABLE",
                "No model offering is currently eligible for this Session intent",
            ),
        )
        return RouteResolutionError(
            code,
            message,
            provider=provider,
            model=model,
            purpose=purpose,
            internal_connection_id=first.connection_id,
            internal_reason=detail or reason,
        )

    def handle_legacy_hint(
        self,
        *,
        client_hint: str | None,
        resolved: RouteBinding,
        caller_version: str | None = None,
        scope: str,
    ) -> None:
        if not client_hint:
            return
        hint = str(client_hint)
        if hint == resolved.connection_id:
            return
        fields = {
            "client_hint": hint,
            "resolved_connection_id": resolved.connection_id,
            "provider": resolved.provider,
            "model": resolved.model,
            "purpose": resolved.purpose,
            "route_revision": resolved.route_revision,
            "caller_version": caller_version,
            "scope": scope,
        }
        mode = str(self.settings.route_legacy_hint_mode or "warn").lower()
        if mode == "strict":
            log_error(
                logger,
                "legacy_connection_hint_mismatch",
                failure_class="client",
                enforcement="reject",
                **fields,
            )
            raise RouteResolutionError(
                "LEGACY_CONNECTION_HINT_MISMATCH",
                "Legacy connection hint does not match the server-resolved route",
                provider=resolved.provider,
                model=resolved.model,
                purpose=resolved.purpose,
                internal_connection_id=resolved.connection_id,
            )
        log_warning(
            logger,
            "legacy_connection_hint_mismatch",
            enforcement="ignored",
            **fields,
        )
