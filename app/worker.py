import asyncio
import json
import logging
import os
import socket
from contextlib import suppress
from datetime import timedelta
from typing import Any
from uuid import uuid4

from .config import get_settings
from .fusion_runtime import FusionRuntime, FusionRuntimeError, is_fusion_stage
from .providers.base import ProviderHTTPError
from .providers.openai_compatible import OpenAICompatibleResponsesProvider
from .providers.registry import ProviderRegistry
from .repository import RelayRepository
from .storage_paths import job_object_path, session_history_version_path
from .supabase import SupabaseBackend, SupabaseError
from .utils import compact_error_excerpt, json_bytes, truncate_utf8, utcnow


settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("model-relay-worker")


class RelayWorker:
    def __init__(self) -> None:
        self.backend = SupabaseBackend(settings)
        self.repo = RelayRepository(self.backend, settings)
        self.providers = ProviderRegistry(settings)
        self.fusion = FusionRuntime(self.backend, self.repo, self.providers, settings)
        self.worker_id = (
            f"{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"
        )

    async def close(self) -> None:
        await self.backend.close()

    async def run_forever(self) -> None:
        logger.info("worker_started id=%s", self.worker_id)
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
                logger.exception("worker_loop_error")
                await asyncio.sleep(settings.worker_poll_seconds)

    async def process_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        logger.info("job_claimed job_id=%s stage=%s", job_id, job.get("stage"))

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
        )
        if not updated:
            logger.warning("job_lost_before_start job_id=%s", job_id)
            return

        stop_heartbeat = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job_id, stop_heartbeat)
        )
        try:
            await asyncio.wait_for(
                self._execute_job(updated),
                timeout=settings.worker_max_runtime_seconds,
            )
        except asyncio.TimeoutError:
            await self._fail_job(
                updated,
                "UPSTREAM_TIMEOUT",
                f"Model execution exceeded {settings.worker_max_runtime_seconds} seconds",
            )
        except ProviderHTTPError as exc:
            await self._store_provider_error(updated, exc)
        except FusionRuntimeError as exc:
            await self._store_fusion_error(updated, exc)
        except SupabaseError as exc:
            await self._store_supabase_error(updated, exc)
        except Exception as exc:
            logger.exception("job_failed_unexpected job_id=%s", job_id)
            await self._fail_job(updated, "WORKER_ERROR", str(exc)[:2000])
        finally:
            stop_heartbeat.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(self, job_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.job_heartbeat_seconds
                )
                return
            except asyncio.TimeoutError:
                ok = await self.repo.renew_lease(job_id, self.worker_id)
                if not ok:
                    logger.warning("lease_renew_failed job_id=%s", job_id)
                    return

    async def _execute_job(self, job: dict[str, Any]) -> None:
        request_snapshot = await self.backend.storage_get_json(
            job["request_object_path"]
        )

        if is_fusion_stage(job.get("stage")):
            await self._execute_fusion_job(job, request_snapshot)
            return

        session = None
        material_prefix = request_snapshot.get("material_prefix")
        history: list[dict[str, Any]] = []
        expected_history_version = int(
            request_snapshot.get("expected_history_version") or 0
        )

        if job.get("relay_session_id"):
            session = await self.repo.get_session(job["relay_session_id"])
            if not session:
                await self._fail_job(job, "SESSION_MISSING", "Relay session not found")
                return

            signed_expiry = session.get("signed_url_expires_at")
            if signed_expiry:
                from datetime import datetime

                expiry = datetime.fromisoformat(signed_expiry.replace("Z", "+00:00"))
                if expiry <= utcnow() + timedelta(
                    seconds=settings.session_expiry_safety_seconds
                ):
                    await self._fail_job(
                        job,
                        "RELAY_SESSION_MATERIAL_EXPIRING",
                        "Material signed URLs are expiring; establish a new session",
                    )
                    return

            prefix_path = session.get("material_prefix_object_path")
            if prefix_path:
                material_prefix = await self.backend.storage_get_json(prefix_path)

            history_path = session.get("history_object_path")
            if history_path:
                loaded = await self.backend.storage_get_json(history_path)
                if isinstance(loaded, list):
                    history = loaded

        provider = self.providers.get(job["provider"])
        result = await provider.execute(
            request_snapshot,
            session=session,
            material_prefix=material_prefix,
            history=history,
        )

        tenant_id = job["tenant_id"]
        conversation_hash = job["conversation_hash"]
        raw_path = job_object_path(
            settings, tenant_id, conversation_hash, job["id"], "raw-response.json"
        )
        output_path = job_object_path(
            settings,
            tenant_id,
            conversation_hash,
            job["id"],
            "response-output.json",
        )
        await self.backend.storage_put(raw_path, result.raw_bytes)
        await self.backend.storage_put(output_path, json_bytes(result.response_output))

        # Cancellation after the upstream call must not mutate session history.
        latest = await self.repo.get_job(job["id"])
        if not latest or latest.get("status") == "cancelled":
            logger.info("job_cancelled_after_upstream job_id=%s", job["id"])
            return

        history_committed = False
        if session:
            prefix_has_query = bool(
                request_snapshot.get("material_prefix_includes_current_query")
            )
            user_item = None
            if not (request_snapshot.get("mode") == "new_session" and prefix_has_query):
                user_item = OpenAICompatibleResponsesProvider.make_user_item(
                    str(request_snapshot["current_query"])
                )
            new_history = list(history)
            new_history.append(
                {
                    "job_id": job["id"],
                    "created_at": utcnow().isoformat(),
                    "user_item": user_item,
                    "response_id": result.response_id,
                    "response_output": result.response_output,
                }
            )
            history_path = session_history_version_path(
                settings,
                tenant_id,
                conversation_hash,
                session["id"],
                expected_history_version + 1,
                job["id"],
            )
            await self.backend.storage_put(history_path, json_bytes(new_history))
            history_committed = await self.repo.commit_session_history(
                session_id=session["id"],
                expected_history_version=expected_history_version,
                history_object_path=history_path,
                provider=job["provider"],
                model=job["model"],
            )
            if not history_committed:
                await self._fail_job(
                    job,
                    "SESSION_CONFLICT",
                    "Session history changed while this job was running; result was archived but not committed",
                    raw_path=raw_path,
                )
                return

        full_text_path = None
        text = result.text
        text_truncated = False
        if len(text.encode("utf-8")) > settings.relay_result_soft_limit_bytes:
            full_text_path = job_object_path(
                settings,
                tenant_id,
                conversation_hash,
                job["id"],
                "visible-result.json",
            )
            await self.backend.storage_put(
                full_text_path,
                json_bytes({"text": text}),
            )
            text = truncate_utf8(text, settings.relay_result_preview_bytes)
            text_truncated = True

        compact_result = {
            "job_id": job["id"],
            "status": "succeeded",
            "relay_session_id": job.get("relay_session_id"),
            "text": text,
            "response_id": result.response_id,
            "usage": result.usage,
            "cached_tokens": result.cached_tokens,
            "history_committed": history_committed if session else False,
            "raw_response_stored": True,
            "text_truncated": text_truncated,
            "full_text_available": bool(full_text_path),
            "full_text_object_id": full_text_path,
        }

        # Defensive compact-result cap before it ever reaches Dify.
        if len(json_bytes(compact_result)) > settings.relay_result_hard_limit_bytes:
            compact_result["text"] = truncate_utf8(
                str(compact_result.get("text") or ""),
                min(settings.relay_result_preview_bytes, 196608),
            )
            compact_result["text_truncated"] = True

        latest = await self.repo.get_job(job["id"])
        if not latest or latest.get("status") == "cancelled":
            logger.info("job_cancelled_before_commit job_id=%s", job["id"])
            return

        await self.repo.update_job(
            job["id"],
            {
                "status": "succeeded",
                "raw_response_object_path": raw_path,
                "response_output_object_path": output_path,
                "compact_result": compact_result,
                "completed_at": utcnow().isoformat(),
                "heartbeat_at": utcnow().isoformat(),
                "error_code": None,
                "error_message": None,
            },
            lease_owner=self.worker_id,
        )
        logger.info("job_succeeded job_id=%s", job["id"])

    async def _execute_fusion_job(
        self, job: dict[str, Any], request_snapshot: dict[str, Any]
    ) -> None:
        result = await self.fusion.execute(job, request_snapshot)
        tenant_id = job["tenant_id"]
        conversation_hash = job["conversation_hash"]
        raw_path = job_object_path(
            settings, tenant_id, conversation_hash, job["id"], "raw-response.json"
        )
        output_path = job_object_path(
            settings, tenant_id, conversation_hash, job["id"], "response-output.json"
        )
        await self.backend.storage_put(raw_path, result.raw_bytes)
        await self.backend.storage_put(output_path, json_bytes(result.response_output))

        latest = await self.repo.get_job(job["id"])
        if not latest or latest.get("status") == "cancelled":
            logger.info("fusion_job_cancelled_before_commit job_id=%s", job["id"])
            return

        compact_result = {
            "job_id": job["id"],
            "status": "succeeded",
            "stage": job.get("stage"),
            "fusion_corpus_id": request_snapshot.get("fusion_corpus_id"),
            "artifact_id": result.artifact_id,
            "payload": result.payload,
            "response_id": result.response_id,
            "usage": result.usage or {},
            "cached_tokens": result.cached_tokens,
            "raw_response_stored": True,
        }
        compact_result.update(result.artifact_aliases)
        if len(json_bytes(compact_result)) > settings.relay_result_hard_limit_bytes:
            raise FusionRuntimeError(
                "FUSION_COMPACT_RESULT_TOO_LARGE",
                "Fusion compact result exceeded the configured hard limit; store a smaller artifact payload",
            )

        await self.repo.update_job(
            job["id"],
            {
                "status": "succeeded",
                "raw_response_object_path": raw_path,
                "response_output_object_path": output_path,
                "compact_result": compact_result,
                "completed_at": utcnow().isoformat(),
                "heartbeat_at": utcnow().isoformat(),
                "error_code": None,
                "error_message": None,
            },
            lease_owner=self.worker_id,
        )
        logger.info(
            "fusion_job_succeeded job_id=%s stage=%s artifact_id=%s",
            job["id"],
            job.get("stage"),
            result.artifact_id,
        )

    async def _store_fusion_error(
        self, job: dict[str, Any], exc: FusionRuntimeError
    ) -> None:
        raw_path = None
        if exc.raw_bytes:
            raw_path = job_object_path(
                settings,
                job["tenant_id"],
                job["conversation_hash"],
                job["id"],
                "fusion-error-response.json",
            )
            try:
                await self.backend.storage_put(raw_path, exc.raw_bytes)
            except Exception:
                raw_path = None
        await self._fail_job(job, exc.code, exc.message, raw_path=raw_path)

    async def _store_supabase_error(
        self, job: dict[str, Any], exc: SupabaseError
    ) -> None:
        path = job_object_path(
            settings,
            job["tenant_id"],
            job["conversation_hash"],
            job["id"],
            "supabase-error.json",
        )
        raw = json_bytes(
            {
                "status_code": exc.status_code,
                "message": str(exc),
                "body": str(exc.body or "")[:8192],
            }
        )
        try:
            await self.backend.storage_put(path, raw)
        except Exception:
            path = None
        message = f"Supabase HTTP {exc.status_code}: {str(exc.body or str(exc))[:4096]}"
        await self._fail_job(job, "SUPABASE_ERROR", message, raw_path=path)

    async def _store_provider_error(
        self, job: dict[str, Any], exc: ProviderHTTPError
    ) -> None:
        path = job_object_path(
            settings,
            job["tenant_id"],
            job["conversation_hash"],
            job["id"],
            "raw-error.json",
        )
        await self.backend.storage_put(
            path,
            exc.body,
            content_type="application/json",
        )
        if 400 <= exc.status_code < 500:
            code = "UPSTREAM_BAD_REQUEST"
        elif exc.status_code >= 500:
            code = "UPSTREAM_SERVER_ERROR"
        else:
            code = "UPSTREAM_HTTP_ERROR"
        message = (
            f"Upstream HTTP {exc.status_code}: "
            f"{compact_error_excerpt(exc.body, 4096)}"
        )
        await self._fail_job(job, code, message, raw_path=path)

    async def _fail_job(
        self,
        job: dict[str, Any],
        code: str,
        message: str,
        *,
        raw_path: str | None = None,
    ) -> None:
        latest = await self.repo.get_job(job["id"])
        if latest and latest.get("status") == "cancelled":
            return
        await self.repo.update_job(
            job["id"],
            {
                "status": "failed",
                "error_code": code,
                "error_message": message[:6000],
                "raw_response_object_path": raw_path,
                "completed_at": utcnow().isoformat(),
                "heartbeat_at": utcnow().isoformat(),
            },
            lease_owner=self.worker_id,
        )
        logger.error("job_failed job_id=%s code=%s", job["id"], code)


async def main() -> None:
    worker = RelayWorker()
    try:
        await worker.run_forever()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
