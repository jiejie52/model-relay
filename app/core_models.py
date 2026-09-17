from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


HistoryMode = Literal["append", "none"]


class SessionCreateRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    conversation_hash: str = Field(min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=80)
    upstream_profile: str = Field(default="default", min_length=1, max_length=120)
    history_mode: HistoryMode = "append"
    defaults: dict[str, Any] = Field(default_factory=dict)
    context: Any = None
    context_identity: str | None = Field(default=None, max_length=500)
    context_expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionJobRequest(BaseModel):
    # `input` is incremental input for this execution. Relay does not accept a
    # client-provided authoritative history; session history is server-managed.
    input: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=120)
    generation: dict[str, Any] = Field(default_factory=dict)
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    structured_output: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_input(self) -> "SessionJobRequest":
        if not self.input:
            raise ValueError("input must contain at least one incremental message")
        return self


class SessionView(BaseModel):
    id: UUID
    provider: str
    upstream_profile: str
    protocol: str
    history_codec: str
    history_mode: HistoryMode
    history_version: int
    model: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


class JobView(BaseModel):
    id: UUID
    status: str
    model: str | None = None
    label: str | None = None
    heartbeat_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    poll_after_seconds: int = 5


class RawErrorInline(BaseModel):
    encoding: Literal["utf-8", "base64"]
    data: str


class RawErrorView(BaseModel):
    origin: Literal["provider", "transport", "dependency", "relay"]
    provider: str | None = None
    service: str | None = None
    http_status: int | None = None
    response_headers: list[list[str]] = Field(default_factory=list)
    body: RawErrorInline | None = None
    body_ref: str | None = None
    byte_length: int | None = None
    sha256: str | None = None
    received_complete: bool | None = None
    request_id: str | None = None
    exception: dict[str, Any] | None = None
    relay_code: str | None = None
    relay_message: str | None = None


class RelayEnvelope(BaseModel):
    schema_version: str = "relay-envelope/2.0"
    request_id: str
    session: SessionView | None = None
    job: JobView | None = None
    result: dict[str, Any] | None = None
    error: RawErrorView | None = None
