from __future__ import annotations

from typing import Any

from ..core.idempotency import stable_hash
from .contracts import CacheDecisionError


def prepare_implicit_prefix_binding(
    *,
    plan: dict[str, Any],
    context_plan: dict[str, Any],
    session: dict[str, Any],
    provider: str | None = None,
    snapshot: dict[str, Any] | None = None,
    provider_physical_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = plan.get("mechanism_config") if isinstance(plan.get("mechanism_config"), dict) else {}
    projection = plan.get("primary_projection") if isinstance(plan.get("primary_projection"), dict) else {"kind": "full_stable_prefix"}
    projection_kind = str(projection.get("kind") or "full_stable_prefix")
    binding_plan = plan.get("binding_plan") if isinstance(plan.get("binding_plan"), dict) else {}
    binding_id = str(binding_plan.get("binding_id") or "primary")
    projection_hash = str(projection.get("projection_hash") or stable_hash(projection))

    if str(provider or "").strip().lower() == "gemini":
        physical = provider_physical_plan if isinstance(provider_physical_plan, dict) else {}
        if projection_kind == "full_stable_prefix":
            selected_physical = physical
        else:
            projections = physical.get("cache_binding_projections") if isinstance(physical.get("cache_binding_projections"), dict) else {}
            selected_physical = projections.get(binding_id) if isinstance(projections.get(binding_id), dict) else {}
        layout_version = str(selected_physical.get("layout_version") or physical.get("layout_version") or "")
        if (
            layout_version not in {"gemini-physical-cache-layout/9", "gemini-physical-cache-layout/10"}
            or not bool(selected_physical.get("implicit_prefix_eligible"))
            or not str(selected_physical.get("implicit_prefix_fingerprint") or "")
            or not isinstance(selected_physical.get("implicit_prefix_payload"), dict)
        ):
            raise CacheDecisionError(
                "GEMINI_IMPLICIT_PREFIX_LAYOUT_UNSUPPORTED",
                "Gemini implicit-prefix execution requires a validated physical projection for this binding",
                reason="physical_binding_projection_unavailable",
            )
        dependencies = selected_physical.get("dependencies") if isinstance(selected_physical.get("dependencies"), dict) else {}
        return {
            "schema_version": "relay-gemini-implicit-prefix-binding/2",
            "binding_id": binding_id,
            "projection_kind": projection_kind,
            "projection_hash": projection_hash,
            "prefix_fingerprint": str(selected_physical.get("implicit_prefix_fingerprint")),
            "prefix_version": int(dependencies.get("safe_history_prefix_version") or 0),
            "compatible_prefix_fingerprints": dict(
                selected_physical.get("implicit_prefix_compatible_fingerprints") or {}
            ),
            "layout_version": layout_version,
            "projector_version": selected_physical.get("projector_version"),
            "physical_plan_hash": selected_physical.get("physical_plan_hash"),
            "implicit_prefix_payload_hash": selected_physical.get("implicit_prefix_payload_hash"),
            "wire_projection_hash": selected_physical.get("implicit_prefix_payload_hash"),
            # Provider-managed implicit caching has no explicit resource handle.
            "prompt_cache_key": None,
            "retention": cfg.get("retention"),
        }

    if projection_kind == "full_stable_prefix":
        prefix_fingerprint = context_plan.get("stable_prefix_fingerprint")
    else:
        physical = provider_physical_plan if isinstance(provider_physical_plan, dict) else {}
        projections = physical.get("cache_binding_projections") if isinstance(physical.get("cache_binding_projections"), dict) else {}
        selected_physical = projections.get(binding_id) if isinstance(projections.get(binding_id), dict) else {}
        prefix_fingerprint = selected_physical.get("implicit_prefix_fingerprint")
        if not prefix_fingerprint:
            raise CacheDecisionError(
                "CACHE_PREFIX_PROJECTION_UNSUPPORTED",
                "Provider adapter did not freeze the requested implicit-prefix projection",
                reason="physical_binding_projection_unavailable",
            )

    key_source = str(cfg.get("key_source") or "derived")
    hint: str | None = None
    if key_source == "legacy_session_prompt_cache_key":
        hint = str(session.get("prompt_cache_key") or "") or None
    elif bool(cfg.get("send_prompt_cache_key", False)):
        # A deterministic, non-content-bearing key bound to this exact projection.
        hint = "relay-cache-" + stable_hash(
            {
                "session_id": str(session.get("id") or ""),
                "projection_hash": projection_hash,
                "prefix_fingerprint": prefix_fingerprint,
            }
        )[:40]
    return {
        "binding_id": binding_id,
        "projection_kind": projection_kind,
        "projection_hash": projection_hash,
        "prefix_fingerprint": prefix_fingerprint,
        "wire_projection_hash": prefix_fingerprint,
        "prompt_cache_key": hint,
        "retention": cfg.get("retention"),
    }
