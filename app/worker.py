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
from .persistence.object_storage import StorageRegistry
from .persistence.supabase_storage import SupabaseObjectStorage
from .providers.moonshot_chat import MoonshotChatAdapter
from .providers.registry import ProviderRegistry
from .providers.responses_v2 import ResponsesV2Adapter
from .supabase import SupabaseBackend
from .v2_repository import RelayV2Repository
from .utils import utcnow


settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("model-relay-worker")


class RelayWorker:
    def __init__(self) -> None:
        self.backend = SupabaseBackend(settings)
        self.repo = RelayV2Repository(self.backend, settings)
        self.storage = StorageRegistry(SupabaseObjectStorage(self.backend, settings))
        self.materials = MaterialResolver(self.repo, self.storage, settings)
        self.providers = ProviderRegistry(settings)
        self.providers.validate_enabled_connections()
        self.providers.register_v2(
            "aihubmix_default",
            ResponsesV2Adapter(self.providers.openai_compatible, self.materials),
        )
        self.providers.register_v2(
            "moonshot_official",
            MoonshotChatAdapter(settings, self.materials, self.repo),
        )
        self.runtime = SharedExecutionRuntime(
            self.repo, self.storage, self.providers, self.materials, settings
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
        logger.info(
            "worker_started id=%s pools=%s",
            self.worker_id,
            sorted(settings.worker_pool_set),
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
            except Exception:
                logger.exception("worker_loop_error")
                try:
                    await asyncio.wait_for(
                        self.stop_requested.wait(), timeout=settings.worker_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        logger.info("worker_drained id=%s", self.worker_id)

    async def _process_v2(self, job: dict) -> None:
        job_id = str(job["id"])
        epoch = int(job.get("lease_epoch") or 0)
        logger.info(
            "v2_job_claimed job_id=%s request_id=%s pool=%s epoch=%s",
            job_id,
            job.get("request_id"),
            job.get("execution_pool"),
            epoch,
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
                    logger.warning("v2_lease_renew_failed job_id=%s epoch=%s", job_id, lease_epoch)
                    return

    async def _process_legacy(self, job: dict) -> None:
        # Fusion/legacy stage semantics are quarantined in the compatibility
        # module. New v2 Session/Request execution never imports that runtime.
        if self._legacy is None:
            from .compatibility.legacy_worker import RelayWorker as LegacyRelayWorker

            self._legacy = LegacyRelayWorker()
        self._legacy.worker_id = self.worker_id
        logger.info("legacy_job_claimed job_id=%s", job.get("id"))
        await self._legacy.process_job(job)


async def main() -> None:
    worker = RelayWorker()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        logger.info("worker_stop_requested id=%s", worker.worker_id)
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
