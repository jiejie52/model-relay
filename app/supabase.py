import json
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings


class SupabaseError(RuntimeError):
    """Lossless Supabase HTTP failure.

    `raw_body` is never truncated. `body` is a convenience UTF-8 replacement
    view retained for legacy call sites; raw_body is authoritative.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        raw_body: bytes = b"",
        *,
        content_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.raw_body = raw_body
        self.body = raw_body.decode("utf-8", errors="replace")
        self.content_type = content_type


class SupabaseBackend:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = settings.supabase_secret_key.get_secret_value()

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
            raw = await response.aread()
            raise SupabaseError(
                response.status_code,
                f"Supabase request failed: {method} {url}",
                raw,
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

    async def delete(self, table: str, *, filters: dict[str, str]) -> None:
        await self._request(
            "DELETE",
            f"{self.settings.supabase_root}/rest/v1/{table}",
            params=filters,
            headers={"Prefer": "return=minimal"},
        )

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


    async def storage_head(self, object_path: str) -> dict[str, Any]:
        encoded_path = quote(object_path.lstrip("/"), safe="/")
        bucket = quote(self.settings.supabase_bucket, safe="")
        response = await self._request(
            "HEAD",
            f"{self.settings.supabase_root}/storage/v1/object/{bucket}/{encoded_path}",
        )
        return {
            "content_type": response.headers.get("content-type"),
            "content_length": response.headers.get("content-length"),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
        }

    async def storage_sign_read_url(self, object_path: str, *, expires_in: int) -> str:
        encoded_path = quote(object_path.lstrip("/"), safe="/")
        bucket = quote(self.settings.supabase_bucket, safe="")
        response = await self._request(
            "POST",
            f"{self.settings.supabase_root}/storage/v1/object/sign/{bucket}/{encoded_path}",
            headers={"Content-Type": "application/json"},
            json_body={"expiresIn": int(expires_in)},
        )
        data = response.json()
        signed = data.get("signedURL") or data.get("signedUrl") or data.get("signed_url")
        if not signed:
            raise RuntimeError("Supabase did not return a signed URL")
        if str(signed).startswith("http"):
            return str(signed)
        return f"{self.settings.supabase_root}{signed}"

    async def storage_delete(self, object_path: str) -> None:
        bucket = quote(self.settings.supabase_bucket, safe="")
        await self._request(
            "DELETE",
            f"{self.settings.supabase_root}/storage/v1/object/{bucket}",
            headers={"Content-Type": "application/json"},
            json_body={"prefixes": [object_path.lstrip("/")]},
        )
