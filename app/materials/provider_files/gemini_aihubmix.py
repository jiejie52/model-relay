from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from ...config import Settings
from ...providers.base import ProviderRequestError
from ...providers.gemini_genai_sdk import GeminiAIHubMixGenAIClient
from ...utils import utcnow
from .base import MaterialFile, ProviderFileResult


class GeminiAIHubMixFileAdapter:
    """AIHubMix Gemini Files API through the official ``google-genai`` SDK.

    The returned SDK File object is kept process-locally by the shared SDK
    wrapper so a same-process cache create can pass that exact object into
    ``client.aio.caches.create``. The durable binding stores only name/URI;
    a Worker or restarted process rehydrates the File with ``files.get``.
    """

    provider = "gemini"
    adapter_version = "gemini-aihubmix-files/2"

    def __init__(
        self,
        settings: Settings,
        *,
        sdk: GeminiAIHubMixGenAIClient | Any | None = None,
    ) -> None:
        if settings.aihubmix_api_key is None:
            raise RuntimeError("AIHUBMIX_API_KEY is required for Gemini native files")
        self.settings = settings
        self.connection_id = settings.aihubmix_gemini_connection_id
        self.account_scope_hash = settings.connection_account_scope_hash(self.connection_id)
        self.sdk = sdk or GeminiAIHubMixGenAIClient(settings)

    async def prepare(self, material: MaterialFile, *, generation: int) -> ProviderFileResult:
        file_obj = await self.sdk.upload_file(
            data=material.data,
            filename=material.filename,
            mime_type=material.content_type,
        )
        file_obj = await self._wait_until_active(file_obj)

        file_name = str(getattr(file_obj, "name", "") or "")
        file_uri = str(getattr(file_obj, "uri", "") or "")
        if not file_name or not file_uri:
            raise ProviderRequestError(
                "PROVIDER_FILE_BINDING",
                "Gemini Files API returned no file.name or file.uri",
            )

        expires_at = utcnow() + timedelta(seconds=self.settings.gemini_file_soft_ttl_seconds)
        mime_type = str(getattr(file_obj, "mime_type", "") or material.content_type)
        display_name = str(getattr(file_obj, "display_name", "") or material.filename)
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
            "processing_state": self._state(file_obj).lower(),
            "generation": generation,
            "expires_at": expires_at.isoformat(),
            "last_verified_at": utcnow().isoformat(),
            "metadata": {
                "mime_type": mime_type,
                "display_name": display_name,
                "size_bytes": material.size_bytes,
                "provider_transport": "google_genai_sdk",
                "sdk_adapter_version": str(getattr(self.sdk, "adapter_version", "")),
            },
        }
        return ProviderFileResult(
            binding=binding,
            raw_response=self.sdk.model_json_bytes(file_obj),
            raw_response_content_type="application/json",
            request_id=self.sdk.request_id(file_obj),
            phase="gemini_files_active",
            http_status=200,
        )

    async def probe(self, binding: dict[str, Any]) -> dict[str, Any]:
        file_name = str(binding.get("external_file_id") or binding.get("provider_file_id") or "")
        if not file_name:
            return {"state": "missing"}
        file_obj = await self.sdk.get_file(name=file_name, refresh=True)
        return {"state": self._state(file_obj).lower(), "resource": self.sdk.dump_model(file_obj)}

    async def delete(self, binding: dict[str, Any]) -> None:
        file_name = str(binding.get("external_file_id") or binding.get("provider_file_id") or "")
        if file_name:
            await self.sdk.delete_file(name=file_name)

    async def _wait_until_active(self, file_obj: Any) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.gemini_file_processing_timeout_seconds
        current = file_obj
        while True:
            state = self._state(current)
            if state == "ACTIVE":
                return self.sdk.remember_file(current)
            if state == "FAILED":
                raise ProviderRequestError(
                    "PROVIDER_FILE_PROCESSING_FAILED",
                    "Gemini Files API reported FAILED while processing the uploaded material",
                )
            if state != "PROCESSING":
                # AIHubMix may omit state for immediately available uploads.
                if not state or state in {"UNSPECIFIED", "FILE_STATE_UNSPECIFIED"}:
                    return self.sdk.remember_file(current)
                raise ProviderRequestError(
                    "PROVIDER_FILE_PROCESSING_STATE",
                    f"Unexpected Gemini file processing state: {state}",
                )
            if loop.time() >= deadline:
                raise ProviderRequestError(
                    "PROVIDER_FILE_PROCESSING_TIMEOUT",
                    "Timed out waiting for Gemini Files API processing",
                )
            await asyncio.sleep(self.settings.gemini_file_poll_seconds)
            name = str(getattr(current, "name", "") or "")
            if not name:
                raise ProviderRequestError(
                    "PROVIDER_FILE_BINDING",
                    "Gemini Files API processing response lost file.name",
                )
            current = await self.sdk.get_file(name=name, refresh=True)

    @staticmethod
    def _state(file_obj: Any) -> str:
        value = getattr(file_obj, "state", None)
        if value is None:
            return ""
        name = getattr(value, "name", None)
        if name:
            return str(name).upper()
        raw = getattr(value, "value", value)
        return str(raw).rsplit(".", 1)[-1].upper()
