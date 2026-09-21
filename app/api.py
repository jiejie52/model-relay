import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
import base64
import hashlib
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status

from .config import get_settings
from .models import CancelResponse, JobStatusResponse, JobSubmitRequest, JobSubmitResponse
from .v2_repository import RelayV2Repository
from .security import require_owner_headers, require_relay_auth
from .storage_paths import job_object_path, session_material_prefix_path
from .supabase import SupabaseBackend, SupabaseError
from .utils import json_bytes, stable_prompt_cache_key, truncate_utf8, utcnow
from .relay_gateway import router as dify_relay_gateway_router
from .api_v2.router import router as relay_v2_router
from .persistence.object_storage import StorageRegistry
from .persistence.supabase_storage import SupabaseObjectStorage
from .materials.ingress import MaterialIngress
from .materials.fallback_storage import FallbackObjectStorage
from .materials.binding_resolver import BindingResolver
from .materials.provider_files import ProviderFileRegistry, GeminiAIHubMixFileAdapter, KimiOfficialFileAdapter
from .materials.resolver import MaterialResolver
from .providers.registry import ProviderRegistry
from .providers.responses_v2 import ResponsesV2Adapter
from .providers.moonshot_chat import MoonshotChatAdapter
from .providers.gemini_native import GeminiNativeAdapter
from .routing import RouteCatalog, RouteResolver
from .core.execution_runtime import SharedExecutionRuntime
from .core.raw_error import RawErrorRecorder
from .execution.inline_executor import InlineExecutor
from .observability import configure_logging, elapsed_ms, error as log_error, info as log_info, now_ms, status_failure_class


settings = get_settings()
configure_logging(settings)
logger = logging.getLogger("model-relay-api")

backend: SupabaseBackend | None = None
repo: RelayV2Repository | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global backend, repo
    backend = SupabaseBackend(settings)
    repo = RelayV2Repository(backend, settings)

    storage_registry = StorageRegistry(SupabaseObjectStorage(backend, settings))
    material_resolver = MaterialResolver(repo, storage_registry, settings)
    fallback_storage = FallbackObjectStorage(repo, storage_registry, settings)
    file_adapters = ProviderFileRegistry()
    if settings.connection_is_active(settings.aihubmix_gemini_connection_id):
        file_adapters.register(
            settings.aihubmix_gemini_connection_id,
            GeminiAIHubMixFileAdapter(settings),
        )
    if settings.connection_is_active(settings.moonshot_connection_id):
        file_adapters.register(
            settings.moonshot_connection_id,
            KimiOfficialFileAdapter(settings, repo, storage_registry),
        )
    material_ingress = MaterialIngress(repo, fallback_storage, file_adapters, settings)
    binding_resolver = BindingResolver(repo, fallback_storage, file_adapters)
    providers = ProviderRegistry(settings)
    providers.validate_enabled_connections()
    if settings.connection_is_active("aihubmix_default"):
        providers.register_v2(
            "aihubmix_default",
            ResponsesV2Adapter(providers.openai_compatible, material_resolver),
            provider="grok",
        )
    if settings.connection_is_active(settings.aihubmix_gemini_connection_id):
        providers.register_v2(
            settings.aihubmix_gemini_connection_id,
            GeminiNativeAdapter(settings),
            provider="gemini",
        )
    if settings.connection_is_active(settings.moonshot_connection_id):
        providers.register_v2(
            settings.moonshot_connection_id,
            MoonshotChatAdapter(settings, material_resolver, repo),
            provider="kimi",
        )
    route_catalog = RouteCatalog.from_settings(settings)
    route_resolver = RouteResolver(
        settings=settings,
        catalog=route_catalog,
        providers=providers,
        provider_files=file_adapters,
    )
    route_resolver.validate_catalog()
    runtime = SharedExecutionRuntime(
        repo, storage_registry, providers, material_resolver, binding_resolver, settings
    )
    raw_errors = RawErrorRecorder(repo, storage_registry, settings)
    inline_executor = InlineExecutor(runtime, raw_errors, repo, settings)

    app.state.settings = settings
    app.state.v2_repo = repo
    app.state.storage_registry = storage_registry
    app.state.material_ingress = material_ingress
    app.state.material_resolver = material_resolver
    app.state.material_fallback_storage = fallback_storage
    app.state.provider_file_registry = file_adapters
    app.state.binding_resolver = binding_resolver
    app.state.provider_registry = providers
    app.state.route_catalog = route_catalog
    app.state.route_resolver = route_resolver
    app.state.shared_runtime = runtime
    app.state.raw_error_recorder = raw_errors
    app.state.inline_executor = inline_executor
    log_info(
        logger,
        "api_started",
        version="0.5.4-supabase-signed-url-normalization",
        deployment_id=settings.deployment_id,
        execution_pool=settings.execution_pool,
        connection_policy=settings.connection_availability_mode,
        configured_connections=providers.registered_connections(),
        configured_file_connections=file_adapters.registered_connections(),
        route_revision=route_catalog.revision,
        route_catalog_hash=route_catalog.catalog_hash,
        route_providers=route_catalog.providers(),
        dependency_http_log_level=settings.dependency_http_log_level,
        uvicorn_access_log=settings.uvicorn_access_log,
    )
    try:
        yield
    finally:
        log_info(logger, "api_stopping", deployment_id=settings.deployment_id)
        await backend.close()


app = FastAPI(title="Model Relay API", version="0.5.4-supabase-signed-url-normalization", lifespan=lifespan)
app.include_router(dify_relay_gateway_router)
app.include_router(relay_v2_router)


@app.middleware("http")
async def relay_http_logging(request: Request, call_next):
    start_ms = now_ms()
    try:
        response = await call_next(request)
    except Exception as exc:
        log_error(
            logger,
            "api_request_unhandled",
            exc_info=True,
            method=request.method,
            path=request.url.path,
            duration_ms=elapsed_ms(start_ms),
            failure_class="relay",
            exception_type=type(exc).__name__,
        )
        raise

    # Do not turn health checks or status/result polling into the dominant log
    # volume. Mutation requests and all HTTP failures remain visible.
    if request.method not in {"GET", "HEAD"} or response.status_code >= 400:
        log_info(
            logger,
            "api_request_complete",
            method=request.method,
            path=request.url.path,
            http_status=response.status_code,
            duration_ms=elapsed_ms(start_ms),
            failure_class=status_failure_class(response.status_code),
        )
    return response


def _repo() -> RelayV2Repository:
    assert repo is not None
    return repo


def _backend() -> SupabaseBackend:
    assert backend is not None
    return backend


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_session_expiring(session: dict[str, Any]) -> bool:
    signed = _parse_dt(session.get("signed_url_expires_at"))
    if signed is None:
        return False
    return signed <= utcnow() + timedelta(seconds=settings.session_expiry_safety_seconds)


def _submit_response(job: dict[str, Any]) -> JobSubmitResponse:
    return JobSubmitResponse(
        job_id=UUID(job["id"]),
        relay_session_id=UUID(job["relay_session_id"]) if job.get("relay_session_id") else None,
        status=job["status"],
        poll_after_seconds=5,
        submitted_at=_parse_dt(job.get("created_at")),
        expires_at=_parse_dt(job.get("expires_at")),
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "relay-api",
        "version": "0.5.4-supabase-signed-url-normalization",
        "deployment_id": settings.deployment_id,
        "execution_pool": settings.execution_pool,
        "route_revision": getattr(app.state, "route_catalog", None).revision if getattr(app.state, "route_catalog", None) else settings.route_revision,
        "providers": getattr(app.state, "route_catalog", None).providers() if getattr(app.state, "route_catalog", None) else [],
    }


@app.post(
    "/v1/jobs",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_relay_auth)],
)
async def submit_job(
    request: JobSubmitRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=500),
) -> JobSubmitResponse:
    repository = _repo()
    store = _backend()

    existing = await repository.find_job_by_idempotency(
        request.tenant_id, idempotency_key
    )
    if existing:
        return _submit_response(existing)

    session_id: UUID | None = None
    session: dict[str, Any] | None = None
    material_prefix_path: str | None = None
    expected_history_version = 0

    if request.mode == "continue_session":
        session = await repository.get_session(
            request.relay_session_id,
            tenant_id=request.tenant_id,
            conversation_hash=request.conversation_hash,
        )
        if not session:
            raise HTTPException(status_code=404, detail="Relay session not found")
        if session.get("provider") != request.provider:
            raise HTTPException(
                status_code=409,
                detail="Relay session provider cannot change across continuation",
            )
        if _is_session_expiring(session):
            raise HTTPException(
                status_code=409,
                detail="RELAY_SESSION_MATERIAL_EXPIRING",
            )
        session_id = UUID(session["id"])
        material_prefix_path = session.get("material_prefix_object_path")
        expected_history_version = int(session.get("history_version") or 0)

    elif request.mode == "new_session":
        session_id = uuid4()
        material_prefix_path = session_material_prefix_path(
            settings,
            request.tenant_id,
            request.conversation_hash,
            str(session_id),
        )
        await store.storage_put(
            material_prefix_path,
            json_bytes(request.material_prefix),
            content_type="application/json",
        )

        signed_expiry = request.material_expires_at or (
            utcnow() + timedelta(seconds=settings.supabase_signed_url_ttl)
        )
        session_expiry = min(
            signed_expiry,
            utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        )
        session = await repository.create_session(
            {
                "id": str(session_id),
                "tenant_id": request.tenant_id,
                "conversation_hash": request.conversation_hash,
                "provider": request.provider,
                "model": request.model,
                "prompt_cache_key": stable_prompt_cache_key(request.conversation_hash),
                "material_prefix_object_path": material_prefix_path,
                "history_object_path": None,
                "signed_url_expires_at": signed_expiry.isoformat(),
                "history_version": 0,
                "expires_at": session_expiry.isoformat(),
            }
        )

    job_id = uuid4()
    snapshot = request.model_dump(mode="json")
    snapshot["relay_session_id"] = str(session_id) if session_id else None
    snapshot["expected_history_version"] = expected_history_version
    snapshot["material_prefix_object_path"] = material_prefix_path

    # Avoid duplicating the immutable material prefix for session-backed jobs.
    if request.mode in {"new_session", "continue_session"}:
        snapshot["material_prefix"] = None

    request_path = job_object_path(
        settings,
        request.tenant_id,
        request.conversation_hash,
        str(job_id),
        "request.json",
    )
    await store.storage_put(request_path, json_bytes(snapshot))

    row = {
        "id": str(job_id),
        "tenant_id": request.tenant_id,
        "conversation_hash": request.conversation_hash,
        "relay_session_id": str(session_id) if session_id else None,
        "stage": request.stage,
        "provider": request.provider,
        "status": "queued",
        "model": request.model,
        "think_level": request.think_level,
        "request_object_path": request_path,
        "compact_result": None,
        "idempotency_key": idempotency_key,
        "attempt_count": 0,
        "expires_at": repository.default_job_expiry().isoformat(),
    }

    try:
        created = await repository.create_job(row)
    except SupabaseError as exc:
        # A concurrent retry may have won the idempotency race.
        if exc.status_code == 409:
            existing = await repository.find_job_by_idempotency(
                request.tenant_id, idempotency_key
            )
            if existing:
                return _submit_response(existing)
        raise HTTPException(status_code=502, detail="Failed to persist Relay job") from exc

    return _submit_response(created)


@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
    dependencies=[Depends(require_relay_auth)],
)
async def get_job_status(
    job_id: UUID,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> JobStatusResponse:
    tenant_id, conversation_hash = owner
    job = await _repo().get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # v2 removes Relay-side provider error classification. Legacy status keeps
    # this field only for shape compatibility and never derives retry policy.
    retryable = False
    return JobStatusResponse(
        job_id=job_id,
        relay_session_id=UUID(job["relay_session_id"]) if job.get("relay_session_id") else None,
        status=job["status"],
        stage=job["stage"],
        provider=job["provider"],
        model=job["model"],
        heartbeat_at=_parse_dt(job.get("heartbeat_at")),
        started_at=_parse_dt(job.get("started_at")),
        completed_at=_parse_dt(job.get("completed_at")),
        poll_after_seconds=10,
        retryable=retryable,
        error_code=job.get("error_code"),
        error_message=job.get("error_message"),
    )


@app.get(
    "/v1/jobs/{job_id}/result",
    dependencies=[Depends(require_relay_auth)],
)
async def get_job_result(
    job_id: UUID,
    view: str = Query(default="dify"),
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> Response:
    if view != "dify":
        raise HTTPException(status_code=400, detail="Only view=dify is available")

    tenant_id, conversation_hash = owner
    job = await _repo().get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] == "succeeded":
        payload = job.get("compact_result") or {
            "job_id": str(job_id),
            "status": "failed",
            "error_code": "COMPACT_RESULT_MISSING",
        }
        raw = json_bytes(payload)
        if len(raw) > settings.relay_result_hard_limit_bytes:
            # Last defensive barrier: Dify must never receive an oversized result.
            safe = dict(payload)
            safe["text"] = truncate_utf8(
                str(safe.get("text") or ""), settings.relay_result_preview_bytes
            )
            safe["text_truncated"] = True
            raw = json_bytes(safe)
        return Response(content=raw, media_type="application/json", status_code=200)

    if job["status"] in {"failed", "cancelled", "expired", "indeterminate"}:
        raw_path = job.get("raw_response_object_path")
        raw_error = None
        if raw_path:
            try:
                raw = await _backend().storage_get(raw_path)
                raw_error = {
                    "body_base64": base64.b64encode(raw).decode("ascii"),
                    "body_size": len(raw),
                    "body_sha256": hashlib.sha256(raw).hexdigest(),
                }
                try:
                    raw_error["body_text"] = raw.decode("utf-8")
                    raw_error["body_encoding"] = "utf-8"
                except UnicodeDecodeError:
                    raw_error["body_encoding"] = "binary"
            except Exception as exc:
                raw_error = {
                    "archive_read_error": {
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    }
                }
        payload = {
            "job_id": str(job_id),
            "status": job["status"],
            "error": raw_error,
        }
        # Relay-originated failures that never had an HTTP body retain their
        # exact diagnostic fields; they are not mapped to UPSTREAM_* classes.
        if raw_error is None and (job.get("error_code") or job.get("error_message")):
            payload["error"] = {
                "source": "relay",
                "code": job.get("error_code"),
                "message": job.get("error_message"),
            }
        return Response(content=json_bytes(payload), media_type="application/json", status_code=200)

    payload = {
        "job_id": str(job_id),
        "status": job["status"],
        "stage": job["stage"],
        "poll_after_seconds": 10,
    }
    return Response(content=json_bytes(payload), media_type="application/json", status_code=202)


@app.post(
    "/v1/jobs/{job_id}/cancel",
    response_model=CancelResponse,
    dependencies=[Depends(require_relay_auth)],
)
async def cancel_job(
    job_id: UUID,
    owner: tuple[str, str] = Depends(require_owner_headers),
) -> CancelResponse:
    tenant_id, conversation_hash = owner
    repository = _repo()
    job = await repository.get_job(
        job_id, tenant_id=tenant_id, conversation_hash=conversation_hash
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] not in {"succeeded", "failed", "cancelled", "expired"}:
        updated = await repository.update_job(
            job_id,
            {
                "status": "cancelled",
                "completed_at": utcnow().isoformat(),
                "error_code": "CANCELLED_BY_CLIENT",
                "error_message": "Job cancelled by client",
            },
        )
        if updated:
            job = updated

    return CancelResponse(job_id=job_id, status=job["status"])
