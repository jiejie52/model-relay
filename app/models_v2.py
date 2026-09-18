from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


HistoryMode = Literal["append", "none"]
MaterialStatus = Literal["processing", "ready", "failed", "deleting", "deleted"]


class MaterialURLSource(BaseModel):
    kind: Literal["url"] = "url"
    url: str = Field(min_length=1, max_length=4096)
    expires_at: datetime | None = None


class MaterialCreateJSON(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    declared_mime: str | None = Field(default=None, max_length=300)
    source: MaterialURLSource
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    expected_size: int | None = Field(default=None, ge=0)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionDefaults(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class V2SessionCreateRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    upstream_profile: str = Field(min_length=1, max_length=160)
    history_mode: HistoryMode = "append"
    defaults: SessionDefaults
    context: list[dict[str, Any]] = Field(default_factory=list)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class V2JobSubmitRequest(BaseModel):
    expected_history_version: int = Field(ge=0)
    input: list[dict[str, Any]] = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    generation: dict[str, Any] = Field(default_factory=dict)
    structured_output: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelayEnvelope(BaseModel):
    schema_version: Literal["relay-envelope/2.0"] = "relay-envelope/2.0"
    request_id: str
    data: Any = None
    error: Any = None


class CapabilityQueryResult(BaseModel):
    profile: str
    provider: str
    protocol: str
    account_scope: str
    allowed_models: list[str]
    capability_profile_version: str
    enabled: bool = True
    capabilities: dict[str, Any] = Field(default_factory=dict)


def extract_material_ids(value: Any) -> list[str]:
    """Extract material_ref IDs in caller order without interpreting business fields."""

    result: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if str(node.get("type") or "") == "material_ref":
                material_id = str(node.get("material_id") or "").strip()
                if material_id:
                    result.append(material_id)
            for key, child in node.items():
                if key not in {"material_id"}:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    # A repeated reference is meaningful in context ordering, but the Session
    # material set itself is a unique ordered set.
    seen: set[str] = set()
    unique: list[str] = []
    for material_id in result:
        if material_id not in seen:
            seen.add(material_id)
            unique.append(material_id)
    return unique
