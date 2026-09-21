from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx

from ...config import Settings
from ...providers.base import ProviderHTTPError, ProviderRequestError
from ...providers.http_wire import decode_entity, read_raw_response
from ...utils import utcnow
from .base import MaterialFile, ProviderFileResult


class GeminiAIHubMixFileAdapter:
    """AIHubMix Gemini Native Proxy -> Gemini Files API.

    Input bytes are uploaded directly to the provider.  No Relay fallback object
    is created by this adapter; fallback policy is owned by MaterialIngress.
    """

    provider = "gemini"
    adapter_version = "gemini-aihubmix-files/1"

    def __init__(self, settings: Settings) -> None:
        if not settings.aihubmix_gemini_base_url:
            raise RuntimeError("AIHUBMIX_GEMINI_BASE_URL is required for Gemini native files")
        if settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required for Gemini native files")
        self.settings = settings
        self.connection_id = settings.aihubmix_gemini_connection_id
        self.base_url = settings.aihubmix_gemini_base_url.rstrip("/")
        self.account_scope_hash = settings.connection_account_scope_hash(self.connection_id)

    async def prepare(self, material: MaterialFile, *, generation: int) -> ProviderFileResult:
        key = self.settings.aihubmix_api_key.get_secret_value()
        start_url = f"{self.base_url}/upload/v1beta/files"
        headers = {
            "x-goog-api-key": key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(material.size_bytes),
            "X-Goog-Upload-Header-Content-Type": material.content_type,
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True, follow_redirects=False) as client:
            async with client.stream(
                "POST",
                start_url,
                headers=headers,
                json={"file": {"display_name": material.filename}},
            ) as response:
                start_raw = await read_raw_response(response)
                start_status = response.status_code
                start_headers = dict(response.headers)
            if not 200 <= start_status < 300:
                raise ProviderHTTPError(
                    start_status,
                    start_raw,
                    content_type=start_headers.get("content-type"),
                    content_encoding=start_headers.get("content-encoding"),
                    request_id=self._request_id(start_headers),
                    response_headers=start_headers,
                    phase="gemini_files_start",
                )
            upload_url = start_headers.get("x-goog-upload-url") or start_headers.get("X-Goog-Upload-Url")
            if not upload_url:
                raise ProviderHTTPError(
                    start_status,
                    start_raw,
                    "Gemini resumable upload start returned no x-goog-upload-url",
                    content_type=start_headers.get("content-type"),
                    request_id=self._request_id(start_headers),
                    response_headers=start_headers,
                    phase="gemini_files_start",
                )

            upload_headers = {
                "Content-Length": str(material.size_bytes),
                "Content-Type": material.content_type,
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "Accept-Encoding": "identity",
            }
            async with client.stream(
                "POST", upload_url, headers=upload_headers, content=material.data
            ) as response:
                final_raw = await read_raw_response(response)
                final_status = response.status_code
                final_headers = dict(response.headers)
            if not 200 <= final_status < 300:
                raise ProviderHTTPError(
                    final_status,
                    final_raw,
                    content_type=final_headers.get("content-type"),
                    content_encoding=final_headers.get("content-encoding"),
                    request_id=self._request_id(final_headers),
                    response_headers=final_headers,
                    phase="gemini_files_finalize",
                )
            file_resource = self._parse_file(final_raw, final_headers, final_status)
            file_resource, probe_raw, probe_headers = await self._wait_until_active(
                client, file_resource, key
            )

        file_name = str(file_resource.get("name") or "")
        file_uri = str(file_resource.get("uri") or "")
        if not file_name or not file_uri:
            raise ProviderRequestError(
                "PROVIDER_FILE_BINDING",
                "Gemini Files API returned no file.name or file.uri",
            )
        expires_at = utcnow() + timedelta(seconds=self.settings.gemini_file_soft_ttl_seconds)
        binding = {
            "provider": self.provider,
            "connection_id": self.connection_id,
            "account_scope_hash": self.account_scope_hash,
            "purpose": "file",
            "representation": "gemini_file_uri",
            "adapter_version": self.adapter_version,
            "external_file_id": file_name,
            "external_uri": file_uri,
            "provider_file_id": file_name,
            "file_uri": file_uri,
            "state": "active",
            "processing_state": str(file_resource.get("state") or "ACTIVE").lower(),
            "generation": generation,
            "expires_at": expires_at.isoformat(),
            "last_verified_at": utcnow().isoformat(),
            "metadata": {
                "mime_type": file_resource.get("mimeType") or file_resource.get("mime_type") or material.content_type,
                "display_name": file_resource.get("displayName") or file_resource.get("display_name") or material.filename,
                "size_bytes": material.size_bytes,
            },
        }
        raw = probe_raw if probe_raw is not None else final_raw
        response_headers = probe_headers if probe_headers is not None else final_headers
        return ProviderFileResult(
            binding=binding,
            raw_response=raw,
            raw_response_content_type=response_headers.get("content-type") if response_headers else "application/json",
            request_id=self._request_id(response_headers or {}),
            phase="gemini_files_active",
        )

    async def probe(self, binding: dict[str, Any]) -> dict[str, Any]:
        file_name = str(binding.get("external_file_id") or binding.get("provider_file_id") or "")
        if not file_name:
            return {"state": "missing"}
        key = self.settings.aihubmix_api_key.get_secret_value()
        url = f"{self.base_url}/v1beta/{file_name.lstrip('/')}"
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            async with client.stream("GET", url, headers={"x-goog-api-key": key, "Accept-Encoding": "identity"}) as response:
                raw = await read_raw_response(response)
                headers = dict(response.headers)
                status = response.status_code
        if not 200 <= status < 300:
            raise ProviderHTTPError(
                status,
                raw,
                content_type=headers.get("content-type"),
                content_encoding=headers.get("content-encoding"),
                request_id=self._request_id(headers),
                response_headers=headers,
                phase="gemini_files_probe",
            )
        resource = self._parse_file(raw, headers, status)
        return {"state": str(resource.get("state") or "").lower(), "resource": resource}

    async def delete(self, binding: dict[str, Any]) -> None:
        file_name = str(binding.get("external_file_id") or binding.get("provider_file_id") or "")
        if not file_name:
            return
        key = self.settings.aihubmix_api_key.get_secret_value()
        url = f"{self.base_url}/v1beta/{file_name.lstrip('/')}"
        timeout = httpx.Timeout(self.settings.material_ingress_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            response = await client.delete(url, headers={"x-goog-api-key": key, "Accept-Encoding": "identity"})
            if response.status_code not in {200, 204, 404}:
                raw = await response.aread()
                raise ProviderHTTPError(
                    response.status_code,
                    raw,
                    content_type=response.headers.get("content-type"),
                    request_id=self._request_id(response.headers),
                    response_headers=dict(response.headers),
                    phase="gemini_files_delete",
                )

    async def _wait_until_active(
        self,
        client: httpx.AsyncClient,
        resource: dict[str, Any],
        key: str,
    ) -> tuple[dict[str, Any], bytes | None, dict[str, str] | None]:
        state = str(resource.get("state") or "ACTIVE").upper()
        if state == "ACTIVE":
            return resource, None, None
        if state == "FAILED":
            raise ProviderRequestError("PROVIDER_FILE_PROCESSING_FAILED", "Gemini file processing failed")
        file_name = str(resource.get("name") or "")
        if not file_name:
            raise ProviderRequestError("PROVIDER_FILE_BINDING", "Gemini file processing response has no file.name")
        deadline = asyncio.get_running_loop().time() + self.settings.gemini_file_processing_timeout_seconds
        last_raw: bytes | None = None
        last_headers: dict[str, str] | None = None
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self.settings.gemini_file_poll_seconds)
            url = f"{self.base_url}/v1beta/{file_name.lstrip('/')}"
            async with client.stream("GET", url, headers={"x-goog-api-key": key, "Accept-Encoding": "identity"}) as response:
                raw = await read_raw_response(response)
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
                    phase="gemini_files_probe",
                )
            resource = self._parse_file(raw, headers, status)
            last_raw, last_headers = raw, headers
            state = str(resource.get("state") or "").upper()
            if state == "ACTIVE":
                return resource, last_raw, last_headers
            if state == "FAILED":
                raise ProviderHTTPError(
                    status,
                    raw,
                    "Gemini file processing entered FAILED state",
                    content_type=headers.get("content-type"),
                    request_id=self._request_id(headers),
                    response_headers=headers,
                    phase="gemini_files_probe",
                )
        raise ProviderRequestError("PROVIDER_FILE_PROCESSING_TIMEOUT", "Gemini file did not become ACTIVE before timeout")

    @staticmethod
    def _parse_file(raw: bytes, headers: dict[str, str], status: int) -> dict[str, Any]:
        try:
            decoded = decode_entity(raw, headers.get("content-encoding"))
            payload = json.loads(decoded.decode("utf-8"))
        except Exception as exc:
            raise ProviderHTTPError(
                status,
                raw,
                "Gemini Files API returned invalid JSON",
                content_type=headers.get("content-type"),
                content_encoding=headers.get("content-encoding"),
                response_headers=headers,
                phase="gemini_files_parse",
            ) from exc
        resource = payload.get("file") if isinstance(payload, dict) and isinstance(payload.get("file"), dict) else payload
        if not isinstance(resource, dict):
            raise ProviderHTTPError(
                status,
                raw,
                "Gemini Files API returned an invalid file resource",
                content_type=headers.get("content-type"),
                response_headers=headers,
                phase="gemini_files_parse",
            )
        return resource

    @staticmethod
    def _request_id(headers: Any) -> str | None:
        for key in ("x-request-id", "request-id", "x-goog-request-id", "x-cloud-trace-context"):
            value = headers.get(key)
            if value:
                return str(value)
        return None
