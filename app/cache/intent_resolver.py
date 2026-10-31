from __future__ import annotations

from typing import Any

from .contracts import CacheDecisionError, CacheIntentPlan
from .projections import freeze_cache_projection
from .economics import (
    CACHE_READ_INPUT_RATIO,
    EXPLICIT_CREATE_INPUT_RATIO,
    estimate_future_reuses,
    estimate_ttl_bounded_future_reuses,
    evaluate_relative_cache_strategy,
)



def _binding_composition_declaration(owner: dict[str, Any]) -> dict[str, Any]:
    raw = owner.get("binding_composition") if isinstance(owner.get("binding_composition"), dict) else {}
    max_bindings = raw.get("max_bindings_per_request", owner.get("max_bindings_per_request", 1))
    if isinstance(max_bindings, bool) or not isinstance(max_bindings, int) or max_bindings < 1:
        max_bindings = 1
    combinations_raw = raw.get(
        "supported_binding_combinations",
        owner.get("supported_binding_combinations", []),
    )
    combinations: set[tuple[str, ...]] = set()
    if isinstance(combinations_raw, list):
        for item in combinations_raw:
            mechanisms = item.get("mechanisms") if isinstance(item, dict) else item
            if not isinstance(mechanisms, (list, tuple)):
                continue
            normalized = tuple(sorted(str(value).strip() for value in mechanisms if str(value).strip()))
            if len(normalized) > 1:
                combinations.add(normalized)
    projection_raw = raw.get("supported_projection_kinds", owner.get("supported_projection_kinds", []))
    projection_kinds = {
        str(value).strip().lower()
        for value in (projection_raw if isinstance(projection_raw, (list, tuple, set)) else [])
        if str(value).strip()
    }
    return {
        "max_bindings_per_request": max_bindings,
        "supported_binding_combinations": combinations,
        "supported_projection_kinds": projection_kinds,
        "allow_overlapping_projections": raw.get(
            "allow_overlapping_projections", owner.get("allow_overlapping_projections", False)
        ) is True,
        "wire_strategy": str(raw.get("wire_strategy") or "").strip() or None,
    }


def _binding_combination_supported(
    *, contract: dict[str, Any], profile_cache: dict[str, Any], mechanisms: list[str]
) -> tuple[bool, dict[str, Any]]:
    contract_declaration = _binding_composition_declaration(contract)
    profile_declaration = _binding_composition_declaration(profile_cache)
    combination = tuple(sorted(str(value) for value in mechanisms))
    allowed = (
        len(combination) <= contract_declaration["max_bindings_per_request"]
        and len(combination) <= profile_declaration["max_bindings_per_request"]
        and combination in contract_declaration["supported_binding_combinations"]
        and combination in profile_declaration["supported_binding_combinations"]
        and bool(profile_declaration["wire_strategy"])
    )
    return allowed, {
        "mechanisms": list(combination),
        "max_bindings_per_request": min(
            contract_declaration["max_bindings_per_request"],
            profile_declaration["max_bindings_per_request"],
        ),
        "wire_strategy": profile_declaration["wire_strategy"],
        "contract_declared": combination in contract_declaration["supported_binding_combinations"],
        "profile_declared": combination in profile_declaration["supported_binding_combinations"],
    }


def _additional_binding_plans(
    *,
    policy: dict[str, Any],
    contract: dict[str, Any],
    profile_cache: dict[str, Any],
    candidates: list[str],
    primary_mechanism: str,
    primary_projection: dict[str, Any],
    context_plan: dict[str, Any],
    mechanism_profiles: dict[str, Any],
    attachment: dict[str, Any],
    prediction: dict[str, Any],
    economics: dict[str, Any],
    verified: bool,
    mode: str,
    provider: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    from itertools import combinations

    metadata = policy.get("metadata") if isinstance(policy.get("metadata"), dict) else {}
    raw_rules = metadata.get("additional_cache_bindings")
    if raw_rules is None:
        raw_rules = metadata.get("binding_rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        return [], [], {"binding_count": 1, "combination_supported": True, "mechanisms": [primary_mechanism]}
    if len(raw_rules) > 16:
        raise CacheDecisionError(
            "CACHE_BINDING_PLAN_INVALID",
            "Additional cache binding rule count exceeds the supported planning limit",
            reason="too_many_additional_binding_rules",
        )

    contract_composition = _binding_composition_declaration(contract)
    profile_composition = _binding_composition_declaration(profile_cache)
    primary_segments = set(primary_projection.get("segment_ids") or [])
    binding_ids = {"primary"}
    seen_projection_keys: set[tuple[str, str]] = set()
    candidates_to_select: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    docx_ids = {
        str(value) for value in (attachment.get("docx_explicit_cache_candidate_ids") or []) if str(value)
    }
    allow_overlap = (
        contract_composition["allow_overlapping_projections"]
        and profile_composition["allow_overlapping_projections"]
    )

    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            rejections.append({"binding_id": f"additional-{index + 1}", "reason": "binding_rule_invalid"})
            continue
        binding_id = str(raw_rule.get("binding_id") or f"additional-{index + 1}").strip()
        mechanism = str(raw_rule.get("mechanism") or "").strip().lower()
        required = raw_rule.get("required") is True
        if not binding_id or len(binding_id) > 100 or binding_id in binding_ids:
            reason = "binding_id_invalid_or_duplicate"
            if required:
                raise CacheDecisionError("CACHE_BINDING_PLAN_INVALID", "Additional cache binding identity is invalid", reason=reason)
            rejections.append({"binding_id": binding_id or None, "reason": reason})
            continue
        binding_ids.add(binding_id)
        if mechanism not in {"stateful_resource", "breakpoint", "implicit_prefix"} or mechanism not in candidates:
            reason = "mechanism_not_certified_by_supply_and_profile"
            if required:
                raise CacheDecisionError("CACHE_BINDING_UNSUPPORTED", "Required additional cache mechanism is not certified", reason=reason)
            rejections.append({"binding_id": binding_id, "mechanism": mechanism or None, "reason": reason})
            continue
        try:
            projection = freeze_cache_projection(
                spec=(raw_rule.get("projection") if isinstance(raw_rule.get("projection"), dict) else {}),
                context_plan=context_plan,
            )
        except ValueError as exc:
            reason = str(exc)
            if required:
                raise CacheDecisionError("CACHE_BINDING_PROJECTION_INVALID", "Required additional cache projection is invalid", reason=reason) from exc
            rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason})
            continue

        projection_kind = str(projection.get("kind") or "")
        if (
            projection_kind not in contract_composition["supported_projection_kinds"]
            or projection_kind not in profile_composition["supported_projection_kinds"]
        ):
            reason = "projection_kind_not_certified_by_supply_and_profile"
            if required:
                raise CacheDecisionError("CACHE_BINDING_PROJECTION_UNSUPPORTED", "Required cache projection is not certified", reason=reason)
            rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason})
            continue

        projection_segments = set(projection.get("segment_ids") or [])
        overlap_primary = primary_segments.intersection(projection_segments)
        if overlap_primary and not allow_overlap:
            reason = "overlapping_cache_projections_not_certified"
            if required:
                raise CacheDecisionError("CACHE_BINDING_PROJECTION_CONFLICT", "Required cache projection overlaps the Primary Cache without explicit certification", reason=reason)
            rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason, "overlap_segment_ids": sorted(overlap_primary)})
            continue

        projection_key = (mechanism, str(projection.get("projection_hash") or ""))
        if projection_key in seen_projection_keys:
            reason = "duplicate_cache_binding_projection"
            if required:
                raise CacheDecisionError("CACHE_BINDING_PLAN_INVALID", "Duplicate cache bindings target the same mechanism and projection", reason=reason)
            rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason})
            continue
        seen_projection_keys.add(projection_key)

        cfg = dict(mechanism_profiles.get(mechanism) or {}) if isinstance(mechanism_profiles.get(mechanism), dict) else {}
        threshold_mode = str(cfg.get("threshold_mode") or "unknown").lower()
        minimum_tokens = cfg.get("minimum_cacheable_tokens")
        assessment = context_plan.get("token_assessment") if isinstance(context_plan.get("token_assessment"), dict) else {}
        resolution_status = "finalized"
        if mechanism == "implicit_prefix" and str(provider or "").lower() == "gemini":
            if type(minimum_tokens) is not int or minimum_tokens <= 0:
                reason = "implicit_prefix_threshold_unverified"
                if required:
                    raise CacheDecisionError("CACHE_BINDING_THRESHOLD_UNVERIFIED", "Required implicit-prefix threshold is not certified", reason=reason)
                rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason})
                continue
            lower, upper = assessment.get("lower"), assessment.get("upper")
            if projection_kind != "full_stable_prefix":
                if cfg.get("final_threshold_guard") is True:
                    resolution_status = "pending"
                else:
                    reason = "projection_threshold_requires_provider_measurement"
                    if required:
                        raise CacheDecisionError("CACHE_BINDING_THRESHOLD_UNCERTAIN", "Required implicit-prefix projection needs Provider-side token measurement", reason=reason)
                    rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason})
                    continue
            elif type(lower) is int and lower >= minimum_tokens:
                resolution_status = "finalized"
            elif type(upper) is int and upper < minimum_tokens:
                reason = "below_minimum"
                if required:
                    raise CacheDecisionError("CACHE_BINDING_BELOW_MINIMUM", "Required implicit-prefix projection is below Provider minimum", reason=reason)
                rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason})
                continue
            elif cfg.get("final_threshold_guard") is True:
                resolution_status = "pending"
            else:
                reason = "context_assessment_uncertain"
                if required:
                    raise CacheDecisionError("CACHE_BINDING_THRESHOLD_UNCERTAIN", "Required implicit-prefix projection has no exact token evidence", reason=reason)
                rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason})
                continue
        elif threshold_mode == "unknown" and cfg.get("final_threshold_guard") is not True:
            reason = "cache_threshold_unverified"
            if required:
                raise CacheDecisionError("CACHE_BINDING_THRESHOLD_UNVERIFIED", "Required cache binding has no certified threshold", reason=reason)
            rejections.append({"binding_id": binding_id, "mechanism": mechanism, "reason": reason})
            continue

        selected_material_ids = [str(value) for value in projection.get("material_ids") or []]
        selected_docx_ids = sorted(set(selected_material_ids).intersection(docx_ids))
        is_docx_projection = bool(selected_docx_ids)
        if mechanism == "stateful_resource":
            cfg["auto_economics"] = {
                "price_schedule": dict(economics.get("price_schedule") or {}) if isinstance(economics.get("price_schedule"), dict) else {},
                "reuse_prediction": prediction,
                "expected_future_reuses": prediction.get("expected_future_reuses", 0),
                "max_new_resource_cost": economics.get("max_new_resource_cost"),
                "safety_margin_ratio": economics.get("safety_margin_ratio", .20),
                "cache_read_input_ratio": CACHE_READ_INPUT_RATIO,
                "explicit_create_input_ratio": EXPLICIT_CREATE_INPUT_RATIO,
                "observed_implicit_hit_rate": None,
                "observed_implicit_hit_rate_source": None,
                "observed_implicit_samples": 0,
                "certified_implicit_alternative": "implicit_prefix" in candidates and verified,
                "enable_history_growth": False,
                "minimum_additional_tokens": economics.get("minimum_additional_tokens", 4096),
                "new_economics_policy": "relative_cache/2" if str(provider or "").lower() == "gemini" else "legacy_forecast/1",
                "attachment_evidence": dict(attachment),
                "existing_resource_candidate": False,
                "docx_explicit_cache_priority": bool(is_docx_projection and verified),
                "docx_explicit_cache_candidate_ids": selected_docx_ids,
                "docx_cache_policy_version": "relay-docx-explicit-priority/1" if is_docx_projection else None,
            }
        candidates_to_select.append({
            "binding_id": binding_id,
            "role": "additional",
            "mechanism": mechanism,
            "projection": projection,
            "projection_hash": projection.get("projection_hash"),
            "mechanism_config": cfg,
            "resolution_status": resolution_status,
            "required": required,
            "decision_reason": "selected_by_frozen_binding_rule",
            "selection_evidence": {
                "projection_kind": projection_kind,
                "projection_material_ids": selected_material_ids,
                "overlapping_primary_segment_ids": sorted(overlap_primary),
                "docx_explicit_cache_priority": is_docx_projection and verified,
            },
        })

    required_candidates = [item for item in candidates_to_select if item.get("required") is True]
    optional_candidates = [item for item in candidates_to_select if item.get("required") is not True]
    chosen: list[dict[str, Any]] | None = None
    chosen_composition: dict[str, Any] | None = None
    for optional_count in range(len(optional_candidates), -1, -1):
        for optional_subset in combinations(optional_candidates, optional_count):
            subset_ids = {str(item.get("binding_id")) for item in optional_subset}
            subset = [
                item for item in candidates_to_select
                if item.get("required") is True or str(item.get("binding_id")) in subset_ids
            ]
            mechanisms = [primary_mechanism, *[str(item.get("mechanism")) for item in subset]]
            if not allow_overlap:
                occupied = set(primary_segments)
                overlap_found = False
                for item in subset:
                    selected_segments = set((item.get("projection") or {}).get("segment_ids") or [])
                    if occupied.intersection(selected_segments):
                        overlap_found = True
                        break
                    occupied.update(selected_segments)
                if overlap_found:
                    continue
            if len(mechanisms) == 1:
                supported = True
                composition = {
                    "mechanisms": list(mechanisms),
                    "max_bindings_per_request": min(
                        contract_composition["max_bindings_per_request"],
                        profile_composition["max_bindings_per_request"],
                    ),
                    "wire_strategy": profile_composition["wire_strategy"],
                    "contract_declared": True,
                    "profile_declared": True,
                }
            else:
                supported, composition = _binding_combination_supported(
                    contract=contract,
                    profile_cache=profile_cache,
                    mechanisms=mechanisms,
                )
            if supported:
                chosen = subset
                chosen_composition = composition
                break
        if chosen is not None:
            break

    if chosen is None:
        if required_candidates:
            raise CacheDecisionError(
                "CACHE_BINDING_COMBINATION_UNSUPPORTED",
                "Required additional cache bindings do not form a Provider-certified combination",
                reason="provider_binding_combination_not_certified",
            )
        chosen = []
        chosen_composition = {
            "mechanisms": [primary_mechanism],
            "max_bindings_per_request": min(
                contract_composition["max_bindings_per_request"],
                profile_composition["max_bindings_per_request"],
            ),
            "wire_strategy": profile_composition["wire_strategy"],
            "contract_declared": True,
            "profile_declared": True,
        }

    chosen_ids = {str(item.get("binding_id")) for item in chosen}
    for item in candidates_to_select:
        if str(item.get("binding_id")) not in chosen_ids:
            rejections.append({
                "binding_id": item.get("binding_id"),
                "mechanism": item.get("mechanism"),
                "reason": "provider_binding_combination_not_certified",
            })
    return chosen, rejections, {
        **(chosen_composition or {}),
        "binding_count": 1 + len(chosen),
        "combination_supported": True,
        "additional_binding_count": len(chosen),
    }


class CacheIntentResolver:
    """Pure, replayable requested -> planned cache semantics.

    No network access, no native cache resources and no price guesses. A frozen
    plan selects a default primary binding and may include additional concurrent
    bindings only when the exact combination is certified by both the Supply
    contract and Protocol Profile. Fallbacks remain mutually-exclusive and
    pre-dispatch only. Runtime cannot invent a candidate after the dispatch seal.
    """

    resolver_version = "relay-cache-resolver/8"

    def resolve(
        self, *, requested_mode: str, context_plan: dict[str, Any],
        cache_contract: dict[str, Any] | None,
        protocol_profile: dict[str, Any] | None,
        cache_policy: dict[str, Any] | None,
        cache_contract_hash: str | None, profile_hash: str | None,
        policy_hash: str | None, provider: str | None = None,
        model: str | None = None, requested_mechanism: str = "auto",
        mechanism_explicit: bool = False,
        reuse_evidence: dict[str, Any] | None = None,
        attachment_evidence: dict[str, Any] | None = None,
    ) -> CacheIntentPlan:
        mode = str(requested_mode or "auto").strip().lower()
        preference = str(requested_mechanism or "auto").strip().lower()
        if mode not in {"off", "auto", "on"}:
            raise CacheDecisionError("CACHE_MODE_INVALID", f"Unsupported cache mode: {requested_mode!r}")
        if preference not in {"auto", "implicit_prefix", "stateful_resource"}:
            raise CacheDecisionError("CACHE_MECHANISM_INVALID", f"Unsupported cache mechanism: {requested_mechanism!r}")
        scope = str((cache_policy or {}).get("scope") or "session")
        if scope != "session":
            # This version never broadens the authorized reuse boundary.
            raise CacheDecisionError("CACHE_SCOPE_INVALID", "This Relay cache plan requires Session isolation")
        if mode == "off":
            if preference != "auto":
                raise CacheDecisionError("CACHE_INTENT_CONFLICT", "cache off cannot force a mechanism")
            return self._none(mode, preference, context_plan, policy_hash, profile_hash,
                              cache_contract_hash, scope, "requested_off")

        contract = cache_contract if isinstance(cache_contract, dict) else {}
        policy = cache_policy if isinstance(cache_policy, dict) else {}
        profile_cache = (protocol_profile or {}).get("cache") if isinstance(protocol_profile, dict) else {}
        profile_cache = profile_cache if isinstance(profile_cache, dict) else {}
        raw = contract.get("supported_mechanisms") or contract.get("mechanisms") or []
        declared = [str(v) for v in raw if str(v) in {"stateful_resource", "implicit_prefix", "breakpoint"}]
        raw_profile_mechanisms = profile_cache.get("supported_mechanisms")
        supported_by_profile = {
            str(x) for x in (raw_profile_mechanisms or declared)
        }
        candidates = [m for m in declared if m in supported_by_profile]
        # Provider-managed implicit prefix caching is certified only when both
        # the frozen Supply contract and its Protocol Profile explicitly name
        # the mechanism. A missing/empty profile declaration is not evidence.
        if "implicit_prefix" in declared and not (
            isinstance(raw_profile_mechanisms, (list, tuple, set))
            and "implicit_prefix" in {str(x) for x in raw_profile_mechanisms}
        ):
            candidates = [m for m in candidates if m != "implicit_prefix"]
        verified = str(contract.get("verification_status") or "unverified").lower() in {
            "verified", "published", "legacy_verified"
        }
        # Historical Gemini admission behavior is preserved ONLY for callers
        # that never supplied a mechanism preference. It does not certify a
        # mechanism on the caller's behalf or authorize a new forced preference.
        legacy_gemini_attempt = (
            not mechanism_explicit and str(provider or "").lower() == "gemini"
            and str(model or "").lower().startswith("gemini-")
        )
        if legacy_gemini_attempt and not verified:
            # Preserve the historical explicit-resource admission override,
            # but never infer Gemini's provider-managed implicit cache support.
            candidates = [item for item in candidates if item == "stateful_resource"]
        frozen_forecast_policy = policy.get("metadata") if isinstance(policy.get("metadata"), dict) else {}
        policy_metadata = frozen_forecast_policy
        economics = frozen_forecast_policy.get("auto_economics") if isinstance(frozen_forecast_policy.get("auto_economics"), dict) else {}
        use_stats = reuse_evidence if isinstance(reuse_evidence, dict) else {}
        profiles = contract.get("mechanism_profiles") if isinstance(contract.get("mechanism_profiles"), dict) else {}
        stateful_profile = profiles.get("stateful_resource") if isinstance(profiles.get("stateful_resource"), dict) else {}
        ttl = stateful_profile.get("ttl_seconds")
        ttl = ttl if type(ttl) is int and ttl > 0 else 3600
        observed = use_stats.get("observed_compatible_uses", 0)
        if type(observed) is not int or observed < 0:
            observed = 0
        if str(provider or "").lower() == "gemini" and mode == "auto":
            prediction = estimate_ttl_bounded_future_reuses(
                policy=economics,
                evidence=use_stats,
                ttl_seconds=ttl,
            )
        else:
            prediction = estimate_future_reuses(policy=economics, observed_compatible_uses=observed)

        def none(reason: str, rejected: list[dict[str, Any]] | None = None) -> CacheIntentPlan:
            return self._none(mode, preference, context_plan, policy_hash, profile_hash,
                              cache_contract_hash, scope, reason,
                              reuse_prediction=prediction, rejections=rejected)

        if not candidates:
            if mode == "on":
                raise CacheDecisionError("CACHE_UNSUPPORTED", "Frozen supply/profile has no supported cache mechanism")
            return none("mechanism_unavailable")
        if not verified and not legacy_gemini_attempt:
            if mode == "on":
                raise CacheDecisionError("CACHE_PROTOCOL_UNVERIFIED", "Frozen supply cache mechanism is not certified")
            return none("protocol_unverified")
        if bool(policy.get("deny", False)):
            if mode == "on":
                raise CacheDecisionError("CACHE_POLICY_DENIED", "Frozen policy prohibits caching")
            return none("policy_denied")
        if bool(policy.get("cross_session_sharing")):
            raise CacheDecisionError("CACHE_SCOPE_INVALID", "Cross-Session cache sharing is not authorized")
        if mode == "auto" and bool(policy.get("auto_disabled", False)):
            return none("auto_policy_skip")

        if preference != "auto" and preference not in candidates:
            if mode == "on":
                raise CacheDecisionError("CACHE_MECHANISM_UNAVAILABLE", "Requested cache mechanism is not certified by the frozen supply/profile")
            return none("requested_mechanism_unavailable")

        # Admission is pure and consumes only frozen canonical facts / read-only
        # Session inventory evidence. A resource hint can influence mechanism
        # selection, but only StatefulResourceManager may prove exact compatible
        # prefix fingerprints and provider-handle validity before dispatch.
        attachment = attachment_evidence if isinstance(attachment_evidence, dict) else {}
        primary_projection_spec = (
            policy_metadata.get("primary_cache_projection")
            if isinstance(policy_metadata.get("primary_cache_projection"), dict)
            else {"kind": "full_stable_prefix"}
        )
        try:
            primary_projection = freeze_cache_projection(
                spec=primary_projection_spec,
                context_plan=context_plan,
            )
        except ValueError as exc:
            if mode == "on":
                raise CacheDecisionError(
                    "CACHE_PRIMARY_PROJECTION_INVALID",
                    "Frozen primary cache projection is invalid",
                    reason=str(exc),
                ) from exc
            return none(str(exc))
        resource_hint = use_stats.get("existing_resource_candidate") is True
        stable_large_attachment = attachment.get("large_stable_attachment_candidate") is True
        # Request-only attachment split is not semantically proven by Gemini
        # layout/9 or /10. It cannot justify choosing a billable explicit mechanism.
        explicit_attachment_candidate = (
            stable_large_attachment
            and bool(str(provider or "").lower() == "gemini")
        )
        primary_material_ids = {
            str(value) for value in (primary_projection.get("material_ids") or []) if str(value)
        }
        docx_candidate_ids = {
            str(value) for value in (attachment.get("docx_explicit_cache_candidate_ids") or []) if str(value)
        }
        docx_explicit_candidate = bool(
            str(provider or "").lower() == "gemini"
            and attachment.get("docx_explicit_cache_candidate") is True
            and primary_material_ids.intersection(docx_candidate_ids)
        )
        explicit_profile_declared = (
            isinstance(raw_profile_mechanisms, (list, tuple, set))
            and "stateful_resource" in {str(x) for x in raw_profile_mechanisms}
        )
        docx_explicit_priority = bool(
            docx_explicit_candidate
            and verified
            and "stateful_resource" in candidates
            and explicit_profile_declared
            and preference in {"auto", "stateful_resource"}
        )
        if docx_explicit_priority:
            docx_priority_block_reason = None
        elif docx_explicit_candidate and not verified:
            docx_priority_block_reason = "supply_cache_contract_unverified"
        elif docx_explicit_candidate and "stateful_resource" not in candidates:
            docx_priority_block_reason = "explicit_resource_not_declared_by_supply_and_profile"
        elif docx_explicit_candidate and not explicit_profile_declared:
            docx_priority_block_reason = "protocol_profile_explicit_resource_not_certified"
        elif docx_explicit_candidate and preference == "implicit_prefix":
            docx_priority_block_reason = "caller_explicitly_selected_implicit_prefix"
        else:
            docx_priority_block_reason = attachment.get("docx_explicit_cache_block_reason")
        implicit_contract_declared = "implicit_prefix" in declared
        implicit_profile_declared = (
            isinstance(raw_profile_mechanisms, (list, tuple, set))
            and "implicit_prefix" in {str(x) for x in raw_profile_mechanisms}
        )
        if implicit_contract_declared and implicit_profile_declared and verified:
            implicit_prefix_capability = "certified"
        elif implicit_contract_declared and implicit_profile_declared:
            implicit_prefix_capability = "unverified"
        elif implicit_contract_declared:
            implicit_prefix_capability = "profile_not_declared"
        else:
            implicit_prefix_capability = "unsupported"
        stable_prefix_ids = context_plan.get("stable_prefix_segment_ids") or []
        minimum_observations = economics.get("minimum_observations", 3)
        if type(minimum_observations) is not int or minimum_observations < 2:
            minimum_observations = 3
        high_frequency_candidate = bool(
            observed >= minimum_observations
            or int(prediction.get("expected_future_reuses") or 0) > 0
        )
        explicit_assessment_candidate = bool(
            stable_prefix_ids and (
                explicit_attachment_candidate or high_frequency_candidate or docx_explicit_priority
            )
        )
        explicit_first_read_advantage = False
        explicit_cost_preview: dict[str, Any] = {}
        if (
            mode == "auto"
            and "stateful_resource" in candidates
            and not docx_explicit_priority
            and (explicit_assessment_candidate or resource_hint)
        ):
            # This normalized preview is linear in the measured prefix size.
            # Exact token count and any hard monetary cap remain execution-time
            # checks immediately before the create side effect.
            explicit_cost_preview = evaluate_relative_cache_strategy(
                tokens=1_000_000,
                ttl_seconds=ttl,
                config={
                    "cache_read_input_ratio": CACHE_READ_INPUT_RATIO,
                    "explicit_create_input_ratio": EXPLICIT_CREATE_INPUT_RATIO,
                    "expected_future_reuses": prediction.get("expected_future_reuses", 0),
                    "observed_implicit_hit_rate": use_stats.get("observed_implicit_hit_rate"),
                    "observed_implicit_hit_rate_source": use_stats.get("observed_implicit_hit_rate_source"),
                    "certified_implicit_alternative": "implicit_prefix" in candidates and verified,
                    "safety_margin_ratio": economics.get("safety_margin_ratio", 0.20),
                    "price_schedule": economics.get("price_schedule") or {},
                    "max_new_resource_cost": economics.get("max_new_resource_cost"),
                },
                forced=False,
                enforce_hard_budget=False,
            )
            explicit_first_read_advantage = explicit_cost_preview.get("allow") is True

        if preference == "auto":
            if mode == "auto" and str(provider or "").lower() == "gemini":
                select_explicit = docx_explicit_priority or resource_hint or (
                    explicit_assessment_candidate and explicit_first_read_advantage
                )
                preferred = (
                    ["stateful_resource", "implicit_prefix", "breakpoint"] if select_explicit
                    else ["implicit_prefix", "stateful_resource", "breakpoint"]
                )
            elif docx_explicit_priority and "stateful_resource" in candidates:
                preferred = ["stateful_resource", "implicit_prefix", "breakpoint"]
            else:
                preferred = [str(x) for x in (policy.get("mechanism_preference") or candidates)]
            ordered = [x for x in preferred if x in candidates] + [x for x in candidates if x not in preferred]
        elif mode == "auto" and preference == "stateful_resource" and "implicit_prefix" in candidates:
            # The explicit preference is soft under auto: evaluate it first only
            # when an existing resource or the relative cost preview supports it.
            # If execution-time measurement reverses that result, implicit is
            # already frozen as the permitted pre-dispatch fallback.
            ordered = (
                ["stateful_resource", "implicit_prefix"]
                if docx_explicit_priority or resource_hint or explicit_first_read_advantage
                else ["implicit_prefix", "stateful_resource"]
            )
        elif docx_explicit_priority and preference == "auto" and "stateful_resource" in candidates:
            ordered = ["stateful_resource", "implicit_prefix", "breakpoint"]
        else:
            ordered = [preference]

        rejections: list[dict[str, Any]] = []
        assessment = context_plan.get("token_assessment") if isinstance(context_plan.get("token_assessment"), dict) else {}
        eligible: list[tuple[str, dict[str, Any], bool]] = []
        for mechanism in ordered:
            cfg = dict(profiles.get(mechanism) or {}) if isinstance(profiles.get(mechanism), dict) else {}
            gemini_implicit = str(provider or "").lower() == "gemini" and mechanism == "implicit_prefix"
            if legacy_gemini_attempt and not verified:
                cfg["system_admission_override"] = "gemini_model_cache_attempt"
                cfg["upstream_verification_status"] = str(contract.get("verification_status") or "unverified")
                if mechanism == "stateful_resource" and str(cfg.get("threshold_mode") or "unknown").lower() == "unknown":
                    cfg["threshold_mode"] = "provider_create"
                    cfg["threshold_admission_override"] = "provider_create"
            threshold = str(cfg.get("threshold_mode") or "unknown").lower()
            minimum = cfg.get("minimum_cacheable_tokens")
            below = not stable_prefix_ids
            uncertain = False
            if not below:
                if threshold in {"not_applicable", "provider_create", "provider_enforced"}:
                    uncertain = gemini_implicit
                elif type(minimum) is int and minimum >= 0:
                    lo, hi = assessment.get("lower"), assessment.get("upper")
                    if type(lo) is int and lo >= minimum:
                        pass
                    elif type(hi) is int and hi < minimum:
                        below = True
                    else:
                        uncertain = True
                else:
                    uncertain = True
            if below:
                rejections.append({"mechanism": mechanism, "reason": "below_minimum"})
                continue
            if uncertain and not bool(cfg.get("final_threshold_guard")):
                rejections.append({"mechanism": mechanism, "reason": "context_assessment_uncertain"})
                continue
            if gemini_implicit and uncertain and (type(minimum) is not int or minimum <= 0):
                rejections.append({"mechanism": mechanism, "reason": "implicit_prefix_threshold_unverified"})
                continue

            eligible.append((mechanism, cfg, uncertain))

        if eligible:
            mechanism, cfg, uncertain = eligible[0]

            if mechanism == "stateful_resource":
                cfg["auto_economics"] = {
                    "price_schedule": dict(economics.get("price_schedule") or {}) if isinstance(economics.get("price_schedule"), dict) else {},
                    "reuse_prediction": prediction,
                    "expected_future_reuses": prediction.get("expected_future_reuses", 0),
                    "max_new_resource_cost": economics.get("max_new_resource_cost"),
                    "safety_margin_ratio": economics.get("safety_margin_ratio", .20),
                    "cache_read_input_ratio": CACHE_READ_INPUT_RATIO,
                    "explicit_create_input_ratio": EXPLICIT_CREATE_INPUT_RATIO,
                    "observed_implicit_hit_rate": use_stats.get("observed_implicit_hit_rate"),
                    "observed_implicit_hit_rate_source": use_stats.get("observed_implicit_hit_rate_source"),
                    "observed_implicit_samples": use_stats.get("observed_implicit_samples", 0),
                    "certified_implicit_alternative": bool("implicit_prefix" in candidates and verified),
                    "enable_history_growth": economics.get("enable_history_growth") is True,
                    "minimum_additional_tokens": economics.get("minimum_additional_tokens", 4096),
                    "new_economics_policy": "relative_cache/2" if str(provider or "").lower() == "gemini" else "legacy_forecast/1",
                    "attachment_evidence": dict(attachment),
                    "existing_resource_candidate": resource_hint,
                    "docx_explicit_cache_priority": bool(docx_explicit_priority),
                    "docx_explicit_cache_candidate_ids": (
                        list(attachment.get("docx_explicit_cache_candidate_ids") or [])
                        if docx_explicit_priority else []
                    ),
                    "docx_cache_policy_version": "relay-docx-explicit-priority/1",
                }

            fallback_mechanisms: list[dict[str, Any]] = []
            if (
                mode == "auto"
                and str(provider or "").lower() == "gemini"
                and verified
                and preference in {"auto", "stateful_resource"}
                and mechanism == "stateful_resource"
            ):
                for fallback_name, fallback_cfg, fallback_uncertain in eligible[1:]:
                    if fallback_name != "implicit_prefix":
                        continue
                    fallback_mechanisms.append({
                        "mechanism": fallback_name,
                        "mechanism_config": fallback_cfg,
                        "resolution_status": "pending" if fallback_uncertain else "finalized",
                        "final_threshold_guard": bool(fallback_cfg.get("final_threshold_guard")),
                    })
                    break

            if fallback_mechanisms:
                fallback_projection_spec = (
                    policy_metadata.get("fallback_cache_projection")
                    if isinstance(policy_metadata.get("fallback_cache_projection"), dict)
                    else {"kind": "full_stable_prefix"}
                )
                try:
                    fallback_projection = freeze_cache_projection(
                        spec=fallback_projection_spec,
                        context_plan=context_plan,
                    )
                except ValueError as exc:
                    fallback_mechanisms = []
                    rejections.append({"mechanism": "implicit_prefix", "reason": str(exc), "candidate_role": "fallback"})
                else:
                    if str(fallback_projection.get("kind") or "") != "full_stable_prefix":
                        contract_composition = _binding_composition_declaration(contract)
                        profile_composition = _binding_composition_declaration(profile_cache)
                        fallback_kind = str(fallback_projection.get("kind") or "")
                        if (
                            fallback_kind not in contract_composition["supported_projection_kinds"]
                            or fallback_kind not in profile_composition["supported_projection_kinds"]
                        ):
                            fallback_mechanisms = []
                            rejections.append({"mechanism": "implicit_prefix", "reason": "fallback_projection_not_certified", "candidate_role": "fallback"})
                        else:
                            fallback_mechanisms = [
                                {**item, "projection": fallback_projection, "projection_hash": fallback_projection.get("projection_hash")}
                                for item in fallback_mechanisms
                            ]
                    else:
                        fallback_mechanisms = [
                            {**item, "projection": fallback_projection, "projection_hash": fallback_projection.get("projection_hash")}
                            for item in fallback_mechanisms
                        ]

            selection_evidence = {
                "existing_resource_candidate": resource_hint,
                "resource_hint_is_compatibility_proof": False,
                "stable_attachment_bytes": attachment.get("stable_attachment_bytes", 0),
                "large_stable_attachment_candidate": stable_large_attachment,
                "high_frequency_candidate": high_frequency_candidate,
                "observed_compatible_uses": observed,
                "observed_implicit_hit_rate": use_stats.get("observed_implicit_hit_rate"),
                "observed_implicit_samples": use_stats.get("observed_implicit_samples", 0),
                "explicit_cost_advantage": explicit_first_read_advantage,
                "explicit_cost_preview": explicit_cost_preview,
                "docx_material_count": attachment.get("docx_material_count", 0),
                "stable_docx_material_count": attachment.get("stable_docx_material_count", 0),
                "cacheable_stable_docx_material_count": attachment.get("cacheable_stable_docx_material_count", 0),
                "docx_explicit_cache_priority": docx_explicit_priority,
                "docx_explicit_cache_priority_block_reason": docx_priority_block_reason,
                "docx_explicit_cache_candidate_ids": (
                    list(attachment.get("docx_explicit_cache_candidate_ids") or [])
                    if docx_explicit_priority else []
                ),
                "implicit_prefix_capability": implicit_prefix_capability,
                "implicit_prefix_eligible": any(item[0] == "implicit_prefix" for item in eligible),
                "selection_basis": (
                    "docx_explicit_cache_priority" if docx_explicit_priority
                    else "same_session_usage_ttl_and_frozen_context"
                ),
            }
            if str(primary_projection.get("kind") or "") != "full_stable_prefix":
                contract_composition = _binding_composition_declaration(contract)
                profile_composition = _binding_composition_declaration(profile_cache)
                primary_kind = str(primary_projection.get("kind") or "")
                if (
                    primary_kind not in contract_composition["supported_projection_kinds"]
                    or primary_kind not in profile_composition["supported_projection_kinds"]
                ):
                    if mode == "on":
                        raise CacheDecisionError(
                            "CACHE_PRIMARY_PROJECTION_UNSUPPORTED",
                            "Primary cache projection is not certified by both contracts",
                            reason="projection_kind_not_certified_by_supply_and_profile",
                        )
                    return none("primary_projection_not_certified", rejections)

            additional_bindings, additional_rejections, binding_composition = _additional_binding_plans(
                policy=policy,
                contract=contract,
                profile_cache=profile_cache,
                candidates=candidates,
                primary_mechanism=mechanism,
                primary_projection=primary_projection,
                context_plan=context_plan,
                mechanism_profiles=profiles,
                attachment=attachment,
                prediction=prediction,
                economics=economics,
                verified=verified,
                mode=mode,
                provider=provider,
            )
            if any(str(item.get("mechanism") or "") == "implicit_prefix" for item in additional_bindings):
                # An explicitly planned additional implicit binding is the
                # concurrent path; do not also freeze it as a mutually-exclusive
                # fallback for the primary explicit binding.
                fallback_mechanisms = []
            rejections.extend(additional_rejections)
            selection_evidence["primary_projection_hash"] = primary_projection.get("projection_hash")
            selection_evidence["fallback_projection_hash"] = (
                fallback_projection.get("projection_hash") if fallback_mechanisms and isinstance(fallback_projection, dict) else None
            )
            selection_evidence["additional_cache_binding_count"] = len(additional_bindings)
            selection_evidence["additional_cache_binding_rejections"] = additional_rejections

            return CacheIntentPlan(
                requested_mode=mode, requested_mechanism=preference,
                planned_mechanism=mechanism,
                resolution_status="pending" if uncertain else "finalized",
                decision_reason="final_threshold_pending" if uncertain else
                    "selected_docx_explicit_cache_priority" if mechanism == "stateful_resource" and docx_explicit_priority else
                    "selected_resource_reuse_candidate" if mechanism == "stateful_resource" and resource_hint and mode == "auto" else
                    "selected_large_stable_attachment" if mechanism == "stateful_resource" and explicit_attachment_candidate and explicit_first_read_advantage and mode == "auto" else
                    "selected_high_frequency_reuse" if mechanism == "stateful_resource" and high_frequency_candidate and explicit_first_read_advantage and mode == "auto" else
                    "selected_implicit_history_default" if mechanism == "implicit_prefix" and mode == "auto" else
                    "selected_gemini_admission_override" if legacy_gemini_attempt and not verified else "selected",
                context_plan_hash=str(context_plan["context_plan_hash"]),
                policy_hash=policy_hash, profile_hash=profile_hash,
                cache_contract_hash=cache_contract_hash, scope=scope,
                allow_uncached_same_context=bool(mode == "auto" and policy.get("auto_prepare_failure") == "uncached_same_context"),
                final_threshold_guard=bool(cfg.get("final_threshold_guard")),
                candidate_rejections=tuple(rejections), mechanism_config=cfg,
                fallback_mechanisms=tuple(fallback_mechanisms),
                primary_projection=primary_projection,
                additional_bindings=tuple(additional_bindings),
                binding_composition=binding_composition,
                reuse_prediction=prediction,
                selection_evidence=selection_evidence,
                resolver_version=self.resolver_version,
            )

        # New explicitly supplied forced mechanism must fail closed. Legacy
        # on+omitted-mechanism preserves the documented 'below minimum -> None'
        # behavior of previously frozen v3 Sessions.
        reason = rejections[0]["reason"] if rejections else "mechanism_unavailable"
        if mode == "on" and mechanism_explicit:
            raise CacheDecisionError("CACHE_MECHANISM_INELIGIBLE", "Requested forced cache mechanism is ineligible", reason=reason)
        if mode == "on" and reason not in {"below_minimum", "context_assessment_uncertain"}:
            raise CacheDecisionError("CACHE_THRESHOLD_UNVERIFIED", "Forced cache eligibility cannot be proved", reason=reason)
        if (mode == "on" and reason == "context_assessment_uncertain"
            and str(provider or "").lower() == "gemini"
            and "implicit_prefix" in ordered):
            raise CacheDecisionError("CACHE_THRESHOLD_UNVERIFIED", "Gemini implicit-prefix threshold requires verified token assessment", reason=reason)
        return none(reason, rejections)

    def _none(
        self, mode: str, preference: str, context_plan: dict[str, Any],
        policy_hash: str | None, profile_hash: str | None,
        cache_contract_hash: str | None, scope: str, reason: str,
        reuse_prediction: dict[str, Any] | None = None,
        rejections: list[dict[str, Any]] | None = None,
    ) -> CacheIntentPlan:
        return CacheIntentPlan(
            requested_mode=mode, requested_mechanism=preference,
            planned_mechanism=None, resolution_status="finalized",
            decision_reason=reason, context_plan_hash=str(context_plan["context_plan_hash"]),
            policy_hash=policy_hash, profile_hash=profile_hash,
            cache_contract_hash=cache_contract_hash, scope=scope,
            reuse_prediction=dict(reuse_prediction or {}),
            candidate_rejections=tuple(rejections or ()),
            resolver_version=self.resolver_version,
        )
