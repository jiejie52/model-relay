from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from contextlib import suppress
from typing import Any
from uuid import uuid4

from .config import get_settings
from .errors.service import RawErrorService
from .materials.resolver import MaterialResolver
from .providers.base import (
    ProviderHTTPError,
    ProviderRequestError,
    ProviderResult,
    ProviderTransportError,
)
from .providers.registry import ProviderRegistry
from .repository import RelayRepository
from .storage.execution_archive import ExecutionArchiveStore
from .storage.uploaded_files import UploadedFileStore
from .storage_paths import (
    job_object_path,
    v2_history_path,
    v2_job_attempt_result_path,
    v2_job_normalized_result_path,
)
from .structured_output import (
    StructuredOutputError,
    resolve_structured_output,
    validate_against_schema,
)
from .supabase import SupabaseBackend, SupabaseError
from .utils import json_bytes, utcnow

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("model-relay-v2-worker")


class DuplicateJSONKeyError(ValueError):
    pass


def _strict_json_loads(text: str) -> Any:
    def hook(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise DuplicateJSONKeyError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    return json.loads(text, object_pairs_hook=hook)


def _result_archive_dict(result: ProviderResult) -> dict[str, Any]:
    return {
        "text": result.text,
        "response_id": result.response_id,
        "usage": result.usage,
        "cached_tokens": result.cached_tokens,
        "response_output": result.response_output,
        "history_delta": result.history_delta,
        "finish_reason": result.finish_reason,
        "result_type": result.result_type,
        "wire_request_hash": result.wire_request_hash,
        "applied_generation": result.applied_generation,
        "provider_metadata": result.provider_metadata,
    }


def _result_from_archive(raw_bytes: bytes, archived: dict[str, Any]) -> ProviderResult:
    try:
        raw_json = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        raw_json = {}
    return ProviderResult(
        raw_bytes=raw_bytes,
        raw_json=raw_json if isinstance(raw_json, dict) else {},
        text=str(archived.get("text") or ""),
        response_id=archived.get("response_id"),
        usage=archived.get("usage") if isinstance(archived.get("usage"), dict) else {},
        cached_tokens=archived.get("cached_tokens") if isinstance(archived.get("cached_tokens"), int) else None,
        response_output=archived.get("response_output") if isinstance(archived.get("response_output"), list) else [],
        history_delta=archived.get("history_delta"),
        finish_reason=archived.get("finish_reason"),
        result_type=str(archived.get("result_type") or "message"),
        wire_request_hash=archived.get("wire_request_hash"),
        applied_generation=archived.get("applied_generation") if isinstance(archived.get("applied_generation"), dict) else {},
        provider_metadata=archived.get("provider_metadata") if isinstance(archived.get("provider_metadata"), dict) else {},
    )


class RelayWorker:
    """V2 core worker.

    It never imports Fusion/Dify application modules. Legacy/Fusion jobs are
    consumed by app.legacy_worker after 003_relay_v2_schema.sql separates claims.
    """

    def __init__(self) -> None:
        self.backend = SupabaseBackend(settings)
        self.repo = RelayRepository(self.backend, settings)
        self.archive = ExecutionArchiveStore(self.backend)
        self.errors = RawErrorService(self.repo, self.archive, settings)
        self.providers = ProviderRegistry(settings)
        self.material_store = UploadedFileStore(settings)
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"

    async def close(self) -> None:
        await self.backend.close()

    async def run_forever(self) -> None:
        logger.info("v2_worker_started id=%s engine=%s", self.worker_id, settings.execution_engine)
        while True:
            try:
                job = await self.repo.claim_job_v2(self.worker_id)
                if not job:
                    await asyncio.sleep(settings.worker_poll_seconds)
                    continue
                await self.process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("v2_worker_loop_error")
                await asyncio.sleep(settings.worker_poll_seconds)

    async def process_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        lease_token = str(job.get("lease_token") or "")
        if not lease_token:
            logger.error("v2_job_without_lease_token job_id=%s", job_id)
            return
        updated = await self.repo.update_job_v2_fenced(
            job_id,
            {
                "status": "running",
                "started_at": job.get("started_at") or utcnow().isoformat(),
                "heartbeat_at": utcnow().isoformat(),
            },
            worker_id=self.worker_id,
            lease_token=lease_token,
        )
        if not updated:
            return

        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(job_id, lease_token, stop))
        try:
            await asyncio.wait_for(
                self._execute_job(updated, lease_token),
                timeout=settings.worker_max_runtime_seconds,
            )
        except asyncio.TimeoutError:
            await self._handle_timeout(updated, lease_token)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("v2_job_unhandled job_id=%s", job_id)
            await self._record_native_failure(
                updated,
                lease_token,
                code="WORKER_ERROR",
                message="Unexpected Relay worker exception; inspect server logs",
                phase="worker_error",
            )
        finally:
            stop.set()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat_loop(self, job_id: str, lease_token: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.job_heartbeat_seconds)
                return
            except asyncio.TimeoutError:
                ok = await self.repo.renew_lease_v2(job_id, self.worker_id, lease_token)
                if not ok:
                    logger.warning("v2_lease_lost job_id=%s", job_id)
                    return

    async def _execute_job(self, job: dict[str, Any], lease_token: str) -> None:
        job_id = str(job["id"])
        tenant_id = str(job["tenant_id"])
        conversation_hash = str(job["conversation_hash"])
        session_id = str(job.get("relay_session_id") or "")
        if not session_id:
            await self._record_native_failure(job, lease_token, code="SESSION_MISSING", message="V2 job has no session", phase="validation")
            return

        request_snapshot = await self.archive.get_json(job["request_object_path"])
        session = await self.repo.get_session(
            session_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
        )
        if not session or session.get("schema_version") != "relay-session/2.0":
            await self._record_native_failure(job, lease_token, code="SESSION_MISSING", message="V2 session was not found", phase="validation")
            return

        profile_name = str(session.get("upstream_profile") or "")
        try:
            profile = self.providers.validate_session_model(profile_name, str(session["provider"]), str(job["model"]))
            adapter = self.providers.get_v2(profile_name)
        except (KeyError, ValueError) as exc:
            await self._record_native_failure(job, lease_token, code="PROVIDER_PROFILE_INVALID", message=str(exc), phase="validation")
            return

        context: list[dict[str, Any]] = []
        if session.get("context_object_path"):
            loaded = await self.archive.get_json(session["context_object_path"])
            if isinstance(loaded, list):
                context = loaded
        history: Any = []
        if session.get("history_object_path"):
            history = await self.archive.get_json(session["history_object_path"])

        resolver = MaterialResolver(
            self.repo,
            self.material_store,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            session_id=session_id,
            lease_owner=f"{self.worker_id}:{job_id}",
            binding_wait_seconds=settings.binding_prepare_wait_seconds,
        )

        # A prior worker may have archived the provider response and died before
        # the DB transaction. Recover that artifact before considering a send.
        recovered = await self._try_recover_archived(job, request_snapshot, session, history, adapter, lease_token)
        if recovered:
            return

        latest = await self.repo.get_job(job_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
        if latest and latest.get("execution_phase") == "dispatch_started":
            # There is no archived response to prove the outcome. Do not blind retry.
            await self._record_native_failure(
                latest,
                lease_token,
                code="DELIVERY_OUTCOME_UNKNOWN",
                message="Provider dispatch started but no complete response was durably archived; automatic resend is disabled",
                phase="delivery_unknown",
                delivery_status="unknown",
            )
            return

        dispatched = False

        async def before_dispatch(wire_request_hash: str) -> None:
            nonlocal dispatched
            updated = await self.repo.update_job_v2_fenced(
                job_id,
                {
                    "execution_phase": "dispatch_started",
                    "delivery_status": "dispatch_started",
                    "wire_request_hash": wire_request_hash,
                    "heartbeat_at": utcnow().isoformat(),
                },
                worker_id=self.worker_id,
                lease_token=lease_token,
            )
            if not updated:
                raise ProviderRequestError("LEASE_LOST", "Worker lost its lease before provider dispatch")
            dispatched = True

        await self.repo.update_job_v2_fenced(
            job_id,
            {"execution_phase": "preparing_provider", "delivery_status": "not_sent"},
            worker_id=self.worker_id,
            lease_token=lease_token,
        )
        try:
            result = await adapter.execute_v2(
                request_snapshot,
                session=session,
                context=context,
                history=history,
                material_resolver=resolver,
                before_dispatch=before_dispatch,
            )
        except ProviderHTTPError as exc:
            error_row = await self.errors.capture_provider_http(
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                exc=exc,
                provider=profile.provider,
            )
            await self.repo.record_job_failure_v2(
                job_id=job_id,
                session_id=session_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                worker_id=self.worker_id,
                lease_token=lease_token,
                error_id=error_row.get("id"),
                error_code=self._native_error_code(exc.body),
                error_message=self._native_error_message(exc.body),
                execution_phase="provider_error",
                delivery_status="response_received",
            )
            return
        except ProviderTransportError as exc:
            error_row = await self.errors.capture_transport(
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                exc=exc,
                provider=profile.provider,
            )
            await self.repo.record_job_failure_v2(
                job_id=job_id,
                session_id=session_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                worker_id=self.worker_id,
                lease_token=lease_token,
                error_id=error_row.get("id"),
                error_code=None,
                error_message=str(exc),
                execution_phase="delivery_unknown" if dispatched else "provider_prepare_failed",
                delivery_status="unknown" if dispatched else "not_sent",
            )
            return
        except ProviderRequestError as exc:
            await self._record_native_failure(
                job,
                lease_token,
                code=exc.code,
                message=exc.message,
                phase="provider_prepare_failed" if not dispatched else "provider_validation_failed",
                delivery_status="unknown" if dispatched else "not_sent",
            )
            return

        raw_path = job_object_path(settings, tenant_id, conversation_hash, job_id, "raw-response.json")
        attempt_path = v2_job_attempt_result_path(settings, tenant_id, conversation_hash, job_id)
        try:
            await self.archive.put_bytes(raw_path, result.raw_bytes, content_type="application/json", upsert=True)
            await self.archive.put_json(attempt_path, _result_archive_dict(result), upsert=True)
            fenced = await self.repo.update_job_v2_fenced(
                job_id,
                {
                    "execution_phase": "response_archived",
                    "delivery_status": "response_archived",
                    "raw_response_object_path": raw_path,
                    "wire_request_hash": result.wire_request_hash,
                },
                worker_id=self.worker_id,
                lease_token=lease_token,
            )
            if not fenced:
                logger.warning("response_archived_but_lease_lost job_id=%s", job_id)
                return
        except SupabaseError as exc:
            error_row = await self.errors.capture_supabase(
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                exc=exc,
            )
            await self.repo.record_job_failure_v2(
                job_id=job_id,
                session_id=session_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                worker_id=self.worker_id,
                lease_token=lease_token,
                error_id=error_row.get("id"),
                error_code=None,
                error_message=str(exc),
                execution_phase="delivery_unknown",
                delivery_status="unknown",
            )
            return

        await self._validate_and_commit(job, request_snapshot, session, history, result, raw_path, lease_token)

    async def _try_recover_archived(
        self,
        job: dict[str, Any],
        request_snapshot: dict[str, Any],
        session: dict[str, Any],
        history: Any,
        adapter: Any,
        lease_token: str,
    ) -> bool:
        phase = str(job.get("execution_phase") or "")
        delivery = str(job.get("delivery_status") or "")
        if phase not in {"response_archived", "validating", "dispatch_started"} and delivery != "response_archived":
            return False
        tenant_id = str(job["tenant_id"])
        conversation_hash = str(job["conversation_hash"])
        job_id = str(job["id"])
        raw_path = job_object_path(settings, tenant_id, conversation_hash, job_id, "raw-response.json")
        attempt_path = v2_job_attempt_result_path(settings, tenant_id, conversation_hash, job_id)
        try:
            raw = await self.archive.get_bytes(raw_path)
            archived = await self.archive.get_json(attempt_path)
        except SupabaseError as exc:
            if exc.status_code in {400, 404}:
                return False
            raise
        if not isinstance(archived, dict):
            return False
        result = _result_from_archive(raw, archived)
        await self.repo.update_job_v2_fenced(
            job_id,
            {"execution_phase": "response_archived", "delivery_status": "response_archived", "raw_response_object_path": raw_path},
            worker_id=self.worker_id,
            lease_token=lease_token,
        )
        await self._validate_and_commit(job, request_snapshot, session, history, result, raw_path, lease_token)
        return True

    async def _validate_and_commit(
        self,
        job: dict[str, Any],
        request_snapshot: dict[str, Any],
        session: dict[str, Any],
        history: Any,
        result: ProviderResult,
        raw_path: str,
        lease_token: str,
    ) -> None:
        job_id = str(job["id"])
        tenant_id = str(job["tenant_id"])
        conversation_hash = str(job["conversation_hash"])
        session_id = str(session["id"])
        await self.repo.update_job_v2_fenced(
            job_id,
            {"execution_phase": "validating"},
            worker_id=self.worker_id,
            lease_token=lease_token,
        )
        try:
            spec = resolve_structured_output(request_snapshot, fallback_name="relay_output")
            structured_value = None
            if spec is not None:
                if not result.text.strip():
                    raise StructuredOutputError("STRUCTURED_OUTPUT_EMPTY", "Provider returned no structured-output text")
                try:
                    structured_value = _strict_json_loads(result.text)
                except DuplicateJSONKeyError as exc:
                    raise StructuredOutputError("STRUCTURED_OUTPUT_DUPLICATE_KEY", str(exc)) from exc
                except json.JSONDecodeError as exc:
                    raise StructuredOutputError("STRUCTURED_OUTPUT_INVALID_JSON", str(exc)) from exc
                if spec.mode == "json_object" and not isinstance(structured_value, dict):
                    raise StructuredOutputError("STRUCTURED_OUTPUT_VALIDATION_FAILED", "Provider JSON output is not an object")
                validate_against_schema(structured_value, spec)
        except StructuredOutputError as exc:
            diagnostic = json_bytes({"code": exc.code, "message": exc.message, "raw_response_object_path": raw_path})
            error_row = await self.errors.capture_http(
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                origin="relay",
                provider=str(job["provider"]),
                service="structured_output_validation",
                status_code=None,
                headers=[],
                body=diagnostic,
                content_type="application/json",
                content_encoding=None,
                received_complete=True,
            )
            await self.repo.record_job_failure_v2(
                job_id=job_id,
                session_id=session_id,
                tenant_id=tenant_id,
                conversation_hash=conversation_hash,
                worker_id=self.worker_id,
                lease_token=lease_token,
                error_id=error_row.get("id"),
                error_code=exc.code,
                error_message=exc.message,
                execution_phase="validation_failed",
                delivery_status="response_archived",
                raw_response_object_path=raw_path,
            )
            return

        normalized = {
            "job_id": job_id,
            "session_id": session_id,
            "status": "succeeded",
            "provider": job["provider"],
            "model": job["model"],
            "text": result.text,
            "structured": structured_value,
            "response_id": result.response_id,
            "usage": result.usage,
            "cached_tokens": result.cached_tokens,
            "finish_reason": result.finish_reason,
            "result_type": result.result_type,
            "applied_generation": result.applied_generation,
            "provider_metadata": result.provider_metadata,
        }
        normalized_path = v2_job_normalized_result_path(settings, tenant_id, conversation_hash, job_id)
        await self.archive.put_json(normalized_path, normalized, upsert=True)

        history_path: str | None = None
        if str(session.get("history_mode") or "append") == "append":
            new_history = list(history) if isinstance(history, list) else []
            if isinstance(result.history_delta, list):
                new_history.extend(result.history_delta)
            elif result.history_delta is not None:
                new_history.append(result.history_delta)
            history_path = v2_history_path(
                settings,
                tenant_id,
                conversation_hash,
                session_id,
                int(job.get("expected_history_version") or 0) + 1,
                job_id,
            )
            await self.archive.put_json(history_path, new_history, upsert=True)

        compact = {
            "job_id": job_id,
            "status": "succeeded",
            "response_id": result.response_id,
            "finish_reason": result.finish_reason,
            "result_type": result.result_type,
            "result_object_path": normalized_path,
        }
        committed = await self.repo.commit_job_result_v2(
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            worker_id=self.worker_id,
            lease_token=lease_token,
            expected_history_version=int(job.get("expected_history_version") or 0),
            history_object_path=history_path,
            raw_response_object_path=raw_path,
            response_output_object_path=normalized_path,
            compact_result=compact,
            wire_request_hash=result.wire_request_hash,
        )
        if committed.get("outcome") != "committed":
            logger.warning("v2_commit_not_authoritative job_id=%s outcome=%s", job_id, committed.get("outcome"))
        else:
            logger.info("v2_job_succeeded job_id=%s", job_id)

    async def _handle_timeout(self, job: dict[str, Any], lease_token: str) -> None:
        latest = await self.repo.get_job(str(job["id"]), tenant_id=job["tenant_id"], conversation_hash=job["conversation_hash"])
        dispatched = bool(latest and latest.get("execution_phase") in {"dispatch_started", "response_archived", "validating"})
        await self._record_native_failure(
            latest or job,
            lease_token,
            code="JOB_DEADLINE_EXCEEDED",
            message=f"Job exceeded {settings.worker_max_runtime_seconds} seconds",
            phase="delivery_unknown" if dispatched else "deadline_exceeded",
            delivery_status="unknown" if dispatched else "not_sent",
        )

    async def _record_native_failure(
        self,
        job: dict[str, Any],
        lease_token: str,
        *,
        code: str | None,
        message: str | None,
        phase: str,
        delivery_status: str = "not_sent",
    ) -> None:
        await self.repo.record_job_failure_v2(
            job_id=str(job["id"]),
            session_id=str(job.get("relay_session_id")) if job.get("relay_session_id") else None,
            tenant_id=str(job["tenant_id"]),
            conversation_hash=str(job["conversation_hash"]),
            worker_id=self.worker_id,
            lease_token=lease_token,
            error_id=None,
            error_code=code,
            error_message=message,
            execution_phase=phase,
            delivery_status=delivery_status,
        )

    @staticmethod
    def _native_error_code(body: bytes) -> str | None:
        try:
            obj = json.loads(body.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        error = obj.get("error")
        if isinstance(error, dict):
            value = error.get("code") or error.get("type")
            return str(value) if value is not None else None
        value = obj.get("code") or obj.get("type")
        return str(value) if value is not None else None

    @staticmethod
    def _native_error_message(body: bytes) -> str | None:
        try:
            obj = json.loads(body.decode("utf-8"))
        except Exception:
            return body.decode("utf-8", errors="replace") or None
        if not isinstance(obj, dict):
            return str(obj)
        error = obj.get("error")
        if isinstance(error, dict) and error.get("message") is not None:
            return str(error.get("message"))
        for key in ("message", "detail"):
            if obj.get(key) is not None:
                return str(obj.get(key))
        return None


async def main() -> None:
    worker = RelayWorker()
    try:
        await worker.run_forever()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
