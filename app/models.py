from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


NormalJobMode = Literal["new_session", "continue_session", "stateless"]
JobMode = Literal["new_session", "continue_session", "stateless", "new_fusion_corpus"]
ThinkLevel = Literal["auto", "low", "medium", "high", "xhigh"]

FUSION_STAGES = {
    "fusion_corpus_ingest",
    "material_evidence_mapping",
    "global_adjudication",
    "scoped_decision",
    "final_evidence_review",
    "direct_final_synthesis",
    "synthesis_blueprint",
    "final_draft_generation",
    "quality_review",
    "evidence_grounded_repair",
}


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

    # Required by normal inference. Fusion stages carry their stage-specific
    # input in payload and therefore do not require current_query at API level.
    current_query: str | None = None
    instructions: str | None = None

    # Normal inference/session fields.
    material_prefix: Any = None
    material_prefix_includes_current_query: bool = False
    material_expires_at: datetime | None = None

    # Fusion runtime fields. Kept generic so Dify can submit the same Job API
    # for Corpus ingest, adjudication, decision, synthesis, quality and repair.
    fusion_corpus_id: str | None = Field(default=None, max_length=220)
    route_profile: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)

    upstream: UpstreamConfig | None = None

    # Provider-specific optional fields. Protected fields such as model/input/store
    # are ignored by the adapter and cannot override Relay safety rules.
    provider_payload: dict[str, Any] = Field(default_factory=dict)

    # Provider-neutral structured-output request. The Relay treats the schema as
    # opaque business data: it validates JSON-Schema syntax and maps it to the
    # provider transport, but never inspects domain property names. Current Dify
    # builders may also use payload.output_schema_mode + payload.output_schema;
    # that compatibility form is resolved by app.structured_output.
    structured_output: dict[str, Any] = Field(default_factory=dict)

    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mode(self) -> "JobSubmitRequest":
        is_fusion = self.stage in FUSION_STAGES
        if is_fusion:
            if self.relay_session_id is not None:
                raise ValueError("Fusion jobs must not provide relay_session_id")
            if self.stage == "fusion_corpus_ingest":
                if self.mode not in {"new_fusion_corpus", "stateless"}:
                    raise ValueError("fusion_corpus_ingest mode must be new_fusion_corpus or stateless")
            elif self.mode not in {"stateless", "new_fusion_corpus"}:
                raise ValueError("Fusion model stages are stateless Relay jobs")
            if not self.fusion_corpus_id:
                raise ValueError("Fusion jobs require fusion_corpus_id")
            return self

        if not str(self.current_query or "").strip():
            raise ValueError("normal jobs require current_query")
        if self.mode == "continue_session" and self.relay_session_id is None:
            raise ValueError("continue_session requires relay_session_id")
        if self.mode == "new_session" and self.relay_session_id is not None:
            raise ValueError("new_session must not provide relay_session_id")
        if self.mode == "stateless" and self.relay_session_id is not None:
            raise ValueError("stateless jobs must not provide relay_session_id")
        if self.mode == "new_fusion_corpus":
            raise ValueError("new_fusion_corpus is only valid for Fusion jobs")
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
    error_id: str | None = None


class CancelResponse(BaseModel):
    job_id: UUID
    status: str
