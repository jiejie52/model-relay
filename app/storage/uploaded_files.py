from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import BinaryIO

from ..config import Settings

# Keep the API importable even when an alternative build path accidentally
# omits the S3 client dependency. Correct Docker builds still verify boto3 at
# build time; this guard turns a missing optional runtime dependency into a
# clear material-storage error instead of crashing uvicorn during module import.
_BOTO_IMPORT_ERROR: ModuleNotFoundError | None = None
try:
    import boto3
    from botocore.client import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in deployment preflight
    _BOTO_IMPORT_ERROR = exc
    boto3 = None  # type: ignore[assignment]
    Config = None  # type: ignore[assignment]

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        pass


class UploadedFileStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str | None
    content_type: str | None


class UploadedFileStore:
    """Railway Storage Bucket adapter.

    This store is deliberately separate from Supabase execution archives. Keys
    passed here are canonical uploaded-file keys only.
    """

    def __init__(self, settings: Settings) -> None:
        if _BOTO_IMPORT_ERROR is not None or boto3 is None or Config is None:
            raise UploadedFileStoreError(
                "Railway material storage client is unavailable because boto3/botocore "
                "is not installed. Rebuild the service from this package's root so "
                "requirements.txt or Dockerfile is applied."
            ) from _BOTO_IMPORT_ERROR
        if not settings.material_store_configured:
            missing = ", ".join(settings.material_store_missing_fields) or "unknown"
            raise UploadedFileStoreError(
                "Railway material storage is not configured; missing logical fields: "
                f"{missing}. Configure MATERIAL_S3_* variables or Railway/AWS bucket "
                "references (ENDPOINT/BUCKET/ACCESS_KEY_ID/SECRET_ACCESS_KEY or "
                "AWS_ENDPOINT_URL/AWS_S3_BUCKET_NAME/AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY)."
            )
        self.settings = settings
        assert settings.material_s3_endpoint
        assert settings.material_s3_bucket
        assert settings.material_s3_access_key_id
        assert settings.material_s3_secret_access_key
        self.bucket = settings.material_s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.material_s3_endpoint,
            region_name=settings.material_s3_region,
            aws_access_key_id=settings.material_s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.material_s3_secret_access_key.get_secret_value(),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": settings.material_s3_addressing_style},
            ),
        )

    def _wrap(self, exc: Exception, action: str) -> UploadedFileStoreError:
        return UploadedFileStoreError(f"Railway material store {action} failed: {exc}")

    async def put_staging(
        self,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str | None = None,
    ) -> ObjectInfo:
        def upload() -> ObjectInfo:
            try:
                extra = {"ContentType": content_type} if content_type else None
                if extra:
                    self.client.upload_fileobj(fileobj, self.bucket, key, ExtraArgs=extra)
                else:
                    self.client.upload_fileobj(fileobj, self.bucket, key)
                return self._head_sync(key)
            except (BotoCoreError, ClientError, OSError) as exc:
                raise self._wrap(exc, "upload") from exc

        return await asyncio.to_thread(upload)

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> ObjectInfo:
        return await self.put_staging(key, io.BytesIO(data), content_type=content_type)

    async def finalize_immutable(self, staging_key: str, canonical_key: str) -> ObjectInfo:
        """Copy staging to a unique canonical key and remove staging.

        Canonical keys contain material_id + generation + content hash and are
        never reused by the application. The uniqueness rule is the immutability
        guard on providers that do not offer object-lock/versioning.
        """

        def finalize() -> ObjectInfo:
            try:
                try:
                    self.client.head_object(Bucket=self.bucket, Key=canonical_key)
                except ClientError as exc:
                    code = str((getattr(exc, "response", {}).get("Error") or {}).get("Code") or "")
                    if code not in {"404", "NoSuchKey", "NotFound"}:
                        raise
                else:
                    raise UploadedFileStoreError(
                        f"canonical object already exists and will not be overwritten: {canonical_key}"
                    )

                self.client.copy_object(
                    Bucket=self.bucket,
                    Key=canonical_key,
                    CopySource={"Bucket": self.bucket, "Key": staging_key},
                    MetadataDirective="COPY",
                )
                info = self._head_sync(canonical_key)
                self.client.delete_object(Bucket=self.bucket, Key=staging_key)
                return info
            except UploadedFileStoreError:
                raise
            except (BotoCoreError, ClientError, OSError) as exc:
                raise self._wrap(exc, "finalize") from exc

        return await asyncio.to_thread(finalize)

    def _head_sync(self, key: str) -> ObjectInfo:
        response = self.client.head_object(Bucket=self.bucket, Key=key)
        return ObjectInfo(
            key=key,
            size=int(response.get("ContentLength") or 0),
            etag=str(response.get("ETag") or "").strip('"') or None,
            content_type=response.get("ContentType"),
        )

    async def head(self, key: str) -> ObjectInfo:
        def run() -> ObjectInfo:
            try:
                return self._head_sync(key)
            except (BotoCoreError, ClientError, OSError) as exc:
                raise self._wrap(exc, "head") from exc

        return await asyncio.to_thread(run)

    async def open_reader(self, key: str) -> bytes:
        def get() -> bytes:
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=key)
                return response["Body"].read()
            except (BotoCoreError, ClientError, OSError) as exc:
                raise self._wrap(exc, "read") from exc

        return await asyncio.to_thread(get)

    async def presign_read(self, key: str, *, expires_seconds: int | None = None) -> str:
        def sign() -> str:
            try:
                return self.client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": key},
                    ExpiresIn=int(expires_seconds or self.settings.material_s3_presign_seconds),
                )
            except (BotoCoreError, ClientError, OSError) as exc:
                raise self._wrap(exc, "presign") from exc

        return await asyncio.to_thread(sign)

    async def delete(self, key: str) -> None:
        def run() -> None:
            try:
                self.client.delete_object(Bucket=self.bucket, Key=key)
            except (BotoCoreError, ClientError, OSError) as exc:
                raise self._wrap(exc, "delete") from exc

        await asyncio.to_thread(run)
