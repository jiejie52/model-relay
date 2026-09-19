from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


ContextPolicy = Literal["conversation", "explicit"]
ExecutionMode = Literal["sync", "async"]


class SessionCreateRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    conversation_hash: str = Field(min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=80)
    connection_id: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    context_policy: ContextPolicy = "conversation"
    material_ids: list[str] = Field(default_factory=list)
    execution_pool: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionResponse(BaseModel):
    schema_version: str = "relay-session/2.0"
    session_id: UUID
    tenant_id: str
    conversation_hash: str
    provider: str
    connection_id: str
    model: str
    context_policy: ContextPolicy
    material_ids: list[str]
    history_version: int
    active_request_id: UUID | None = None
    execution_pool: str
    created_at: datetime | None = None
    expires_at: datetime | None = None


class ExecutionSpec(BaseModel):
    mode: ExecutionMode


class SessionRequestCreate(BaseModel):
    input: Any
    instructions: str | None = None
    material_ids: list[str] = Field(default_factory=list)
    provider: str | None = Field(default=None, max_length=80)
    connection_id: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=200)
    think_level: str | None = Field(default=None, max_length=40)
    execution: ExecutionSpec
    structured_output: dict[str, Any] = Field(default_factory=dict)
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RawErrorMeta(BaseModel):
    source: str
    upstream_http_status: int | None = None
    upstream_request_id: str | None = None
    content_type: str | None = None
    content_encoding: str | None = None
    body_encoding: str | None = None
    body_size: int | None = None
    body_sha256: str | None = None
    body_object_id: str | None = None
    exception_type: str | None = None
    message: str | None = None
    provider_success_object_id: str | None = None
    provider_output_object_id: str | None = None
    archive_error: dict[str, Any] | None = None


class RequestEnvelope(BaseModel):
    schema_version: str = "relay-session/2.0"
    session_id: UUID
    request_id: UUID
    job_id: UUID | None = None
    execution: ExecutionSpec
    status: str
    history_version: int
    result: dict[str, Any] | None = None
    error: RawErrorMeta | None = None
    poll_after_seconds: int | None = None


class MaterialCreateJSON(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    conversation_hash: str = Field(min_length=1, max_length=256)
    filename: str = Field(min_length=1, max_length=500)
    content_type: str | None = Field(default=None, max_length=250)
    source_url: str | None = None
    content_base64: str | None = None
    source_ref: str | None = Field(default=None, max_length=1000)
    parent_material_id: str | None = Field(default=None, max_length=220)
    ordinal: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def one_source(self) -> "MaterialCreateJSON":
        count = int(bool(self.source_url)) + int(bool(self.content_base64))
        if count != 1:
            raise ValueError("exactly one of source_url or content_base64 is required")
        return self


class MaterialResponse(BaseModel):
    schema_version: str = "relay-material/2.0"
    material_id: str
    status: str
    filename: str
    content_type: str
    size: int
    sha256: str
    object_id: str
    storage_id: str
    source_ref: str | None = None
    parent_material_id: str | None = None
    ordinal: int | None = None
    created_at: datetime | None = None
