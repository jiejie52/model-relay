from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


JobMode = Literal["new_session", "continue_session", "stateless"]
ThinkLevel = Literal["auto", "low", "medium", "high", "xhigh"]


class UpstreamConfig(BaseModel):
    base_url: str | None = None


class JobSubmitRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    conversation_hash: str = Field(min_length=1, max_length=256)

    relay_session_id: UUID | None = None

    stage: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    think_level: ThinkLevel = "auto"
    mode: JobMode = "stateless"

    current_query: str = Field(min_length=1)
    instructions: str | None = None

    # For new_session this is persisted once as the immutable material prefix.
    # For stateless jobs it is stored inside the job request snapshot.
    material_prefix: Any = None
    # Set true when Dify passes an already-frozen first-turn prefix that already
    # contains current_query (compatible with the existing Grok material prefix).
    material_prefix_includes_current_query: bool = False
    material_expires_at: datetime | None = None

    upstream: UpstreamConfig | None = None

    # Provider-specific optional fields. Protected fields such as model/input/store
    # are ignored by the adapter and cannot override Relay safety rules.
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mode(self) -> "JobSubmitRequest":
        if self.mode == "continue_session" and self.relay_session_id is None:
            raise ValueError("continue_session requires relay_session_id")
        if self.mode == "new_session" and self.relay_session_id is not None:
            raise ValueError("new_session must not provide relay_session_id")
        if self.mode == "stateless" and self.relay_session_id is not None:
            raise ValueError("stateless jobs must not provide relay_session_id")
        return self


class JobSubmitResponse(BaseModel):
    job_id: UUID
    relay_session_id: UUID | None = None
    status: str
    poll_after_seconds: int
    submitted_at: datetime | None = None
    expires_at: datetime | None = None


class JobStatusResponse(BaseModel):
    job_id: UUID
    relay_session_id: UUID | None = None
    status: str
    stage: str
    provider: str
    model: str
    heartbeat_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    poll_after_seconds: int = 10
    retryable: bool = False
    error_code: str | None = None
    error_message: str | None = None


class CancelResponse(BaseModel):
    job_id: UUID
    status: str
