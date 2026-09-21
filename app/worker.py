from __future__ import annotations

import asyncio
import logging
import os
import socket
import signal
from contextlib import suppress
from uuid import uuid4

from .config import get_settings
from .core.execution_runtime import SharedExecutionRuntime
from .core.raw_error import RawErrorRecorder
from .execution.queue_executor import QueueExecutor
from .materials.resolver import MaterialResolver
from .materials.fallback_storage import FallbackObjectStorage
from .materials.binding_resolver import BindingResolver
from .materials.provider_files import ProviderFileRegistry, GeminiAIHubMixFileAdapter, KimiOfficialFileAdapter
from .persistence.object_storage import StorageRegistry
from .persistence.supabase_storage import SupabaseObjectStorage
from .providers.moonshot_chat import MoonshotChatAdapter
from .providers.gemini_native import GeminiNativeAdapter
from .providers.registry import ProviderRegistry
from .providers.responses_v2 import ResponsesV2Adapter
from .routing import RouteCatalog, RouteResolver
from .supabase import SupabaseBackend
from .v2_repository import RelayV2Repository
from .utils import utcnow
from .observability import configure_logging, info as log_info, warning as log_warning, error as log_error, now_ms, elapsed_ms


settings = get_settings()
configure_logging(settings)
logger = logging.getLogger("model-relay-worker")


class RelayWorker:
    def __init__(self) -> None:
        self.backend = SupabaseBackend(settings)
        self.repo = RelayV2Repository(self.backend, settings)
        self.storage = StorageRegistry(SupabaseObjectStorage(self.backend, settings))
        self.materials = MaterialResolver(self.repo, self.storage, settings)
        self.fallback_storage = FallbackObjectStorage(self.repo, self.storage, settings)
        self.file_adapters = ProviderFileRegistry()
        if settings.connection_is_active(settings.aihubmix_gemini_connection_id):
            self.file_adapters.register(
                settings.aihubmix_gemini_connection_id,
                GeminiAIHubMixFileAdapter(settings),
            )
        if settings.connection_is_active(settings.moonshot_connection_id):
            self.file_adapters.register(
                settings.moonshot_connection_id,
                KimiOfficialFileAdapter(settings, self.repo, self.storage),
            )
        self.bindings = BindingResolver(self.repo, self.fallback_storage, self.file_adapters)
        self.providers = ProviderRegistry(settings)
        self.providers.validate_enabled_connections()
        if settings.connection_is_active("aihubmix_default"):
            self.providers.register_v2(
                "aihubmix_default",
                ResponsesV2Adapter(self.providers.openai_compatible, self.materials),
                provider="grok",
            )
        if settings.connection_is_active(settings.aihubmix_gemini_connection_id):
            self.providers.register_v2(
                settings.aihubmix_gemini_connection_id,
                GeminiNativeAdapter(settings),
                provider="gemini",
            )
        if settings.connection_is_active(settings.moonshot_connection_id):
            self.providers.register_v2(
                settings.moonshot_connection_id,
                MoonshotChatAdapter(settings, self.materials, self.repo),
                provider="kimi",
            )
        self.route_catalog = RouteCatalog.from_settings(settings)
        self.route_resolver = RouteResolver(
            settings=settings,
            catalog=self.route_catalog,
            providers=self.providers,
            provider_files=self.file_adapters,
        )
        self.route_resolver.validate_catalog()
        self.runtime = SharedExecutionRuntime(
            self.repo, self.storage, self.providers, self.materials, self.bindings, settings
        )
        self.errors = RawErrorRecorder(self.repo, self.storage, settings)
        self.queue = QueueExecutor(self.runtime, self.errors, self.repo, settings)
        self.worker_id = f"{settings.deployment_id}:{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"
        self._legacy = None
        self.stop_requested = asyncio.Event()

    async def close(self) -> None:
        if self._legacy is not None:
            await self._legacy.close()
        await self.backend.close()

    async def run_forever(self) -> None:
        log_info(
            logger,
            "worker_started",
            worker_id=self.worker_id,
            deployment_id=settings.deployment_id,
            execution_pools=sorted(settings.worker_pool_set),
            route_revision=self.route_catalog.revision,
            route_catalog_hash=self.route_catalog.catalog_hash,
            route_providers=self.route_catalog.providers(),
            connection_policy=settings.connection_availability_mode,
            configured_connections=self.providers.registered_connections(),
            configured_file_connections=self.file_adapters.registered_connections(),
        )
        while not self.stop_requested.is_set():
            try:
                job = await self.repo.claim_job_v2(self.worker_id)
                if not job:
                    try:
                        await asyncio.wait_for(
                            self.stop_requested.wait(), timeout=settings.worker_poll_seconds
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                if str(job.get("protocol_version") or "v1") == "v2" and job.get("request_id"):
                    await self._process_v2(job)
                else:
                    await self._process_legacy(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_error(
                    logger,
                    "worker_loop_error",
                    exc_info=True,
                    worker_id=self.worker_id,
                    failure_class="relay",
                    exception_type=type(exc).__name__,
                )
                try:
                    await asyncio.wait_for(
                        self.stop_requested.wait(), timeout=settings.worker_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        log_info(logger, "worker_drained", worker_id=self.worker_id)

    async def _process_v2(self, job: dict) -> None:
        job_id = str(job["id"])
        epoch = int(job.get("lease_epoch") or 0)
        started_ms = now_ms()
        log_info(
            logger,
            "job_claimed",
            worker_id=self.worker_id,
            job_id=job_id,
            request_id=job.get("request_id"),
            execution_pool=job.get("execution_pool"),
            lease_epoch=epoch,
            protocol_version="v2",
        )
        await self.repo.update_job(
            job_id,
            {
                "status": "running",
                "started_at": job.get("started_at") or utcnow().isoformat(),
            },
            lease_owner=self.worker_id,
        )
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job_id, epoch, stop))
        try:
            await self.queue.execute(job, self.worker_id)
            log_info(
                logger,
                "job_execution_returned",
                worker_id=self.worker_id,
                job_id=job_id,
                request_id=job.get("request_id"),
                duration_ms=elapsed_ms(started_ms),
            )
        finally:
            stop.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, job_id: str, lease_epoch: int, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.job_heartbeat_seconds)
                return
            except asyncio.TimeoutError:
                ok = await self.repo.renew_lease_v2(job_id, self.worker_id, lease_epoch)
                if not ok:
                    log_warning(logger, "lease_renew_failed", worker_id=self.worker_id, job_id=job_id, lease_epoch=lease_epoch)
                    return

    async def _process_legacy(self, job: dict) -> None:
        # Fusion/legacy stage semantics are quarantined in the compatibility
        # module. New v2 Session/Request execution never imports that runtime.
        if self._legacy is None:
            from .compatibility.legacy_worker import RelayWorker as LegacyRelayWorker

            self._legacy = LegacyRelayWorker()
        self._legacy.worker_id = self.worker_id
        log_info(logger, "legacy_job_claimed", worker_id=self.worker_id, job_id=job.get("id"))
        await self._legacy.process_job(job)


async def main() -> None:
    worker = RelayWorker()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        log_info(logger, "worker_stop_requested", worker_id=worker.worker_id)
        worker.stop_requested.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await worker.run_forever()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
