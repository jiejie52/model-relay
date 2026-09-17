from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .config import get_settings
from .error_contract import dependency_http_error_meta, provider_http_error_meta, relay_error_meta, transport_error_meta
from .providers.base import (
    ProviderHTTPError,
    ProviderRequestError,
    ProviderTransportError,
)
from .providers.registry import ProviderRegistry
from .repository import RelayRepository
from .storage_paths import job_object_path, session_history_version_path
from .supabase import SupabaseBackend, SupabaseError
from .utils import json_bytes, truncate_utf8, utcnow


settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("model-relay-worker")


class RelayWorker:
    """Provider-neutral Relay Core worker.

    Fusion business stages are intentionally not imported here. Legacy Fusion
    jobs are consumed by `python -m app.application.fusion_worker`.
    """

    def __init__(self) -> None:
        self.backend = SupabaseBackend(settings)
        self.repo = RelayRepository(self.backend, settings)
        self.providers = ProviderRegistry(settings)
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"

    async def close(self) -> None:
        await self.backend.close()

    async def run_forever(self) -> None:
        logger.info("core_worker_started id=%s", self.worker_id)
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
                logger.exception("core_worker_loop_error")
                await asyncio.sleep(settings.worker_poll_seconds)

    async def process_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        fence = int(job.get("lease_fence") or 0)
        logger.info(
            "job_claimed job_id=%s engine=%s provider=%s fence=%s",
            job_id,
            job.get("execution_engine"),
            job.get("provider"),
            fence,
        )

        current = await self.repo.get_job(job_id)
        if not current or current.get("status") == "cancelled":
            return

        updated = await self.repo.update_job(
            job_id,
            {
                "status": "running",
                "started_at": current.get("started_at") or utcnow().isoformat(),
                "heartbeat_at": utcnow().isoformat(),
            },
            lease_owner=self.worker_id,
            lease_fence=fence,
        )
        if not updated:
            logger.warning("job_lost_before_start job_id=%s", job_id)
            return

        stop_heartbeat = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job_id, fence, stop_heartbeat)
        )
        try:
            await asyncio.wait_for(
                self._execute_job(updated), timeout=settings.worker_max_runtime_seconds
            )
        except asyncio.TimeoutError as exc:
            meta = transport_error_meta(
                provider=str(updated.get("provider") or "") or None,
                service="provider-execution",
                exception_type=type(exc).__name__,
                message=f"Model execution exceeded {settings.worker_max_runtime_seconds} seconds",
                cause_chain=[],
            )
            await self._fail_job(updated, meta)
        except ProviderHTTPError as exc:
            await self._store_provider_error(updated, exc)
        except ProviderTransportError as exc:
            meta = transport_error_meta(
                provider=exc.provider,
                service=exc.service,
                exception_type=exc.exception_type,
                message=exc.message,
                cause_chain=exc.cause_chain,
            )
            await self._fail_job(updated, meta)
        except ProviderRequestError as exc:
            await self._fail_job(updated, relay_error_meta(exc.code, exc.message))
        except SupabaseError as exc:
            meta = dependency_http_error_meta(
                service="supabase",
                http_status=exc.status_code,
                response_headers=exc.headers,
                body=exc.body,
            )
            meta["exception"] = {"type": type(exc).__name__, "message": str(exc)}
            await self._fail_job(updated, meta, code="SUPABASE_ERROR", message=str(exc))
        except Exception as exc:
            logger.exception("job_failed_unexpected job_id=%s", job_id)
            await self._fail_job(
                updated,
                relay_error_meta("WORKER_ERROR", f"{type(exc).__name__}: {exc}"),
            )
        finally:
            stop_heartbeat.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(
        self, job_id: str, lease_fence: int, stop: asyncio.Event
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.job_heartbeat_seconds
                )
                return
            except asyncio.TimeoutError:
                ok = await self.repo.renew_lease(
                    job_id, self.worker_id, lease_fence
                )
                if not ok:
                    logger.warning(
                        "lease_renew_failed job_id=%s fence=%s", job_id, lease_fence
                    )
                    return

    async def _execute_job(self, job: dict[str, Any]) -> None:
        snapshot = await self.backend.storage_get_json(job["request_object_path"])
        if not isinstance(snapshot, dict):
            raise ProviderRequestError(
                "REQUEST_SNAPSHOT_INVALID", "Relay request snapshot is not an object"
            )

        session: dict[str, Any] | None = None
        context = snapshot.get("material_prefix")
        history: list[dict[str, Any]] = []
        expected_history_version = int(
            snapshot.get("expected_history_version")
            if snapshot.get("expected_history_version") is not None
            else job.get("expected_history_version")
            or 0
        )
        history_mode = str(snapshot.get("history_mode") or "append")

        if job.get("relay_session_id"):
            session = await self.repo.get_session(job["relay_session_id"])
            if not session:
                raise ProviderRequestError("SESSION_MISSING", "Relay session not found")
            if str(session.get("provider") or "") != str(job.get("provider") or ""):
                raise ProviderRequestError(
                    "SESSION_PROVIDER_MISMATCH",
                    "Relay session provider cannot change across continuation",
                )
            adapter = self.providers.get(str(job["provider"]))
            if session.get("protocol") and session.get("protocol") != adapter.protocol:
                raise ProviderRequestError(
                    "SESSION_PROTOCOL_MISMATCH",
                    "Relay session protocol does not match provider adapter",
                )
            if session.get("history_codec") and session.get("history_codec") != adapter.history_codec:
                raise ProviderRequestError(
                    "SESSION_HISTORY_CODEC_MISMATCH",
                    "Relay session history codec does not match provider adapter",
                )
            history_mode = str(session.get("history_mode") or history_mode or "append")

            signed_expiry = session.get("signed_url_expires_at")
            if signed_expiry:
                expiry = datetime.fromisoformat(
                    str(signed_expiry).replace("Z", "+00:00")
                )
                if expiry <= utcnow() + timedelta(
                    seconds=settings.session_expiry_safety_seconds
                ):
                    raise ProviderRequestError(
                        "RELAY_SESSION_MATERIAL_EXPIRING",
                        "Material signed URLs are expiring; establish a new session",
                    )

            context_path = (
                snapshot.get("context_object_path")
                or session.get("context_object_path")
                or session.get("material_prefix_object_path")
            )
            if context_path:
                context = await self.backend.storage_get_json(context_path)

            if history_mode == "append":
                history_path = session.get("history_object_path")
                if history_path:
                    loaded = await self.backend.storage_get_json(history_path)
                    if isinstance(loaded, list):
                        history = loaded

        provider = self.providers.get(str(job["provider"]))
        result = await provider.execute(
            snapshot,
            session=session,
            material_prefix=context,
            history=history,
        )

        tenant_id = str(job["tenant_id"])
        conversation_hash = str(job["conversation_hash"])
        raw_path = job_object_path(
            settings, tenant_id, conversation_hash, str(job["id"]), "raw-response.json"
        )
        output_path = job_object_path(
            settings,
            tenant_id,
            conversation_hash,
            str(job["id"]),
            "response-output.json",
        )
        await self.backend.storage_put(raw_path, result.raw_bytes)
        await self.backend.storage_put(
            output_path, json_bytes(result.response_output)
        )

        latest = await self.repo.get_job(job["id"])
        if not latest or latest.get("status") == "cancelled":
            logger.info("job_cancelled_after_upstream job_id=%s", job["id"])
            return

        full_text_path = None
        text = result.text
        text_truncated = False
        if len(text.encode("utf-8")) > settings.relay_result_soft_limit_bytes:
            full_text_path = job_object_path(
                settings,
                tenant_id,
                conversation_hash,
                str(job["id"]),
                "visible-result.json",
            )
            await self.backend.storage_put(full_text_path, json_bytes({"text": text}))
            text = truncate_utf8(text, settings.relay_result_preview_bytes)
            text_truncated = True

        compact_result = {
            "job_id": str(job["id"]),
            "status": "succeeded",
            "relay_session_id": job.get("relay_session_id"),
            "text": text,
            "response_id": result.response_id,
            "usage": result.usage,
            "cached_tokens": result.cached_tokens,
            "finish_reason": result.finish_reason,
            "history_committed": False,
            "raw_response_stored": True,
            "text_truncated": text_truncated,
            "full_text_available": bool(full_text_path),
            "full_text_object_id": full_text_path,
        }
        if len(json_bytes(compact_result)) > settings.relay_result_hard_limit_bytes:
            compact_result["text"] = truncate_utf8(
                str(compact_result.get("text") or ""),
                min(settings.relay_result_preview_bytes, 196608),
            )
            compact_result["text_truncated"] = True

        if session and history_mode == "append":
            record = result.history_record
            if record is None:
                # Compatibility fallback for adapters/jobs created before v2.
                record = {
                    "codec": str(session.get("history_codec") or "legacy"),
                    "job_id": str(job["id"]),
                    "response_id": result.response_id,
                    "response_output": result.response_output,
                }
            record = dict(record)
            record.setdefault("job_id", str(job["id"]))
            record.setdefault("created_at", utcnow().isoformat())
            new_history = list(history)
            new_history.append(record)
            history_path = session_history_version_path(
                settings,
                tenant_id,
                conversation_hash,
                str(session["id"]),
                expected_history_version + 1,
                str(job["id"]),
            )
            await self.backend.storage_put(history_path, json_bytes(new_history))
            compact_result["history_committed"] = True

            # New jobs reserve active_job_id. The RPC validates session version,
            # lease owner and fence before atomically closing Session+Job.
            if str(session.get("active_job_id") or "") == str(job["id"]):
                ok = await self.repo.commit_session_and_job_success(
                    session_id=str(session["id"]),
                    job_id=str(job["id"]),
                    expected_history_version=expected_history_version,
                    history_object_path=history_path,
                    provider=str(job["provider"]),
                    model=str(job["model"]),
                    lease_owner=self.worker_id,
                    lease_fence=int(job.get("lease_fence") or 0),
                    raw_response_object_path=raw_path,
                    response_output_object_path=output_path,
                    compact_result=compact_result,
                    provider_response_id=result.response_id,
                )
                if not ok:
                    await self._fail_job(
                        job,
                        relay_error_meta(
                            "SESSION_CONFLICT",
                            "Session history or lease changed before atomic success commit",
                        ),
                        raw_response_path=raw_path,
                    )
                    return
                logger.info("job_succeeded_atomic job_id=%s", job["id"])
                return

            # Historical jobs created before active-job reservation use the old CAS
            # so their existing job_id remains resumable after upgrade.
            committed = await self.repo.commit_session_history(
                session_id=str(session["id"]),
                expected_history_version=expected_history_version,
                history_object_path=history_path,
                provider=str(job["provider"]),
                model=str(job["model"]),
            )
            if not committed:
                await self._fail_job(
                    job,
                    relay_error_meta(
                        "SESSION_CONFLICT",
                        "Session history changed while this legacy job was running",
                    ),
                    raw_response_path=raw_path,
                )
                return

        latest = await self.repo.get_job(job["id"])
        if not latest or latest.get("status") == "cancelled":
            return
        await self.repo.update_job(
            job["id"],
            {
                "status": "succeeded",
                "raw_response_object_path": raw_path,
                "response_output_object_path": output_path,
                "compact_result": compact_result,
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
        logger.info("job_succeeded job_id=%s", job["id"])

    async def _store_provider_error(
        self, job: dict[str, Any], exc: ProviderHTTPError
    ) -> None:
        path = job_object_path(
            settings,
            str(job["tenant_id"]),
            str(job["conversation_hash"]),
            str(job["id"]),
            "raw-error.bin",
        )
        content_type = exc.content_type or "application/octet-stream"
        storage_error: str | None = None
        try:
            await self.backend.storage_put(path, exc.body, content_type=content_type)
        except Exception as store_exc:
            # Storage failure must not replace the original provider failure.
            storage_error = f"{type(store_exc).__name__}: {store_exc}"
            path = None

        meta = provider_http_error_meta(
            provider=exc.provider or str(job.get("provider") or "") or None,
            service=exc.service,
            http_status=exc.status_code,
            response_headers=exc.headers,
            body_ref="raw-error" if path else "raw-error-unavailable",
            body=exc.body,
            request_id=exc.request_id,
            received_complete=True,
        )
        if storage_error:
            meta["archive_error"] = storage_error
            # Inline body is still not stored in DB to avoid silently truncating
            # large provider errors. The metadata truthfully reports unavailability.
            meta["body_ref"] = None
        await self._fail_job(
            job,
            meta,
            raw_error_path=path,
            code=None,
            message=None,
        )

    async def _fail_job(
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
        if job.get("relay_session_id"):
            await self.repo.release_session_job(
                session_id=str(job["relay_session_id"]), job_id=str(job["id"])
            )
        logger.error(
            "job_failed job_id=%s origin=%s http_status=%s",
            job["id"],
            meta.get("origin"),
            meta.get("http_status"),
        )


async def main() -> None:
    worker = RelayWorker()
    try:
        await worker.run_forever()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
