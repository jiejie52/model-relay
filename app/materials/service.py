from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import mimetypes
import socket
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx

from ..config import Settings
from ..errors.service import RawErrorService
from ..repository import RelayRepository
from ..storage.uploaded_files import UploadedFileStore
from ..utils import safe_segment, utcnow
from ..v2_utils import request_fingerprint


class MaterialIngressError(RuntimeError):
    def __init__(self, code: str, message: str, *, error_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.error_id = error_id


@dataclass
class PreparedFile:
    fileobj: BinaryIO
    size: int
    sha256: str
    detected_mime: str | None


def _detect_mime(filename: str, declared: str | None, head: bytes) -> str | None:
    # Minimal non-executing detection: common signatures plus filename fallback.
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or declared


class MaterialService:
    def __init__(
        self,
        repository: RelayRepository,
        store: UploadedFileStore,
        errors: RawErrorService,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.store = store
        self.errors = errors
        self.settings = settings

    def make_ids(self) -> tuple[str, str]:
        return f"mat_{uuid4().hex}", f"ing_{uuid4().hex}"

    async def reserve(
        self,
        *,
        tenant_id: str,
        conversation_hash: str,
        idempotency_key: str,
        filename: str,
        declared_mime: str | None,
        expected_sha256: str | None,
        expected_size: int | None,
        expires_at: Any,
        source_identity: dict[str, Any],
    ) -> dict[str, Any]:
        material_id, ingestion_id = self.make_ids()
        effective_expiry = expires_at or (
            utcnow() + timedelta(seconds=self.settings.material_ready_ttl_seconds)
        )
        fingerprint = request_fingerprint(
            {
                "filename": filename,
                "declared_mime": declared_mime,
                "expected_sha256": expected_sha256,
                "expected_size": expected_size,
                "expires_at": str(expires_at) if expires_at is not None else "default-policy",
                "source": source_identity,
            }
        )
        result = await self.repository.reserve_material_v2(
            material_id=material_id,
            ingestion_id=ingestion_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            filename=filename,
            declared_mime=declared_mime,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            expires_at=effective_expiry.isoformat(),
            source_identity=source_identity,
        )
        if result.get("outcome") == "conflict":
            raise MaterialIngressError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used with a different material request",
            )
        return result

    async def ingest_fileobj(
        self,
        *,
        material: dict[str, Any],
        ingestion_id: str,
        fileobj: BinaryIO,
        tenant_id: str,
        conversation_hash: str,
        ingestion_lease_token: str | None = None,
    ) -> dict[str, Any]:
        material_id = str(material["id"])
        filename = str(material["filename"])
        generation = int(material.get("generation") or 1)
        prepared = await asyncio.to_thread(
            self._measure_file,
            fileobj,
            filename,
            material.get("declared_mime"),
        )
        if prepared.size > self.settings.material_max_bytes:
            raise MaterialIngressError(
                "MATERIAL_TOO_LARGE",
                f"Material exceeds MATERIAL_MAX_BYTES ({self.settings.material_max_bytes})",
            )
        expected_size = material.get("expected_size")
        if expected_size is not None and int(expected_size) != prepared.size:
            raise MaterialIngressError(
                "MATERIAL_EXPECTED_SIZE_MISMATCH",
                f"Captured material size {prepared.size} does not match expected_size {expected_size}",
            )
        expected_sha256 = str(material.get("expected_sha256") or "").strip().lower()
        if expected_sha256 and expected_sha256 != prepared.sha256.lower():
            raise MaterialIngressError(
                "MATERIAL_EXPECTED_SHA256_MISMATCH",
                "Captured material SHA-256 does not match expected_sha256",
            )

        staging_key = self._staging_key(tenant_id, conversation_hash, material_id, ingestion_id)
        canonical_key = self._canonical_key(
            tenant_id,
            conversation_hash,
            material_id,
            generation,
            prepared.sha256,
            filename,
        )
        await self.repository.backend.update(
            "relay_materials",
            {"phase": "verifying", "updated_at": utcnow().isoformat()},
            filters={"id": f"eq.{material_id}"},
        )
        await self.repository.backend.update(
            "relay_material_ingestions",
            {"phase": "verifying", "staging_key": staging_key, "canonical_candidate_key": canonical_key, "updated_at": utcnow().isoformat()},
            filters={"id": f"eq.{ingestion_id}"},
        )

        prepared.fileobj.seek(0)
        staged = await self.store.put_staging(
            staging_key,
            prepared.fileobj,
            content_type=prepared.detected_mime or "application/octet-stream",
        )
        if staged.size != prepared.size:
            raise MaterialIngressError(
                "MATERIAL_STORAGE_SIZE_MISMATCH",
                f"Staging object size {staged.size} differs from captured size {prepared.size}",
            )
        final_info = await self.store.finalize_immutable(staging_key, canonical_key)
        if final_info.size != prepared.size:
            raise MaterialIngressError(
                "MATERIAL_STORAGE_SIZE_MISMATCH",
                "Canonical object size differs from captured material",
            )
        # Read back the immutable canonical bytes before publishing ready. This
        # closes the gap where a successful copy is assumed without verifying the
        # actual object content.
        canonical_bytes = await self.store.open_reader(canonical_key)
        canonical_sha = hashlib.sha256(canonical_bytes).hexdigest()
        if len(canonical_bytes) != prepared.size or canonical_sha != prepared.sha256:
            raise MaterialIngressError(
                "MATERIAL_STORAGE_INTEGRITY_MISMATCH",
                "Canonical material failed byte/hash verification",
            )

        result = await self.repository.publish_material_ready_v2(
            material_id=material_id,
            ingestion_id=ingestion_id,
            tenant_id=tenant_id,
            conversation_hash=conversation_hash,
            generation=generation,
            object_key=canonical_key,
            sha256=prepared.sha256,
            byte_length=prepared.size,
            detected_mime=prepared.detected_mime,
            lease_token=ingestion_lease_token,
        )
        outcome = result.get("outcome")
        if outcome == "lease_lost":
            raise MaterialIngressError(
                "MATERIAL_INGESTION_LEASE_LOST",
                "Material ingestion lost its fencing lease before ready publication",
            )
        if outcome not in {"ready", "reused"}:
            raise MaterialIngressError(
                "MATERIAL_PUBLISH_FAILED",
                f"Material publish failed: {outcome}",
            )
        return result.get("material") or material

    def _measure_file(self, fileobj: BinaryIO, filename: str, declared_mime: str | None) -> PreparedFile:
        fileobj.seek(0)
        digest = hashlib.sha256()
        size = 0
        head = b""
        while True:
            chunk = fileobj.read(1024 * 1024)
            if not chunk:
                break
            if not head:
                head = chunk[:8192]
            size += len(chunk)
            if size > self.settings.material_max_bytes:
                break
            digest.update(chunk)
        fileobj.seek(0)
        return PreparedFile(
            fileobj=fileobj,
            size=size,
            sha256=digest.hexdigest(),
            detected_mime=_detect_mime(filename, declared_mime, head),
        )

    async def fetch_url_to_spool(
        self,
        *,
        url: str,
        filename: str,
        tenant_id: str,
        conversation_hash: str,
        provider_label: str = "material_ingress",
    ) -> BinaryIO:
        current = url
        timeout = httpx.Timeout(
            connect=self.settings.material_fetch_connect_timeout_seconds,
            read=self.settings.material_fetch_timeout_seconds,
            write=self.settings.material_fetch_timeout_seconds,
            pool=self.settings.material_fetch_connect_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=False) as client:
            for redirect_count in range(self.settings.material_url_max_redirects + 1):
                await self._validate_fetch_url(current)
                try:
                    response = await client.get(current, headers={"Accept": "*/*", "Accept-Encoding": "identity"})
                    raw = await response.aread()
                except httpx.HTTPError as exc:
                    from ..providers.base import ProviderTransportError

                    err = ProviderTransportError(
                        str(exc),
                        provider=None,
                        service=provider_label,
                        exception_type=type(exc).__name__,
                    )
                    captured = await self.errors.capture_transport(
                        tenant_id=tenant_id,
                        conversation_hash=conversation_hash,
                        exc=err,
                    )
                    raise MaterialIngressError(
                        "MATERIAL_FETCH_TRANSPORT_ERROR",
                        str(exc),
                        error_id=captured.get("id"),
                    ) from exc

                if 300 <= response.status_code < 400 and response.headers.get("location"):
                    if redirect_count >= self.settings.material_url_max_redirects:
                        raise MaterialIngressError("MATERIAL_FETCH_REDIRECT_LIMIT", "Too many redirects")
                    current = urljoin(current, response.headers["location"])
                    continue

                if not 200 <= response.status_code < 300:
                    from ..providers.base import ProviderHTTPError

                    exc = ProviderHTTPError(
                        response.status_code,
                        raw,
                        "Material source returned an HTTP error",
                        headers=list(response.headers.multi_items()),
                        content_type=response.headers.get("content-type"),
                        content_encoding=response.headers.get("content-encoding"),
                        received_complete=True,
                        service=provider_label,
                    )
                    captured = await self.errors.capture_provider_http(
                        tenant_id=tenant_id,
                        conversation_hash=conversation_hash,
                        exc=exc,
                    )
                    raise MaterialIngressError(
                        "MATERIAL_FETCH_HTTP_ERROR",
                        f"Source returned HTTP {response.status_code}",
                        error_id=captured.get("id"),
                    )

                if len(raw) > self.settings.material_max_bytes:
                    raise MaterialIngressError("MATERIAL_TOO_LARGE", "Fetched material exceeds configured limit")
                spool = tempfile.SpooledTemporaryFile(max_size=min(self.settings.material_max_bytes, 8 * 1024 * 1024))
                spool.write(raw)
                spool.seek(0)
                return spool
        raise MaterialIngressError("MATERIAL_FETCH_FAILED", "Material fetch did not produce a response")

    async def _validate_fetch_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise MaterialIngressError("MATERIAL_SOURCE_URL_REJECTED", "Only HTTPS material URLs are allowed")
        port = parsed.port or 443
        if port not in self.settings.allowed_material_url_ports:
            raise MaterialIngressError("MATERIAL_SOURCE_URL_REJECTED", f"URL port {port} is not allowed")

        def resolve() -> list[str]:
            infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
            return sorted({info[4][0] for info in infos})

        try:
            addresses = await asyncio.to_thread(resolve)
        except OSError as exc:
            raise MaterialIngressError("MATERIAL_SOURCE_DNS_FAILED", str(exc)) from exc
        if not addresses:
            raise MaterialIngressError("MATERIAL_SOURCE_DNS_FAILED", "Source hostname resolved to no addresses")
        for raw in addresses:
            ip = ipaddress.ip_address(raw)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise MaterialIngressError(
                    "MATERIAL_SOURCE_URL_REJECTED",
                    f"Source resolved to a non-public address: {ip}",
                )

    def _staging_key(self, tenant: str, conversation: str, material_id: str, ingestion_id: str) -> str:
        return "/".join(
            [
                self.settings.material_s3_prefix.strip("/"),
                "staging",
                safe_segment(tenant),
                safe_segment(conversation),
                safe_segment(material_id),
                safe_segment(ingestion_id),
            ]
        )

    def _canonical_key(
        self,
        tenant: str,
        conversation: str,
        material_id: str,
        generation: int,
        sha256: str,
        filename: str,
    ) -> str:
        return "/".join(
            [
                self.settings.material_s3_prefix.strip("/"),
                "canonical",
                safe_segment(tenant),
                safe_segment(conversation),
                safe_segment(material_id),
                f"g{generation}",
                sha256,
                safe_segment(PurePosixPath(filename).name),
            ]
        )
