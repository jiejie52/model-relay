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
from .storage.uploaded_files import UploadedFileStore
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
    material_store = UploadedFileStore(settings)
    app.state.settings = settings
    app.state.backend = backend
    app.state.repo = repo
    app.state.archive = archive
    app.state.providers = providers
    app.state.errors = errors
    app.state.material_store = material_store
    try:
        yield
    finally:
        await backend.close()


app = FastAPI(
    title="Model Relay V2 Core API",
    version="2.0.0-v2-material-session",
    lifespan=lifespan,
)
app.include_router(relay_v2_router)


@app.get("/health")
async def health():
    return {"ok": True, "service": "relay-v2-core", "version": "2.0.0-v2-material-session"}
