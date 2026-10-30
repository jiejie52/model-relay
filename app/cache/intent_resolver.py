from __future__ import annotations

from typing import Any

from .contracts import CacheDecisionError, CacheIntentPlan
from .economics import estimate_future_reuses, evaluate_current_read_resource


class CacheIntentResolver:
    """Pure, replayable requested -> planned cache semantics.

    No network access, no native cache resources and no price guesses. A frozen
    plan selects ONE mechanism. Runtime may only prepare that mechanism or use
    the authorized complete uncached context before dispatch.
    """

    resolver_version = "relay-cache-resolver/5"

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
        frozen_forecast_policy = policy.get("metadata") if isinstance(policy.get("metadata"), dict) else {}
        economics = frozen_forecast_policy.get("auto_economics") if isinstance(frozen_forecast_policy.get("auto_economics"), dict) else {}
        use_stats = reuse_evidence if isinstance(reuse_evidence, dict) else {}
        observed = use_stats.get("observed_compatible_uses", 0)
        if type(observed) is not int or observed < 0:
            observed = 0
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
        profiles = contract.get("mechanism_profiles") if isinstance(contract.get("mechanism_profiles"), dict) else {}
        stateful_profile = profiles.get("stateful_resource") if isinstance(profiles.get("stateful_resource"), dict) else {}
        ttl = stateful_profile.get("ttl_seconds")
        ttl = ttl if type(ttl) is int and ttl > 0 else 3600
        attachment = attachment_evidence if isinstance(attachment_evidence, dict) else {}
        resource_hint = use_stats.get("existing_resource_candidate") is True
        stable_large_attachment = attachment.get("large_stable_attachment_candidate") is True
        # Request-only attachment split is not semantically proven by Gemini
        # layout/9. It cannot justify choosing a billable explicit mechanism.
        explicit_attachment_candidate = (
            stable_large_attachment
            and bool(str(provider or "").lower() == "gemini")
        )
        explicit_first_read_advantage = False
        if (mode == "auto" and explicit_attachment_candidate
            and "stateful_resource" in candidates):
            # Prices are linear in tokens; this comparison selects a candidate,
            # NOT permission to create. The true prefix count and hard budget
            # are checked at the execution-phase create gate.
            preview = evaluate_current_read_resource(
                tokens=1_000_000, ttl_seconds=ttl,
                config={
                    "price_schedule": economics.get("price_schedule") or {},
                    "reuse_prediction": prediction,
                    "certified_implicit_alternative": "implicit_prefix" in candidates and verified,
                    "estimated_implicit_hit_rate": economics.get("estimated_implicit_hit_rate"),
                    "safety_margin_ratio": economics.get("safety_margin_ratio", .20),
                    "no_separate_creation_fee_verified": economics.get("no_separate_creation_fee_verified") is True,
                    # Resource size/cap is unknown at admission.
                },
                forced=False,
            )
            explicit_first_read_advantage = preview.get("allow") is True

        if preference == "auto":
            if mode == "auto" and str(provider or "").lower() == "gemini":
                select_explicit = resource_hint or (explicit_attachment_candidate and explicit_first_read_advantage)
                preferred = (
                    ["stateful_resource", "implicit_prefix", "breakpoint"] if select_explicit
                    else ["implicit_prefix", "stateful_resource", "breakpoint"]
                )
            else:
                preferred = [str(x) for x in (policy.get("mechanism_preference") or candidates)]
            ordered = [x for x in preferred if x in candidates] + [x for x in candidates if x not in preferred]
        elif (mode == "auto" and preference == "stateful_resource" and
              "implicit_prefix" in candidates and not (resource_hint or explicit_first_read_advantage)):
            # A soft explicit preference cannot overrule the cost gate or the
            # attachment/history distinction. A forced `on` remains binding.
            ordered = ["implicit_prefix", "stateful_resource"]
        else:
            ordered = [preference]

        rejections: list[dict[str, Any]] = []
        assessment = context_plan.get("token_assessment") if isinstance(context_plan.get("token_assessment"), dict) else {}
        stable_prefix_ids = context_plan.get("stable_prefix_segment_ids") or []
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

            if mechanism == "stateful_resource":
                cfg["auto_economics"] = {
                    "price_schedule": dict(economics.get("price_schedule") or {}) if isinstance(economics.get("price_schedule"), dict) else {},
                    "reuse_prediction": prediction,
                    "max_new_resource_cost": economics.get("max_new_resource_cost"),
                    "safety_margin_ratio": economics.get("safety_margin_ratio", .20),
                    "estimated_implicit_hit_rate": economics.get("estimated_implicit_hit_rate"),
                    "certified_implicit_alternative": bool("implicit_prefix" in candidates and verified),
                    "enable_history_growth": economics.get("enable_history_growth") is True,
                    "minimum_additional_tokens": economics.get("minimum_additional_tokens", 4096),
                    "no_separate_creation_fee_verified": economics.get("no_separate_creation_fee_verified") is True,
                    "new_economics_policy": (
                        "current_read/1" if str(provider or "").lower() == "gemini"
                        else "legacy_forecast/1"
                    ),
                    "attachment_evidence": dict(attachment),
                    "existing_resource_candidate": resource_hint,
                }
            return CacheIntentPlan(
                requested_mode=mode, requested_mechanism=preference,
                planned_mechanism=mechanism,
                resolution_status="pending" if uncertain else "finalized",
                decision_reason="final_threshold_pending" if uncertain else
                    "selected_resource_reuse_candidate" if mechanism == "stateful_resource" and resource_hint and mode == "auto" else
                    "selected_large_stable_attachment" if mechanism == "stateful_resource" and explicit_attachment_candidate and mode == "auto" else
                    "selected_implicit_history_default" if mechanism == "implicit_prefix" and mode == "auto" else
                    "selected_gemini_admission_override" if legacy_gemini_attempt and not verified else "selected",
                context_plan_hash=str(context_plan["context_plan_hash"]),
                policy_hash=policy_hash, profile_hash=profile_hash,
                cache_contract_hash=cache_contract_hash, scope=scope,
                allow_uncached_same_context=bool(mode == "auto" and policy.get("auto_prepare_failure") == "uncached_same_context"),
                final_threshold_guard=bool(cfg.get("final_threshold_guard")),
                candidate_rejections=tuple(rejections), mechanism_config=cfg,
                reuse_prediction=prediction,
                selection_evidence={
                    "existing_resource_candidate": resource_hint,
                    "resource_hint_is_compatibility_proof": False,
                    "stable_attachment_bytes": attachment.get("stable_attachment_bytes", 0),
                    "large_stable_attachment_candidate": stable_large_attachment,
                    "attachment_current_read_advantage": explicit_first_read_advantage,
                    "selection_basis": "frozen_inventory_and_canonical_context",
                },
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
