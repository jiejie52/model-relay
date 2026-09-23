from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any

from ..config import Settings
from ..materials.provider_files.registry import ProviderFileRegistry
from ..model_options import CAPABILITY_PROFILE_REVISION
from ..observability import error as log_error, info as log_info, warning as log_warning
from ..providers.registry import ProviderRegistry
from .catalog import RouteCatalog


logger = logging.getLogger("model-relay-routing")


@dataclass(frozen=True)
class RouteIntent:
    provider: str
    model: str
    purpose: str
    deployment_id: str


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
        # connection_id/account details stay private; the public error remains
        # specific enough to distinguish routing, model and server config faults.
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
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.providers = providers
        self.provider_files = provider_files

    def _connection_is_enabled(self, connection_id: str) -> bool:
        checker = getattr(self.settings, "connection_is_enabled", None)
        if callable(checker):
            return bool(checker(connection_id))
        enabled = getattr(self.settings, "enabled_connection_set", set())
        return connection_id in enabled

    def _connection_configuration(self, connection_id: str) -> tuple[bool, str | None]:
        checker = getattr(self.settings, "connection_configuration", None)
        if callable(checker):
            return checker(connection_id)
        return True, None

    def _connection_policy(self) -> str:
        return str(getattr(self.settings, "connection_availability_mode", "legacy") or "legacy")

    def _registered_provider_connections(self) -> list[str]:
        fn = getattr(self.providers, "registered_connections", None)
        return list(fn()) if callable(fn) else []

    def _registered_file_connections(self) -> list[str]:
        fn = getattr(self.provider_files, "registered_connections", None)
        return list(fn()) if callable(fn) else []

    def validate_catalog(self) -> None:
        """Validate structural consistency without making availability define routes.

        A route may exist while its credentials/adapter are not configured yet.
        That is no longer a startup-fatal error in the default ``all`` mode;
        selecting that route returns a precise RouteResolutionError instead.
        """
        unavailable: list[dict[str, Any]] = []
        for entry in self.catalog.entries:
            if entry.deployment_id not in (None, "*", self.settings.deployment_id):
                continue

            if not self._connection_is_enabled(entry.connection_id):
                unavailable.append(
                    {
                        "provider": entry.provider,
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
                        "connection_id": entry.connection_id,
                        "reason": "inference_adapter_not_registered",
                    }
                )
                continue

            registered_provider = str(provider_meta.get("provider") or "").lower()
            if registered_provider and registered_provider != entry.provider.lower():
                raise RuntimeError(
                    f"Route catalog provider {entry.provider!r} does not match inference adapter "
                    f"provider {registered_provider!r} for {entry.connection_id!r}"
                )

            file_meta = self.provider_files.describe(entry.connection_id)
            if entry.requires_file_adapter and file_meta is None:
                unavailable.append(
                    {
                        "provider": entry.provider,
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
                connection_policy=self._connection_policy(),
                **row,
            )

        log_info(
            logger,
            "route_catalog_validated",
            deployment_id=self.settings.deployment_id,
            route_revision=self.catalog.revision,
            route_catalog_hash=self.catalog.catalog_hash,
            route_count=len(self.catalog.entries),
            route_providers=self.catalog.providers(),
            connection_policy=self._connection_policy(),
            registered_inference_connections=self._registered_provider_connections(),
            registered_file_connections=self._registered_file_connections(),
            unavailable_route_count=len(unavailable),
        )

    def resolve(self, *, provider: str, model: str, purpose: str) -> RouteBinding:
        provider_n = str(provider or "").strip().lower()
        model_n = str(model or "").strip()
        purpose_n = str(purpose or "").strip() or "request"
        intent = RouteIntent(provider_n, model_n, purpose_n, self.settings.deployment_id)
        entry = None
        try:
            if not provider_n or not model_n:
                raise RouteResolutionError(
                    "ROUTE_INTENT_INVALID",
                    "provider and model are required for Relay route resolution",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                )
            try:
                entry = self.catalog.match(
                    provider=provider_n,
                    model=model_n,
                    deployment_id=self.settings.deployment_id,
                )
            except ValueError as exc:
                raise RouteResolutionError(
                    "ROUTE_CATALOG_AMBIGUOUS",
                    "Relay has more than one equally preferred route for this provider/model",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    internal_reason=str(exc),
                ) from exc

            if entry is None:
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

            if not self._connection_is_enabled(entry.connection_id):
                raise RouteResolutionError(
                    "ROUTE_CONNECTION_DISABLED",
                    "Relay resolved the provider/model route, but that server connection is disabled by the current connection policy",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    internal_connection_id=entry.connection_id,
                    internal_reason="disabled_by_connection_policy",
                )

            configured, config_reason = self._connection_configuration(entry.connection_id)
            if not configured:
                raise RouteResolutionError(
                    "ROUTE_CONNECTION_NOT_CONFIGURED",
                    "Relay resolved the provider/model route, but its server-side connection is not fully configured on this deployment",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    internal_connection_id=entry.connection_id,
                    internal_reason=config_reason,
                )

            provider_meta = self.providers.describe(entry.connection_id)
            if provider_meta is None:
                raise RouteResolutionError(
                    "ROUTE_ADAPTER_NOT_REGISTERED",
                    "Relay resolved the provider/model route, but its inference adapter is not registered on this process",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    internal_connection_id=entry.connection_id,
                    internal_reason="inference_adapter_not_registered",
                )

            registered_provider = str(provider_meta.get("provider") or "").lower()
            if registered_provider and registered_provider != provider_n:
                raise RouteResolutionError(
                    "ROUTE_CONFIG_INVALID",
                    "Relay route provider does not match the registered inference adapter",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    internal_connection_id=entry.connection_id,
                    internal_reason=f"registered_provider={registered_provider}",
                )

            file_meta = self.provider_files.describe(entry.connection_id)
            if entry.requires_file_adapter and file_meta is None:
                raise RouteResolutionError(
                    "ROUTE_FILE_ADAPTER_NOT_REGISTERED",
                    "Relay resolved the provider/model route, but the required native file adapter is not registered on this process",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    internal_connection_id=entry.connection_id,
                    internal_reason="provider_file_adapter_not_registered",
                )
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

            try:
                scope_hash = self.settings.connection_account_scope_hash(entry.connection_id)
            except RuntimeError as exc:
                raise RouteResolutionError(
                    "ROUTE_CONNECTION_NOT_CONFIGURED",
                    "Relay resolved the provider/model route, but its server-side account configuration is incomplete",
                    provider=provider_n,
                    model=model_n,
                    purpose=purpose_n,
                    internal_connection_id=entry.connection_id,
                    internal_reason=str(exc),
                ) from exc

            canonical = {
                "provider": provider_n,
                "model": model_n,
                "connection_id": entry.connection_id,
                "route_revision": self.catalog.revision,
                "account_scope_hash": scope_hash,
                "inference_adapter_version": provider_meta.get("adapter_version"),
                "file_adapter_version": file_meta.get("adapter_version") if file_meta else None,
                "execution_pool": self.settings.execution_pool,
                "capability_revision": CAPABILITY_PROFILE_REVISION,
            }
            binding_hash = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            binding = RouteBinding(
                provider=provider_n,
                model=model_n,
                connection_id=entry.connection_id,
                route_revision=self.catalog.revision,
                route_binding_hash=binding_hash,
                account_scope_hash=scope_hash,
                inference_adapter_version=str(provider_meta.get("adapter_version") or "unknown"),
                file_adapter_version=(str(file_meta.get("adapter_version")) if file_meta else None),
                execution_pool=self.settings.execution_pool,
                purpose=purpose_n,
                capability_revision=CAPABILITY_PROFILE_REVISION,
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
                connection_policy=self._connection_policy(),
                connection_id=binding.connection_id,
                adapter_version=binding.inference_adapter_version,
                file_adapter_version=binding.file_adapter_version,
                capability_revision=binding.capability_revision,
            )
            return binding
        except RouteResolutionError as exc:
            connection_id = exc.internal_connection_id or (entry.connection_id if entry is not None else None)
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
                deployment_id=intent.deployment_id,
                route_revision=self.catalog.revision,
                route_catalog_hash=self.catalog.catalog_hash,
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
                failure_class=(
                    "client"
                    if exc.code in {"ROUTE_INTENT_INVALID", "ROUTE_MODEL_UNSUPPORTED", "LEGACY_CONNECTION_HINT_MISMATCH"}
                    else "relay_configuration"
                ),
            )
            raise

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
