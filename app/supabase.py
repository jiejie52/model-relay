import json
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings


class SupabaseError(RuntimeError):
    """Supabase HTTP error with the original response preserved losslessly."""

    def __init__(
        self,
        status_code: int,
        message: str,
        body: bytes = b"",
        *,
        headers: list[tuple[str, str]] | None = None,
        content_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.headers = headers or []
        self.content_type = content_type

    @property
    def body_text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class SupabaseBackend:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = settings.supabase_secret_key.get_secret_value()

        # Supabase's current sb_secret_* keys are opaque backend keys and should
        # be sent in the apikey header. Legacy service_role JWTs additionally use
        # Authorization: Bearer for backwards compatibility.
        self.headers = {
            "apikey": key,
            "Accept": "application/json",
        }
        if not key.startswith("sb_secret_"):
            self.headers["Authorization"] = f"Bearer {key}"

        self.client = httpx.AsyncClient(
            timeout=settings.supabase_timeout_seconds,
            verify=True,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        expected: set[int] | None = None,
    ) -> httpx.Response:
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        response = await self.client.request(
            method,
            url,
            params=params,
            headers=merged,
            json=json_body,
            content=content,
        )
        ok = expected or set(range(200, 300))
        if response.status_code not in ok:
            # Do not truncate dependency errors. Callers decide whether to persist
            # or inline the bytes, but this transport layer must preserve them.
            body = response.content
            raise SupabaseError(
                response.status_code,
                f"Supabase request failed: {method} {url}",
                body,
                headers=[
                    (name.decode("latin-1"), value.decode("latin-1"))
                    for name, value in response.headers.raw
                ],
                content_type=response.headers.get("content-type"),
            )
        return response

    async def select(
        self,
        table: str,
        *,
        filters: dict[str, str] | None = None,
        select: str = "*",
        limit: int | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if order:
            params["order"] = order
        response = await self._request(
            "GET",
            f"{self.settings.supabase_root}/rest/v1/{table}",
            params=params,
        )
        return response.json()

    async def insert(self, table: str, row: dict[str, Any]) -> list[dict[str, Any]]:
        response = await self._request(
            "POST",
            f"{self.settings.supabase_root}/rest/v1/{table}",
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json_body=row,
        )
        return response.json()

    async def upsert(
        self,
        table: str,
        row: dict[str, Any],
        *,
        on_conflict: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {"on_conflict": on_conflict} if on_conflict else None
        response = await self._request(
            "POST",
            f"{self.settings.supabase_root}/rest/v1/{table}",
            params=params,
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            json_body=row,
        )
        return response.json()

    async def update(
        self,
        table: str,
        values: dict[str, Any],
        *,
        filters: dict[str, str],
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "PATCH",
            f"{self.settings.supabase_root}/rest/v1/{table}",
            params=filters,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json_body=values,
        )
        return response.json()

    async def rpc(self, function: str, payload: dict[str, Any]) -> Any:
        response = await self._request(
            "POST",
            f"{self.settings.supabase_root}/rest/v1/rpc/{function}",
            headers={"Content-Type": "application/json"},
            json_body=payload,
        )
        if not response.content:
            return None
        return response.json()

    async def storage_put(
        self,
        object_path: str,
        data: bytes,
        *,
        content_type: str = "application/json",
        upsert: bool = True,
    ) -> str:
        encoded_path = quote(object_path.lstrip("/"), safe="/")
        bucket = quote(self.settings.supabase_bucket, safe="")
        headers = {
            "Content-Type": content_type,
            "x-upsert": "true" if upsert else "false",
        }
        await self._request(
            "POST",
            f"{self.settings.supabase_root}/storage/v1/object/{bucket}/{encoded_path}",
            headers=headers,
            content=data,
        )
        return object_path

    async def storage_get(self, object_path: str) -> bytes:
        encoded_path = quote(object_path.lstrip("/"), safe="/")
        bucket = quote(self.settings.supabase_bucket, safe="")
        response = await self._request(
            "GET",
            f"{self.settings.supabase_root}/storage/v1/object/{bucket}/{encoded_path}",
        )
        return response.content

    async def storage_get_json(self, object_path: str) -> Any:
        raw = await self.storage_get(object_path)
        return json.loads(raw.decode("utf-8"))
