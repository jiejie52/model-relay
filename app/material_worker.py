from __future__ import annotations

import asyncio
import logging
import os
import socket
from uuid import uuid4

from .config import get_settings
from .errors.service import RawErrorService
from .materials.service import MaterialIngressError, MaterialService
from .repository import RelayRepository
from .storage.execution_archive import ExecutionArchiveStore
from .storage.uploaded_files import UploadedFileStore
from .supabase import SupabaseBackend
from .utils import utcnow

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("model-relay-material-worker")


class MaterialIngressWorker:
    def __init__(self) -> None:
        self.backend = SupabaseBackend(settings)
        self.repo = RelayRepository(self.backend, settings)
        self.archive = ExecutionArchiveStore(self.backend)
        self.errors = RawErrorService(self.repo, self.archive, settings)
        self.store = UploadedFileStore(settings)
        self.service = MaterialService(self.repo, self.store, self.errors, settings)
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"

    async def close(self) -> None:
        await self.backend.close()

    async def run_forever(self) -> None:
        logger.info("material_worker_started id=%s", self.worker_id)
        while True:
            try:
                result = await self.backend.rpc(
                    "claim_relay_material_ingestion_v2",
                    {"p_worker_id": self.worker_id, "p_lease_seconds": max(300, settings.worker_max_runtime_seconds)},
                )
                ingestion = result[0] if isinstance(result, list) and result else result if isinstance(result, dict) else None
                if not ingestion:
                    await asyncio.sleep(settings.worker_poll_seconds)
                    continue
                await self.process(ingestion)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("material_worker_loop_error")
                await asyncio.sleep(settings.worker_poll_seconds)

    async def process(self, ingestion: dict) -> None:
        material_id = str(ingestion["material_id"])
        material = await self.repo.get_material(material_id)
        if not material or material.get("status") != "processing":
            return
        source = ingestion.get("source_identity") or {}
        if source.get("kind") != "url":
            return
        try:
            source_expiry = source.get("expires_at")
            if source_expiry:
                from datetime import datetime
                expiry = datetime.fromisoformat(str(source_expiry).replace("Z", "+00:00"))
                if expiry <= utcnow():
                    raise MaterialIngressError("MATERIAL_SOURCE_EXPIRED", "Material source URL expired before capture")
            spool = await self.service.fetch_url_to_spool(
                url=str(source.get("url") or ""),
                filename=str(material.get("filename") or "material"),
                tenant_id=str(material["tenant_id"]),
                conversation_hash=str(material["conversation_hash"]),
            )
            try:
                await self.service.ingest_fileobj(
                    material=material,
                    ingestion_id=str(ingestion["id"]),
                    fileobj=spool,
                    tenant_id=str(material["tenant_id"]),
                    conversation_hash=str(material["conversation_hash"]),
                    ingestion_lease_token=str(ingestion.get("lease_token") or "") or None,
                )
            finally:
                spool.close()
            logger.info("material_ready material_id=%s", material_id)
        except MaterialIngressError as exc:
            await self.repo.fail_material_ingestion_v2(
                material_id=material_id,
                ingestion_id=str(ingestion["id"]),
                tenant_id=str(material["tenant_id"]),
                conversation_hash=str(material["conversation_hash"]),
                error_id=exc.error_id,
                phase=str(ingestion.get("phase") or "fetching"),
                lease_token=str(ingestion.get("lease_token") or "") or None,
            )
            logger.error("material_failed material_id=%s code=%s", material_id, exc.code)
        except Exception as exc:
            await self.repo.fail_material_ingestion_v2(
                material_id=material_id,
                ingestion_id=str(ingestion["id"]),
                tenant_id=str(material["tenant_id"]),
                conversation_hash=str(material["conversation_hash"]),
                error_id=None,
                phase=str(ingestion.get("phase") or "fetching"),
                lease_token=str(ingestion.get("lease_token") or "") or None,
            )
            logger.exception("material_failed_unexpected material_id=%s", material_id)


async def main() -> None:
    worker = MaterialIngressWorker()
    try:
        await worker.run_forever()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
