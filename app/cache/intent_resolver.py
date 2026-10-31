from __future__ import annotations

from typing import Any

from .contracts import CacheDecisionError, CacheIntentPlan, provider_managed_trial_enabled
from .economics import (
    CACHE_READ_INPUT_RATIO,
    EXPLICIT_CREATE_INPUT_RATIO,
    estimate_future_reuses,
    estimate_ttl_bounded_future_reuses,
    evaluate_relative_cache_strategy,
)


class CacheIntentResolver:
    """Pure, replayable requested -> planned cache semantics.

    No network access, no native cache resources and no price guesses. A frozen
    plan selects one primary mechanism and may freeze an authorized pre-dispatch
    fallback. Runtime cannot invent a candidate or change mechanisms after the
    dispatch seal.
    """

    resolver_version = "relay-cache-resolver/6"

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
        supported_by_profile = {
            str(x) for x in (profile_cache.get("supported_mechanisms") or declared)
        }
        candidates = [m for m in declared if m in supported_by_profile]
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
        profiles = contract.get("mechanism_profiles") if isinstance(contract.get("mechanism_profiles"), dict) else {}
        # Old contracts keep resolver/6 identity; mechanism-level admission is
        # a new frozen contract, used only by newly published capabilities.
        resolver_version = (
            "relay-cache-resolver/7"
            if any(
                isinstance(cfg, dict) and (
                    "verification_status" in cfg
                    or cfg.get("threshold_mode") == "provider_managed_trial"
                )
                for cfg in profiles.values()
            )
            else self.resolver_version
        )
        protocol_mechanisms = profile_cache.get("mechanism_profiles") if isinstance(profile_cache.get("mechanism_profiles"), dict) else {}
        certified: set[str] = set()
        trials: set[str] = set()
        admitted: list[str] = []
        for name in candidates:
            cfg = profiles.get(name) if isinstance(profiles.get(name), dict) else {}
            wire_cfg = protocol_mechanisms.get(name) if isinstance(protocol_mechanisms.get(name), dict) else {}
            status = str(cfg.get("verification_status") or contract.get("verification_status") or "unverified").lower()
            wire_status = str(wire_cfg.get("verification_status") or "verified").lower()
            if (
                status in {"verified", "published", "legacy_verified"}
                and wire_status in {"verified", "published", "legacy_verified"}
            ):
                certified.add(name)
                admitted.append(name)
            elif (
                mode == "auto" and name == "implicit_prefix"
                and str(provider or "").lower() == "gemini"
                and (protocol_profile or {}).get("protocol") == "gemini_native"
                and provider_managed_trial_enabled(cfg)
                and provider_managed_trial_enabled(wire_cfg)
            ):
                trials.add(name)
                admitted.append(name)
            elif legacy_gemini_attempt and not verified and name == "stateful_resource":
                # Preserve only the pre-existing explicit-resource override.
                admitted.append(name)
        candidates = admitted
        frozen_forecast_policy = policy.get("metadata") if isinstance(policy.get("metadata"), dict) else {}
        economics = frozen_forecast_policy.get("auto_economics") if isinstance(frozen_forecast_policy.get("auto_economics"), dict) else {}
        use_stats = reuse_evidence if isinstance(reuse_evidence, dict) else {}
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
                              reuse_prediction=prediction, rejections=rejected, resolver_version=resolver_version)

        if not candidates:
            if mode == "on":
                raise CacheDecisionError("CACHE_UNSUPPORTED", "Frozen supply/profile has no supported cache mechanism")
            return none("mechanism_unavailable")
        if not verified and not legacy_gemini_attempt and not certified and not trials:
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
        resource_hint = use_stats.get("existing_resource_candidate") is True
        stable_large_attachment = attachment.get("large_stable_attachment_candidate") is True
        # Request-only attachment split is not semantically proven by Gemini
        # layout/9. It cannot justify choosing a billable explicit mechanism.
        explicit_attachment_candidate = (
            stable_large_attachment
            and bool(str(provider or "").lower() == "gemini")
        )
        stable_prefix_ids = context_plan.get("stable_prefix_segment_ids") or []
        minimum_observations = economics.get("minimum_observations", 3)
        if type(minimum_observations) is not int or minimum_observations < 2:
            minimum_observations = 3
        high_frequency_candidate = bool(
            observed >= minimum_observations
            or int(prediction.get("expected_future_reuses") or 0) > 0
        )
        explicit_assessment_candidate = bool(
            stable_prefix_ids and (explicit_attachment_candidate or high_frequency_candidate)
        )
        explicit_first_read_advantage = False
        explicit_cost_preview: dict[str, Any] = {}
        if (
            mode == "auto"
            and "stateful_resource" in candidates
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
                    "certified_implicit_alternative": "implicit_prefix" in certified,
                    "trial_implicit_alternative": "implicit_prefix" in trials,
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
                select_explicit = resource_hint or (
                    explicit_assessment_candidate and explicit_first_read_advantage
                )
                preferred = (
                    ["stateful_resource", "implicit_prefix", "breakpoint"] if select_explicit
                    else ["implicit_prefix", "stateful_resource", "breakpoint"]
                )
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
                if resource_hint or explicit_first_read_advantage
                else ["implicit_prefix", "stateful_resource"]
            )
        else:
            ordered = [preference]

        rejections: list[dict[str, Any]] = []
        assessment = context_plan.get("token_assessment") if isinstance(context_plan.get("token_assessment"), dict) else {}
        eligible: list[tuple[str, dict[str, Any], bool]] = []
        for mechanism in ordered:
            cfg = dict(profiles.get(mechanism) or {}) if isinstance(profiles.get(mechanism), dict) else {}
            gemini_implicit = str(provider or "").lower() == "gemini" and mechanism == "implicit_prefix"
            if mechanism in trials:
                cfg["admission_status"] = "provider_managed_trial"
            if mechanism == "stateful_resource" and legacy_gemini_attempt and not verified:
                cfg["system_admission_override"] = "gemini_model_cache_attempt"
                cfg["upstream_verification_status"] = str(contract.get("verification_status") or "unverified")
                if str(cfg.get("threshold_mode") or "unknown").lower() == "unknown":
                    cfg["threshold_mode"] = "provider_create"
                    cfg["threshold_admission_override"] = "provider_create"
            threshold = str(cfg.get("threshold_mode") or "unknown").lower()
            minimum = cfg.get("minimum_cacheable_tokens")
            below = not stable_prefix_ids
            uncertain = False
            if not below:
                if mechanism in trials:
                    # Provider decides whether this full-context request hits.
                    # No local token threshold or invented reuse count applies.
                    pass
                elif threshold in {"not_applicable", "provider_create", "provider_enforced"}:
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
                    "certified_implicit_alternative": "implicit_prefix" in certified,
                    "trial_implicit_alternative": "implicit_prefix" in trials,
                    "enable_history_growth": economics.get("enable_history_growth") is True,
                    "minimum_additional_tokens": economics.get("minimum_additional_tokens", 4096),
                    "new_economics_policy": "relative_cache/2" if str(provider or "").lower() == "gemini" else "legacy_forecast/1",
                    "attachment_evidence": dict(attachment),
                    "existing_resource_candidate": resource_hint,
                }

            fallback_mechanisms: list[dict[str, Any]] = []
            if (
                mode == "auto"
                and str(provider or "").lower() == "gemini"
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
                "selection_basis": "same_session_usage_ttl_and_frozen_context",
            }
            material_reuse = context_plan.get("material_reuse")
            if isinstance(material_reuse, dict):
                selection_evidence["material_reuse"] = dict(material_reuse)
                selection_evidence["material_reuse_is_provider_compatibility_proof"] = False
            return CacheIntentPlan(
                requested_mode=mode, requested_mechanism=preference,
                planned_mechanism=mechanism,
                resolution_status="pending" if uncertain else "finalized",
                decision_reason="selected_provider_managed_trial" if mechanism in trials else
                    "final_threshold_pending" if uncertain else
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
                reuse_prediction=prediction,
                selection_evidence=selection_evidence,
                resolver_version=resolver_version,
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
        resolver_version: str | None = None,
    ) -> CacheIntentPlan:
        return CacheIntentPlan(
            requested_mode=mode, requested_mechanism=preference,
            planned_mechanism=None, resolution_status="finalized",
            decision_reason=reason, context_plan_hash=str(context_plan["context_plan_hash"]),
            policy_hash=policy_hash, profile_hash=profile_hash,
            cache_contract_hash=cache_contract_hash, scope=scope,
            reuse_prediction=dict(reuse_prediction or {}),
            candidate_rejections=tuple(rejections or ()),
            resolver_version=resolver_version or self.resolver_version,
        )
