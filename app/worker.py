from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import signal
import time
from contextlib import nullcontext, suppress
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status

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


WORKER_VERSION = "1.0.0"

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
        self._wake_requested = asyncio.Event()
        self._follow_api_enabled = settings.worker_follow_api_enabled
        self._active_until = (
            time.monotonic() + settings.worker_api_activity_grace_seconds
            if self._follow_api_enabled
            else float("inf")
        )
        self._quiescent = False
        self._busy = False
        self._control_server: uvicorn.Server | None = None

    @property
    def lifecycle_state(self) -> str:
        if self._busy:
            return "busy"
        if self._quiescent:
            return "quiescent"
        return "active"

    def note_api_activity(self, *, reason: str | None = None) -> None:
        if not self._follow_api_enabled:
            return
        was_quiescent = self._quiescent
        self._active_until = max(
            self._active_until,
            time.monotonic() + settings.worker_api_activity_grace_seconds,
        )
        self._wake_requested.set()
        if was_quiescent:
            log_info(
                logger,
                "worker_wake_signal_received",
                worker_id=self.worker_id,
                reason=reason,
            )

    def request_stop(self) -> None:
        self.stop_requested.set()
        self._wake_requested.set()
        if self._control_server is not None:
            self._control_server.should_exit = True

    def disable_api_follow(self, *, reason: str) -> None:
        if not self._follow_api_enabled:
            return
        self._follow_api_enabled = False
        self._active_until = float("inf")
        self._wake_requested.set()
        log_error(
            logger,
            "worker_api_follow_disabled",
            worker_id=self.worker_id,
            reason=reason,
            failure_class="relay",
        )

    def control_app(self) -> FastAPI:
        app = FastAPI(
            title="Model Relay Worker Control",
            version=WORKER_VERSION,
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @app.get("/health")
        async def health() -> dict[str, object]:
            return {
                "ok": True,
                "service": "relay-worker",
                "version": WORKER_VERSION,
                "worker_id": self.worker_id,
                "state": self.lifecycle_state,
                "follow_api": self._follow_api_enabled,
            }

        @app.post("/wake", status_code=status.HTTP_202_ACCEPTED)
        async def wake(
            authorization: str | None = Header(default=None, alias="Authorization"),
            reason: str | None = Header(default=None, alias="X-Relay-Wake-Reason"),
        ) -> dict[str, object]:
            expected = f"Bearer {settings.relay_api_token.get_secret_value()}"
            if authorization is None or not secrets.compare_digest(authorization, expected):
                raise HTTPException(status_code=401, detail="Unauthorized")
            self.note_api_activity(reason=reason)
            return {
                "ok": True,
                "state": self.lifecycle_state,
                "worker_id": self.worker_id,
            }

        return app

    async def run_control_server(self) -> None:
        config = uvicorn.Config(
            self.control_app(),
            host=settings.worker_control_host,
            port=settings.worker_control_port,
            log_level=str(settings.log_level or "INFO").lower(),
            access_log=False,
        )
        server = uvicorn.Server(config)
        # The control server runs inside the Worker's existing event loop. Keep
        # the main Worker signal handlers authoritative so SIGTERM means
        # "drain current work, then exit" rather than letting Uvicorn replace
        # them. Uvicorn >=0.35 uses capture_signals(); keep a fallback for
        # older compatible releases that still expose install_signal_handlers().
        if hasattr(server, "capture_signals"):
            server.capture_signals = lambda: nullcontext()
        elif hasattr(server, "install_signal_handlers"):
            server.install_signal_handlers = lambda: None
        self._control_server = server
        log_info(
            logger,
            "worker_control_started",
            worker_id=self.worker_id,
            host=settings.worker_control_host,
            port=settings.worker_control_port,
        )
        await server.serve()

    async def close(self) -> None:
        if self._legacy is not None:
            await self._legacy.close()
        await self.backend.close()

    async def run_forever(self) -> None:
        log_info(
            logger,
            "worker_started",
            worker_id=self.worker_id,
            version=WORKER_VERSION,
            deployment_id=settings.deployment_id,
            execution_pools=sorted(settings.worker_pool_set),
            route_revision=self.route_catalog.revision,
            route_catalog_hash=self.route_catalog.catalog_hash,
            route_providers=self.route_catalog.providers(),
            connection_policy=settings.connection_availability_mode,
            configured_connections=self.providers.registered_connections(),
            configured_file_connections=self.file_adapters.registered_connections(),
            follow_api=self._follow_api_enabled,
            api_activity_grace_seconds=settings.worker_api_activity_grace_seconds,
        )
        while not self.stop_requested.is_set():
            try:
                job = await self.repo.claim_job_v2(self.worker_id)
                if not job:
                    if self._follow_api_enabled and time.monotonic() >= self._active_until:
                        if await self._has_unfinished_pool_job():
                            # A live or stale lease may belong to this process, a
                            # prior Worker incarnation, or another replica. Keep the
                            # service active until every in-pool task is resolved or
                            # becomes claimable/recoverable.
                            try:
                                await asyncio.wait_for(
                                    self.stop_requested.wait(),
                                    timeout=settings.worker_poll_seconds,
                                )
                            except asyncio.TimeoutError:
                                pass
                            continue
                        await self._wait_quiescent()
                        continue
                    timeout = settings.worker_poll_seconds
                    if self._follow_api_enabled:
                        remaining = max(0.0, self._active_until - time.monotonic())
                        timeout = min(timeout, max(0.05, remaining))
                    try:
                        await asyncio.wait_for(self.stop_requested.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                    continue

                self._busy = True
                self._quiescent = False
                try:
                    if str(job.get("protocol_version") or "v1") == "v2" and job.get("request_id"):
                        await self._process_v2(job)
                    else:
                        await self._process_legacy(job)
                finally:
                    self._busy = False
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

    async def _has_unfinished_pool_job(self) -> bool:
        # claim_relay_job_v2() can recover an expired lease, but it intentionally
        # cannot take over a still-valid lease. This guard is therefore broader
        # than "owned by this worker": after a restart, an unfinished lease may
        # still carry the previous process's worker_id. Sleeping before that lease
        # expires would strand recovery until unrelated API traffic arrives.
        row = None
        for execution_pool in sorted(settings.worker_pool_set):
            rows = await self.backend.select(
                "relay_jobs",
                filters={
                    "execution_pool": f"eq.{execution_pool}",
                    "status": "in.(leased,running)",
                    "expires_at": f"gt.{utcnow().isoformat()}",
                },
                select="id,status,execution_pool,lease_owner,lease_expires_at",
                limit=1,
            )
            if rows:
                row = rows[0]
                break
        if row is None:
            return False
        log_warning(
            logger,
            "worker_sleep_deferred_unfinished_job",
            worker_id=self.worker_id,
            job_id=row.get("id"),
            job_status=row.get("status"),
            job_execution_pool=row.get("execution_pool"),
            job_lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
        )
        return True

    async def _wait_quiescent(self) -> None:
        # The immediately preceding claim returned no job, so the serial Worker
        # has completed its current task and drained every claimable queued task.
        # From here until /wake, perform no DB/provider polling: Railway can then
        # detect network inactivity and suspend this service safely.
        self._quiescent = True
        self._wake_requested.clear()
        if time.monotonic() < self._active_until:
            self._quiescent = False
            return
        log_info(
            logger,
            "worker_quiescent",
            worker_id=self.worker_id,
            reason="api_inactive_and_queue_drained",
        )

        wake_task = asyncio.create_task(self._wake_requested.wait())
        stop_task = asyncio.create_task(self.stop_requested.wait())
        try:
            done, pending = await asyncio.wait(
                {wake_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._quiescent = False

        if not self.stop_requested.is_set():
            log_info(
                logger,
                "worker_resumed",
                worker_id=self.worker_id,
                reason="api_activity",
            )

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
    control_task: asyncio.Task[None] | None = None

    def request_stop() -> None:
        log_info(logger, "worker_stop_requested", worker_id=worker.worker_id)
        worker.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    if worker._follow_api_enabled:
        control_task = asyncio.create_task(worker.run_control_server())

        def control_done(task: asyncio.Task[None]) -> None:
            if worker.stop_requested.is_set():
                return
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                return
            reason = f"control_server_stopped:{type(exc).__name__ if exc else 'clean_exit'}"
            worker.disable_api_follow(reason=reason)

        control_task.add_done_callback(control_done)

    try:
        await worker.run_forever()
    finally:
        worker.request_stop()
        if control_task is not None:
            if worker._control_server is not None:
                worker._control_server.should_exit = True
            try:
                await asyncio.wait_for(control_task, timeout=5.0)
            except asyncio.TimeoutError:
                control_task.cancel()
                with suppress(asyncio.CancelledError):
                    await control_task
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
