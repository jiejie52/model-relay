from __future__ import annotations


import logging
from typing import Any

from app.runtime.affinity import build_wire_context, drift_reason, rotate_affinity
from app.core.context_codec import freeze_history
from app.materials.classification import material_cache_class
from app.core.idempotency import stable_hash
from app.protocol_adapters.wire_projection.gemini_physical import build_gemini_physical_cache_plan, uses_cache_material_projection_layout, uses_supported_physical_layout
from app.protocol_adapters.base import ProviderRequestError
from app.protocol_adapters.wire_projection.gemini_wire import project_material_part
from app.observability import info as log_info


logger = logging.getLogger("model-relay-runtime")




class PhysicalContextProjector:
    def __init__(self, repo, bindings, settings):
        self.repo, self.bindings, self.settings = repo, bindings, settings

    async def prepare_scope(self, state):
        if int(state.upgrade.get("version") or 0) >= 3 and state.upgrade.get("grok_mode") != "legacy_v1":
            try:
                state.history = freeze_history(state.history)
            except ValueError as exc:
                raise ProviderRequestError("HISTORY_INTEGRITY_MISMATCH", str(exc)) from exc
        affinity_rows = []
        state.wire = {}
        state.rotated_affinity = False
        if state.upgrade and state.fence is not None:
            state.wire = build_wire_context(self.settings, state.snapshot, state.session, state.request_id, [])
            if state.wire["normalize"]:
                affinity_rows = await self.repo.recent_affinity_observations(state.request_row)
            if affinity_rows and state.wire.get("affinity_token"):
                reason = drift_reason(
                    affinity_rows, epoch=state.wire["affinity_epoch"],
                    min_tokens=int(state.upgrade.get("implicit_min_tokens") or 2048),
                    strategy=state.wire["affinity_eviction_strategy"],
                )
                if reason and str(state.session.get("affinity_eviction_request_id")) != state.request_id:
                    state.session = await rotate_affinity(self.repo, state.request_row, state.session, state.fence, reason)
                    state.rotated_affinity = True
                    state.wire = build_wire_context(self.settings, state.snapshot, state.session, state.request_id, [])
            if affinity_rows:
                previous = (affinity_rows[0].get("cache_usage") or {}).get("affinity_audit") or {}
                if previous.get("affinity_epoch") == state.wire["affinity_epoch"]:
                    state.wire["previous_chain"] = previous.get("physical_chain") or []
                    state.wire["previous_semantic_fingerprint"] = previous.get("semantic_fingerprint")
                    state.wire["compared_request_id"] = str(affinity_rows[0]["id"])


    async def project(self, state):
        state.context_plan = state.snapshot.get("context_plan") if isinstance(state.snapshot.get("context_plan"), dict) else None
        if (int(state.upgrade.get("version") or 0) >= 3 and state.upgrade.get("grok_mode") != "legacy_v1"
                and state.context_plan is not None):
            expected_turns = [s["canonical_content_hash"] for s in state.context_plan.get("segments") or []
                              if s.get("domain") == "history"]
            actual_turns = [stable_hash(turn.get("transport_history") or {}) for turn in state.history]
            if expected_turns != actual_turns:
                raise ProviderRequestError("HISTORY_INTEGRITY_MISMATCH", "History no longer matches the accepted ContextPlan")
        state.provider_physical_plan: dict[str, Any] | None = None
        state.cache_material_projection = None
        state.material_reuse_observation = None
        if (
            state.is_v3
            and state.native_gemini
            and uses_supported_physical_layout(state.session)
        ):
            if state.context_plan is None:
                raise ProviderRequestError(
                    "CACHE_CONTEXT_MISMATCH",
                    "Gemini physical projection requires the frozen ContextPlan",
                )
            state.cache_material_projection = None
            if (state.snapshot.get("placement_contract_version") is None or not state.snapshot["material_placement_plan"].get("deduplicate_auto", True)) and uses_cache_material_projection_layout(state.session):
                state.cache_material_projection = await self.bindings.prepare_gemini_cache_projection(
                    material_ids=state.material_ids,
                    material_bindings=state.material_bindings,
                    session=state.session,
                    tenant_id=state.request_row["tenant_id"],
                    conversation_hash=state.request_row["conversation_hash"],
                    request_id=state.request_id,
                    session_id=state.session_id,
                    enable_projection_reuse=bool((state.context_plan or {}).get("material_reuse", {}).get("projection_reuse_enabled")),
                    cache_budget_bytes=(state.upgrade.get("material_budget") if state.wire.get("normalize") else None),
                    explicit_cache_material_mode=(state.snapshot.get("cache_contract") or {}).get("explicit_cache_material_mode", "inline_bytes_only"),
                )
            if state.cache_material_projection is not None:
                state.material_reuse_observation = state.cache_material_projection.pop("_material_reuse_observation", None)
            state.provider_physical_plan = build_gemini_physical_cache_plan(
                snapshot=state.snapshot,
                session=state.session,
                history=state.history,
                material_ids=state.material_ids,
                material_bindings=state.material_bindings,
                context_plan=state.context_plan,
                cache_material_projection=state.cache_material_projection,
                placement_session=state.plan.session_record,
                placement_history=state.plan.history,
            )
            log_info(
                logger,
                "gemini_physical_cache_plan_frozen",
                request_id=state.request_id,
                session_id=state.session_id,
                connection_id=state.snapshot.get("connection_id"),
                model=state.snapshot.get("model"),
                layout_version=state.provider_physical_plan.get("layout_version"),
                projector_version=state.provider_physical_plan.get("projector_version"),
                physical_plan_hash=state.provider_physical_plan.get("physical_plan_hash"),
                cached_prefix_wire_hash=state.provider_physical_plan.get("cached_prefix_wire_hash"),
                uncached_suffix_wire_hash=state.provider_physical_plan.get("uncached_suffix_wire_hash"),
                occurrence_mapping_hash=state.provider_physical_plan.get("occurrence_mapping_hash"),
                cache_material_ids=(state.provider_physical_plan.get("dependencies") or {}).get("cache_material_ids"),
                inference_only_session_material_ids=(state.provider_physical_plan.get("dependencies") or {}).get("inference_only_session_material_ids"),
                cacheable=state.provider_physical_plan.get("cacheable"),
                cache_layout_version=state.provider_physical_plan.get("layout_version"),
                instruction_source=(
                    "request" if state.snapshot.get("instructions") else
                    "session" if (state.provider_physical_plan.get("cached_prefix") or {}).get("systemInstruction") else
                    "none"
                ),
                request_instruction_fingerprint=(
                    stable_hash(str(state.snapshot.get("instructions"))) if state.snapshot.get("instructions") else None
                ),
                cached_instruction_fingerprint=(
                    stable_hash((state.provider_physical_plan.get("cached_prefix") or {}).get("systemInstruction"))
                    if (state.provider_physical_plan.get("cached_prefix") or {}).get("systemInstruction") is not None else None
                ),
                suffix_instruction_fingerprint=(
                    stable_hash((state.provider_physical_plan.get("uncached_suffix") or {}).get("systemInstruction"))
                    if (state.provider_physical_plan.get("uncached_suffix") or {}).get("systemInstruction") is not None else None
                ),
                stable_prefix_fingerprint=(state.context_plan or {}).get("stable_prefix_fingerprint"),
                content_fingerprint=state.provider_physical_plan.get("content_fingerprint"),
                reuse_key_hash=(stable_hash(state.provider_physical_plan.get("reuse_key")) if state.provider_physical_plan.get("reuse_key") else None),
                cache_material_representation_digests=(state.provider_physical_plan.get("dependencies") or {}).get("session_material_representation_digests"),
                cache_material_projection_hash=(state.provider_physical_plan.get("dependencies") or {}).get("cache_material_projection_hash"),
                cache_identity_diff_reasons=(
                    [reason for reason, present in [
                        ("request_instruction_present", bool(state.snapshot.get("instructions"))),
                        ("cached_instruction_present", (state.provider_physical_plan.get("cached_prefix") or {}).get("systemInstruction") is not None),
                        ("suffix_instruction_present", (state.provider_physical_plan.get("uncached_suffix") or {}).get("systemInstruction") is not None),
                        ("cache_materials_empty", not bool((state.provider_physical_plan.get("dependencies") or {}).get("cache_material_ids"))),
                        ("inference_only_materials_present", bool((state.provider_physical_plan.get("dependencies") or {}).get("inference_only_session_material_ids"))),
                    ] if present]
                ),
            )

        state.cache_binding = None
        if int(state.upgrade.get("version") or 0) >= 3:
            # Bind native URI/inline representations back to Canonical Material
            # solely for semantic-vs-wire divergence diagnosis.
            identity_bindings = list(state.material_bindings)
            if state.native_gemini and state.cache_material_projection:
                identity_bindings += state.cache_material_projection.get("cache_material_bindings") or []
            identities = {}
            for material_binding in identity_bindings:
                if (material_binding.get("content_sha256") and
                    (material_binding.get("representation") or material_binding.get("binding_kind")) in {
                        "gemini_inline_data", "gemini_external_url", "gemini_file_uri"}):
                    part = project_material_part(material_binding, material_id=material_binding.get("material_id"))
                    identities[stable_hash(part)] = {
                        "material_id": material_binding.get("material_id"),
                        "sha256": material_binding["content_sha256"],
                        "content_type": material_binding.get("content_type"),
                        "projection_sha256": material_binding.get("projection_sha256"),
                        "class": material_cache_class(material_binding.get("content_type")),
                    }
            state.wire["material_identities"] = identities
