from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx

from ...config import Settings
from ...persistence.object_storage import StorageRegistry
from ...providers.base import ProviderHTTPError, ProviderRequestError
from ...providers.http_wire import decode_entity, read_raw_response
from ...storage_paths import relay_object_path
from ...utils import utcnow
from ...v2_repository import RelayV2Repository
from .base import MaterialFile, ProviderFileResult


class KimiOfficialFileAdapter:
    provider = "kimi"
    adapter_version = "kimi-official-files/1"

    def __init__(
        self,
        settings: Settings,
        repo: RelayV2Repository,
        storage: StorageRegistry,
    ) -> None:
        if settings.moonshot_api_key is None:
            raise RuntimeError("MOONSHOT_API_KEY is required for Kimi files")
        self.settings = settings
        self.repo = repo
        self.storage = storage
        self.connection_id = settings.moonshot_connection_id
        self.account_scope_hash = settings.connection_account_scope_hash(self.connection_id)

    async def prepare(self, material: MaterialFile, *, generation: int) -> ProviderFileResult:
        purpose, representation = self._purpose(material.content_type)
        upload_raw, upload_headers, upload_status, payload = await self._upload(material, purpose)
        file_id = str(payload.get("id") or payload.get("file_id") or "")
        if not file_id:
            raise ProviderRequestError("PROVIDER_FILE_BINDING", "Kimi Files API returned no file id")

        if purpose == "file-extract":
            payload = await self._wait_file_ready(file_id, payload)
        metadata: dict[str, Any] = {
            "filename": payload.get("filename") or material.filename,
            "mime_type": payload.get("mime_type") or material.content_type,
            "bytes": payload.get("bytes") or payload.get("size") or material.size_bytes,
            "provider_status": payload.get("status") or payload.get("extract_status"),
        }
        external_uri = None
        derived_object_id = None
        if purpose == "file-extract":
            extracted, content_type, content_raw, content_headers, content_status = await self._read_content(file_id)
            derived_object_id = await self._store_extraction(material, extracted, content_type)
            metadata["extraction_object_id"] = derived_object_id
            metadata["extraction_content_type"] = content_type
            # The content response is the most useful provider-derived artifact
            # for text materials; keep its request-id when available.
            request_id = self._request_id(content_headers) or self._request_id(upload_headers)
            raw_response = content_raw
            raw_content_type = content_headers.get("content-type")
        else:
            external_uri = f"ms://{file_id}"
            request_id = self._request_id(upload_headers)
            raw_response = upload_raw
            raw_content_type = upload_headers.get("content-type")

        binding = {
            "provider": self.provider,
            "connection_id": self.connection_id,
            "account_scope_hash": self.account_scope_hash,
            "purpose": purpose,
            "representation": representation,
            "adapter_version": self.adapter_version,
            "external_file_id": file_id,
            "external_uri": external_uri,
            "provider_file_id": file_id,
            "file_uri": external_uri,
            "state": "active",
            "processing_state": "ready",
            "generation": generation,
            "expires_at": None,
            "last_verified_at": utcnow().isoformat(),
            "metadata": metadata,
        }
        return ProviderFileResult(
            binding=binding,
            raw_response=raw_response,
            raw_response_content_type=raw_content_type,
            request_id=request_id,
            phase=f"kimi_files_{purpose}_ready",
            derived_object_id=derived_object_id,
            http_status=content_status if purpose == "file-extract" else upload_status,
        )

    async def probe(self, binding: dict[str, Any]) -> dict[str, Any]:
        file_id = str(binding.get("external_file_id") or binding.get("provider_file_id") or "")
        if not file_id:
            return {"state": "missing"}
        url = f"{self.settings.moonshot_root}/files/{file_id}"
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            async with client.stream("GET", url, headers=self._headers(json_content=False)) as response:
                raw = await read_raw_response(response, log_context={"provider": self.provider, "connection_id": self.connection_id, "phase": "kimi_files_probe"})
                status = response.status_code
                headers = dict(response.headers)
        if status in {404, 410}:
            return {"state": "expired"}
        if not 200 <= status < 300:
            raise ProviderHTTPError(
                status,
                raw,
                content_type=headers.get("content-type"),
                content_encoding=headers.get("content-encoding"),
                request_id=self._request_id(headers),
                response_headers=headers,
                phase="kimi_files_probe",
            )
        return {"state": "active"}

    async def delete(self, binding: dict[str, Any]) -> None:
        file_id = str(binding.get("external_file_id") or binding.get("provider_file_id") or "")
        if not file_id:
            return
        url = f"{self.settings.moonshot_root}/files/{file_id}"
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            response = await client.delete(url, headers=self._headers(json_content=False))
            if response.status_code not in {200, 204, 404}:
                raw = await response.aread()
                raise ProviderHTTPError(
                    response.status_code,
                    raw,
                    content_type=response.headers.get("content-type"),
                    request_id=self._request_id(response.headers),
                    response_headers=dict(response.headers),
                    phase="kimi_files_delete",
                )

    async def _upload(
        self, material: MaterialFile, purpose: str
    ) -> tuple[bytes, dict[str, str], int, dict[str, Any]]:
        url = f"{self.settings.moonshot_root}/files"
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            async with client.stream(
                "POST",
                url,
                headers=self._headers(json_content=False),
                data={"purpose": purpose},
                files={"file": (material.filename, material.data, material.content_type)},
            ) as response:
                raw = await read_raw_response(response, log_context={"material_id": material.material_id, "provider": self.provider, "connection_id": self.connection_id, "phase": "kimi_files_upload", "purpose": purpose})
                status = response.status_code
                headers = dict(response.headers)
        if not 200 <= status < 300:
            raise ProviderHTTPError(
                status,
                raw,
                content_type=headers.get("content-type"),
                content_encoding=headers.get("content-encoding"),
                request_id=self._request_id(headers),
                response_headers=headers,
                phase="kimi_files_upload",
            )
        try:
            decoded = decode_entity(raw, headers.get("content-encoding"))
            payload = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise ProviderHTTPError(
                status,
                raw,
                "Kimi Files API returned invalid JSON",
                content_type=headers.get("content-type"),
                content_encoding=headers.get("content-encoding"),
                request_id=self._request_id(headers),
                response_headers=headers,
                phase="kimi_files_parse",
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("PROVIDER_FILE_BINDING", "Kimi Files API returned an invalid response")
        return raw, headers, status, payload


    async def _wait_file_ready(self, file_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        ready_states = {"ready", "processed", "succeeded", "success", "completed"}
        failed_states = {"failed", "error", "cancelled"}
        state = str(payload.get("extract_status") or payload.get("status") or "").lower()
        if not state or state in ready_states:
            return payload
        if state in failed_states:
            raise ProviderRequestError("PROVIDER_FILE_PROCESSING_FAILED", f"Kimi file processing failed: {state}")
        deadline = asyncio.get_running_loop().time() + self.settings.kimi_file_processing_timeout_seconds
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(self.settings.kimi_file_poll_seconds)
                url = f"{self.settings.moonshot_root}/files/{file_id}"
                async with client.stream("GET", url, headers=self._headers(json_content=False)) as response:
                    raw = await read_raw_response(response, log_context={"provider": self.provider, "connection_id": self.connection_id, "phase": "kimi_files_probe", "provider_file_id": file_id})
                    status = response.status_code
                    headers = dict(response.headers)
                if not 200 <= status < 300:
                    raise ProviderHTTPError(
                        status,
                        raw,
                        content_type=headers.get("content-type"),
                        content_encoding=headers.get("content-encoding"),
                        request_id=self._request_id(headers),
                        response_headers=headers,
                        phase="kimi_files_probe",
                    )
                try:
                    decoded = decode_entity(raw, headers.get("content-encoding"))
                    current = json.loads(decoded.decode("utf-8"))
                except Exception as exc:
                    raise ProviderHTTPError(
                        status,
                        raw,
                        "Kimi file probe returned invalid JSON",
                        content_type=headers.get("content-type"),
                        content_encoding=headers.get("content-encoding"),
                        request_id=self._request_id(headers),
                        response_headers=headers,
                        phase="kimi_files_probe_parse",
                    ) from exc
                if not isinstance(current, dict):
                    continue
                state = str(current.get("extract_status") or current.get("status") or "").lower()
                if not state or state in ready_states:
                    return current
                if state in failed_states:
                    raise ProviderHTTPError(
                        status,
                        raw,
                        f"Kimi file processing entered {state}",
                        content_type=headers.get("content-type"),
                        request_id=self._request_id(headers),
                        response_headers=headers,
                        phase="kimi_files_probe",
                    )
        raise ProviderRequestError("PROVIDER_FILE_PROCESSING_TIMEOUT", "Kimi file did not become ready before timeout")

    async def _read_content(
        self, file_id: str
    ) -> tuple[bytes, str, bytes, dict[str, str], int]:
        url = f"{self.settings.moonshot_root}/files/{file_id}/content"
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            async with client.stream("GET", url, headers=self._headers(json_content=False)) as response:
                raw = await read_raw_response(response, log_context={"provider": self.provider, "connection_id": self.connection_id, "phase": "kimi_files_content", "provider_file_id": file_id})
                status = response.status_code
                headers = dict(response.headers)
        if not 200 <= status < 300:
            raise ProviderHTTPError(
                status,
                raw,
                content_type=headers.get("content-type"),
                content_encoding=headers.get("content-encoding"),
                request_id=self._request_id(headers),
                response_headers=headers,
                phase="kimi_files_content",
            )
        decoded = decode_entity(raw, headers.get("content-encoding"))
        content_type = str(headers.get("content-type") or "text/plain; charset=utf-8")
        # Some deployments return JSON with a content/text field; otherwise the
        # official endpoint is treated as text/plain bytes.
        extracted = decoded
        if "json" in content_type.lower():
            try:
                obj = json.loads(decoded.decode("utf-8"))
                if isinstance(obj, dict):
                    for key in ("content", "text", "data"):
                        if isinstance(obj.get(key), str):
                            extracted = obj[key].encode("utf-8")
                            content_type = "text/plain; charset=utf-8"
                            break
            except Exception:
                pass
        return extracted, content_type, raw, headers, status

    async def _store_extraction(self, material: MaterialFile, data: bytes, content_type: str) -> str:
        import hashlib

        object_id = f"obj_{uuid4().hex}"
        path = relay_object_path(
            self.settings,
            material.tenant_id,
            material.conversation_hash,
            object_id,
            f"{material.filename}.kimi-extracted.txt",
        )
        backend = self.storage.get(self.settings.default_storage_id)
        location = await backend.put_bytes(path, data, content_type=content_type.split(";", 1)[0])
        # Provider-derived extraction is a Relay artifact, not an input-file
        # fallback copy. It deliberately remains in RelayArtifactStorage.
        await self.repo.create_object(
            {
                "id": object_id,
                "tenant_id": material.tenant_id,
                "conversation_hash": material.conversation_hash,
                "storage_id": location.storage_id,
                "bucket": location.bucket,
                "object_key": location.key,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "content_type": content_type,
                "created_at": utcnow().isoformat(),
            }
        )
        return object_id

    def _headers(self, *, json_content: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.moonshot_api_key.get_secret_value()}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "identity",
        }
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _purpose(mime: str) -> tuple[str, str]:
        value = (mime or "application/octet-stream").lower()
        if value.startswith("image/") and value != "image/svg+xml":
            return "image", "kimi_image_ms_uri"
        if value.startswith("video/"):
            return "video", "kimi_video_ms_uri"
        return "file-extract", "kimi_extracted_text"

    @staticmethod
    def _request_id(headers: Any) -> str | None:
        for key in ("x-request-id", "request-id", "x-moonshot-request-id"):
            value = headers.get(key)
            if value:
                return str(value)
        return None
