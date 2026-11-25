from __future__ import annotations


import logging
from types import SimpleNamespace
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


    def project_auxiliary_gemini_native(self, state):
        from app.protocol_adapters.wire_projection.gemini_wire import (
            project_current_user_content,
            project_history_contents,
            project_system_instruction,
        )

        current = project_current_user_content(
            state.snapshot,
            material_ids=state.material_ids,
            material_bindings=state.material_bindings,
        )
        contents = project_history_contents(state.history) + [current]
        full_payload: dict[str, Any] = {"contents": contents}
        instruction = project_system_instruction(state.snapshot)
        if instruction is not None:
            full_payload["systemInstruction"] = instruction
        plan = {
            "protocol": "gemini_native",
            "payload_hash": stable_hash(full_payload),
            "current_user_hash": stable_hash(current),
            "visible_history_hash": stable_hash(state.history),
            "full_uncached_payload": full_payload,
        }
        state.provider_physical_plan = {**plan, "physical_plan_hash": stable_hash(plan)}

    def project_interactions(self, state):
        from app.observability.http_wire_logger import wire_context
        from app.protocol_adapters.wire_projection.gemini_interactions_wire import project_gemini_interactions_payload

        context = SimpleNamespace(
            snapshot=state.snapshot, session=state.session, history=state.history,
            material_ids=state.material_ids, material_bindings=state.material_bindings,
            continuation_execution_binding=state.continuation_binding,
        )
        with wire_context(state.wire):
            payload, current = project_gemini_interactions_payload(context)
        plan = {
            "protocol": "gemini_interactions",
            "payload_hash": stable_hash(payload),
            "current_user_hash": stable_hash(current),
            "visible_history_hash": stable_hash(state.history),
        }
        state.provider_physical_plan = {**plan, "physical_plan_hash": stable_hash(plan)}

    async def project_responses(self, state):
        from app.materials.resolver import MaterialResolver
        from app.core.material_placement import is_material_part, transport_user_parts
        from app.protocol_adapters.wire_projection.responses_wire import project_responses_current_user_item

        resolver = MaterialResolver(self.repo, self.bindings.fallback.storage, self.settings)
        current = await project_responses_current_user_item(
            state.snapshot,
            material_ids=state.material_ids,
            material_bindings=state.material_bindings,
            materials=resolver,
            tenant_id=state.request_row["tenant_id"],
            conversation_hash=state.request_row["conversation_hash"],
        )
        state._responses_current_user_item = current

        history_turns = [t for t in state.history if isinstance(t, dict) and (t.get("transport_history") or {}).get("kind") == "responses"]
        cb = state.continuation_binding
        cb_dict = cb.to_dict() if hasattr(cb, "to_dict") else (cb if isinstance(cb, dict) else {})
        covered = int(cb_dict.get("covered_turn_count") or 0) if cb_dict.get("effective_mode") == "stateful_anchor_delta" else 0
        delta_turns = history_turns[covered:] if 0 <= covered <= len(history_turns) else history_turns

        kinds = ("input_file", "input_image", "input_text")
        def _count_files(turns):
            total = 0
            text_bytes = 0
            for turn in turns:
                parts, _ = transport_user_parts(turn.get("transport_history"))
                for idx, part in enumerate(parts):
                    if is_material_part(part, kinds, is_responses=True, index=idx, total_parts=len(parts)) and part.get("type") in {"input_file", "input_image"}:
                        total += 1
                    if part.get("type") == "input_text":
                        text_bytes += len(str(part.get("text") or "").encode("utf-8"))
            return total, text_bytes

        hist_files, hist_text_bytes = _count_files(history_turns)
        delta_files, delta_text_bytes = _count_files(delta_turns)
        cur_parts = current.get("content") or []
        cur_files = sum(1 for p in cur_parts if isinstance(p, dict) and p.get("type") in {"input_file", "input_image"})
        cur_text_bytes = sum(len(str(p.get("text") or "").encode("utf-8")) for p in cur_parts if isinstance(p, dict) and p.get("type") == "input_text")

        plan = {
            "protocol": "responses",
            "current_user_hash": stable_hash(current),
            "visible_history_hash": stable_hash(state.history),
            "wire_metrics": {
                "logical_total_file_part_count": hist_files + cur_files,
                "wire_total_file_part_count": delta_files + cur_files,
                "wire_current_turn_file_part_count": cur_files,
                "history_content_count": len(history_turns),
                "history_text_bytes": delta_text_bytes if covered else hist_text_bytes,
                "current_text_bytes": cur_text_bytes,
            },
        }
        state.provider_physical_plan = {**plan, "physical_plan_hash": stable_hash(plan)}

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

        effective_protocol = state.snapshot.get("effective_wire_protocol") or state.snapshot.get("protocol")
        if effective_protocol == "gemini_interactions":
            self.project_interactions(state)
        elif effective_protocol == "gemini_native" and state.snapshot.get("protocol") == "gemini_interactions":
            self.project_auxiliary_gemini_native(state)
        elif state.snapshot.get("protocol") == "responses" and state.snapshot.get("placement_contract_version") is not None:
            await self.project_responses(state)

        if state.cache_binding_obj is None:
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
