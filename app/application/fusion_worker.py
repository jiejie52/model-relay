from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import suppress
from typing import Any
from uuid import uuid4

from ..config import get_settings
from ..error_contract import dependency_http_error_meta, provider_http_error_meta, relay_error_meta, transport_error_meta
from ..providers.base import ProviderHTTPError, ProviderRequestError, ProviderTransportError
from ..providers.registry import ProviderRegistry
from ..storage_paths import job_object_path
from ..supabase import SupabaseBackend, SupabaseError
from ..utils import json_bytes, utcnow
from .fusion_repository import FusionRepository
from .fusion_runtime import FusionRuntime, FusionRuntimeError


settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("model-relay-fusion-worker")


class FusionLegacyWorker:
    """Legacy Fusion application worker, intentionally outside Relay Core."""

    def __init__(self) -> None:
        self.backend = SupabaseBackend(settings)
        self.repo = FusionRepository(self.backend, settings)
        self.providers = ProviderRegistry(settings)
        self.runtime = FusionRuntime(self.backend, self.repo, self.providers, settings)
        self.worker_id = f"fusion-{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"

    async def close(self) -> None:
        await self.backend.close()

    async def run_forever(self) -> None:
        logger.info("fusion_worker_started id=%s", self.worker_id)
        while True:
            try:
                job = await self.repo.claim_job(self.worker_id)
                if not job:
                    await asyncio.sleep(settings.worker_poll_seconds)
                    continue
                await self.process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fusion_worker_loop_error")
                await asyncio.sleep(settings.worker_poll_seconds)

    async def process_job(self, job: dict[str, Any]) -> None:
        fence = int(job.get("lease_fence") or 0)
        updated = await self.repo.update_job(
            job["id"],
            {
                "status": "running",
                "started_at": job.get("started_at") or utcnow().isoformat(),
                "heartbeat_at": utcnow().isoformat(),
            },
            lease_owner=self.worker_id,
            lease_fence=fence,
        )
        if not updated:
            return

        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(str(job["id"]), fence, stop))
        try:
            await asyncio.wait_for(
                self._execute_job(updated), timeout=settings.worker_max_runtime_seconds
            )
        except asyncio.TimeoutError as exc:
            await self._fail(
                updated,
                transport_error_meta(
                    provider=str(updated.get("provider") or "") or None,
                    service="fusion-provider-execution",
                    exception_type=type(exc).__name__,
                    message=f"Fusion execution exceeded {settings.worker_max_runtime_seconds} seconds",
                    cause_chain=[],
                ),
            )
        except ProviderHTTPError as exc:
            await self._provider_error(updated, exc)
        except ProviderTransportError as exc:
            await self._fail(
                updated,
                transport_error_meta(
                    provider=exc.provider,
                    service=exc.service,
                    exception_type=exc.exception_type,
                    message=exc.message,
                    cause_chain=exc.cause_chain,
                ),
            )
        except ProviderRequestError as exc:
            await self._fail(updated, relay_error_meta(exc.code, exc.message))
        except FusionRuntimeError as exc:
            raw_path = None
            if exc.raw_bytes:
                raw_path = job_object_path(
                    settings,
                    str(updated["tenant_id"]),
                    str(updated["conversation_hash"]),
                    str(updated["id"]),
                    "fusion-stage-error.bin",
                )
                try:
                    await self.backend.storage_put(
                        raw_path, exc.raw_bytes, content_type="application/octet-stream"
                    )
                except Exception:
                    raw_path = None
            await self._fail(
                updated,
                relay_error_meta(exc.code, exc.message),
                raw_response_path=raw_path,
            )
        except SupabaseError as exc:
            meta = dependency_http_error_meta(
                service="supabase",
                http_status=exc.status_code,
                response_headers=exc.headers,
                body=exc.body,
            )
            meta["exception"] = {"type": type(exc).__name__, "message": str(exc)}
            await self._fail(
                updated,
                meta,
                code="SUPABASE_ERROR",
                message=str(exc),
            )
        except Exception as exc:
            logger.exception("fusion_job_unexpected job_id=%s", updated["id"])
            await self._fail(
                updated,
                relay_error_meta("FUSION_WORKER_ERROR", f"{type(exc).__name__}: {exc}"),
            )
        finally:
            stop.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat_loop(self, job_id: str, fence: int, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.job_heartbeat_seconds)
                return
            except asyncio.TimeoutError:
                if not await self.repo.renew_lease(job_id, self.worker_id, fence):
                    return

    async def _execute_job(self, job: dict[str, Any]) -> None:
        snapshot = await self.backend.storage_get_json(job["request_object_path"])
        result = await self.runtime.execute(job, snapshot)
        raw_path = job_object_path(
            settings,
            str(job["tenant_id"]),
            str(job["conversation_hash"]),
            str(job["id"]),
            "raw-response.json",
        )
        output_path = job_object_path(
            settings,
            str(job["tenant_id"]),
            str(job["conversation_hash"]),
            str(job["id"]),
            "response-output.json",
        )
        await self.backend.storage_put(raw_path, result.raw_bytes)
        await self.backend.storage_put(output_path, json_bytes(result.response_output))

        latest = await self.repo.get_job(job["id"])
        if not latest or latest.get("status") == "cancelled":
            return

        compact = {
            "job_id": str(job["id"]),
            "status": "succeeded",
            "stage": job.get("stage"),
            "fusion_corpus_id": snapshot.get("fusion_corpus_id"),
            "artifact_id": result.artifact_id,
            "payload": result.payload,
            "response_id": result.response_id,
            "usage": result.usage or {},
            "cached_tokens": result.cached_tokens,
            "raw_response_stored": True,
        }
        compact.update(result.artifact_aliases)
        if len(json_bytes(compact)) > settings.relay_result_hard_limit_bytes:
            raise FusionRuntimeError(
                "FUSION_COMPACT_RESULT_TOO_LARGE",
                "Fusion compact result exceeded the configured hard limit",
            )

        await self.repo.update_job(
            job["id"],
            {
                "status": "succeeded",
                "raw_response_object_path": raw_path,
                "response_output_object_path": output_path,
                "compact_result": compact,
                "provider_response_id": result.response_id,
                "completed_at": utcnow().isoformat(),
                "heartbeat_at": utcnow().isoformat(),
                "error_code": None,
                "error_message": None,
                "raw_error_object_path": None,
                "raw_error_meta": None,
            },
            lease_owner=self.worker_id,
            lease_fence=int(job.get("lease_fence") or 0),
        )

    async def _provider_error(self, job: dict[str, Any], exc: ProviderHTTPError) -> None:
        path = job_object_path(
            settings,
            str(job["tenant_id"]),
            str(job["conversation_hash"]),
            str(job["id"]),
            "raw-error.bin",
        )
        try:
            await self.backend.storage_put(
                path, exc.body, content_type=exc.content_type or "application/octet-stream"
            )
        except Exception as store_exc:
            path = None
            archive_error = f"{type(store_exc).__name__}: {store_exc}"
        else:
            archive_error = None
        meta = provider_http_error_meta(
            provider=exc.provider or str(job.get("provider") or "") or None,
            service=exc.service,
            http_status=exc.status_code,
            response_headers=exc.headers,
            body_ref="raw-error" if path else "raw-error-unavailable",
            body=exc.body,
            request_id=exc.request_id,
        )
        if archive_error:
            meta["archive_error"] = archive_error
            meta["body_ref"] = None
        await self._fail(job, meta, raw_error_path=path, code=None, message=None)

    async def _fail(
        self,
        job: dict[str, Any],
        meta: dict[str, Any],
        *,
        raw_error_path: str | None = None,
        raw_response_path: str | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        latest = await self.repo.get_job(job["id"])
        if latest and latest.get("status") == "cancelled":
            return
        if meta.get("origin") == "relay":
            code = code if code is not None else meta.get("relay_code")
            message = message if message is not None else meta.get("relay_message")
        await self.repo.update_job(
            job["id"],
            {
                "status": "failed",
                "error_code": code,
                "error_message": message,
                "raw_error_object_path": raw_error_path,
                "raw_error_meta": meta,
                "raw_response_object_path": raw_response_path,
                "completed_at": utcnow().isoformat(),
                "heartbeat_at": utcnow().isoformat(),
            },
            lease_owner=self.worker_id,
            lease_fence=int(job.get("lease_fence") or 0),
        )
        logger.error(
            "fusion_job_failed job_id=%s origin=%s http_status=%s",
            job["id"],
            meta.get("origin"),
            meta.get("http_status"),
        )


async def main() -> None:
    worker = FusionLegacyWorker()
    try:
        await worker.run_forever()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
