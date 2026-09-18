from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api_v2 import router as relay_v2_router
from .config import get_settings
from .errors.service import RawErrorService
from .providers.registry import ProviderRegistry
from .repository import RelayRepository
from .storage.execution_archive import ExecutionArchiveStore
from .storage.uploaded_files import UploadedFileStore, UploadedFileStoreError
from .supabase import SupabaseBackend

settings = get_settings()
logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    backend = SupabaseBackend(settings)
    repo = RelayRepository(backend, settings)
    archive = ExecutionArchiveStore(backend)
    providers = ProviderRegistry(settings)
    errors = RawErrorService(repo, archive, settings)
    material_store = None
    material_store_error = None
    if settings.material_store_configured:
        try:
            material_store = UploadedFileStore(settings)
        except UploadedFileStoreError as exc:
            material_store_error = str(exc)
            logging.getLogger("model-relay-v2-api").error("Material store unavailable: %s", exc)
    app.state.settings = settings
    app.state.backend = backend
    app.state.repo = repo
    app.state.archive = archive
    app.state.providers = providers
    app.state.errors = errors
    app.state.material_store = material_store
    app.state.material_store_error = material_store_error
    try:
        yield
    finally:
        await backend.close()


app = FastAPI(
    title="Model Relay V2 Core API",
    version="2.0.5-hotfix5",
    lifespan=lifespan,
)
app.include_router(relay_v2_router)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "relay-v2-core",
        "version": "2.0.5-hotfix5",
        "material_storage": {
            "configured": bool(settings.material_store_configured),
            "ready": getattr(app.state, "material_store", None) is not None,
            "missing_fields": settings.material_store_missing_fields,
            "error": getattr(app.state, "material_store_error", None),
        },
    }
