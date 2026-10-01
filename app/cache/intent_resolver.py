from __future__ import annotations

from typing import Any

from .contracts import CacheDecisionError, CacheIntentPlan


class CacheIntentResolver:
    """Pure resolver for requested -> planned cache semantics.

    It consumes only frozen configuration and context assessment facts. It never
    performs network I/O and never creates provider resources.
    """

    resolver_version = "relay-cache-resolver/1"

    def resolve(
        self,
        *,
        requested_mode: str,
        context_plan: dict[str, Any],
        cache_contract: dict[str, Any] | None,
        protocol_profile: dict[str, Any] | None,
        cache_policy: dict[str, Any] | None,
        cache_contract_hash: str | None,
        profile_hash: str | None,
        policy_hash: str | None,
    ) -> CacheIntentPlan:
        mode = str(requested_mode or "auto").strip().lower()
        if mode not in {"off", "auto", "on"}:
            raise CacheDecisionError("CACHE_MODE_INVALID", f"Unsupported cache mode: {requested_mode!r}")

        scope = str((cache_policy or {}).get("scope") or "session")
        if mode == "off":
            return CacheIntentPlan(
                requested_mode="off",
                planned_mechanism=None,
                resolution_status="finalized",
                decision_reason="requested_off",
                context_plan_hash=str(context_plan["context_plan_hash"]),
                policy_hash=policy_hash,
                profile_hash=profile_hash,
                cache_contract_hash=cache_contract_hash,
                scope=scope,
                resolver_version=self.resolver_version,
            )

        contract = cache_contract if isinstance(cache_contract, dict) else {}
        raw_mechanisms = contract.get("supported_mechanisms") or contract.get("mechanisms") or []
        mechanisms = [str(x) for x in raw_mechanisms if str(x)]
        verification = str(contract.get("verification_status") or "unverified").lower()
        if not mechanisms:
            if mode == "on":
                raise CacheDecisionError(
                    "CACHE_UNSUPPORTED",
                    "The frozen Model Supply has no certified cache mechanism",
                    reason="no_mechanism",
                )
            return self._none(mode, context_plan, policy_hash, profile_hash, cache_contract_hash, scope, "mechanism_unavailable")

        if verification not in {"verified", "published", "legacy_verified"}:
            if mode == "on":
                raise CacheDecisionError(
                    "CACHE_PROTOCOL_UNVERIFIED",
                    "The frozen Model Supply cache mechanism is not certified for this channel/profile",
                    reason=verification or "unverified",
                )
            return self._none(mode, context_plan, policy_hash, profile_hash, cache_contract_hash, scope, "protocol_unverified")

        profile_cache = (protocol_profile or {}).get("cache") if isinstance(protocol_profile, dict) else {}
        profile_cache = profile_cache if isinstance(profile_cache, dict) else {}
        supported_by_profile = {
            str(x) for x in (profile_cache.get("supported_mechanisms") or mechanisms)
        }
        candidates = [x for x in mechanisms if x in supported_by_profile]
        if not candidates:
            if mode == "on":
                raise CacheDecisionError(
                    "CACHE_PROTOCOL_UNVERIFIED",
                    "The frozen protocol profile cannot project any certified cache mechanism",
                    reason="profile_mechanism_mismatch",
                )
            return self._none(mode, context_plan, policy_hash, profile_hash, cache_contract_hash, scope, "profile_mechanism_mismatch")

        policy = cache_policy if isinstance(cache_policy, dict) else {}
        if bool(policy.get("deny", False)):
            if mode == "on":
                raise CacheDecisionError("CACHE_POLICY_DENIED", "Cache use is denied by the frozen policy")
            return self._none(mode, context_plan, policy_hash, profile_hash, cache_contract_hash, scope, "policy_denied")

        preference = [str(x) for x in (policy.get("mechanism_preference") or candidates)]
        ordered = [x for x in preference if x in candidates] + [x for x in candidates if x not in preference]
        mechanism = ordered[0]
        mechanism_cfg = contract.get("mechanism_profiles", {}).get(mechanism, {}) if isinstance(contract.get("mechanism_profiles"), dict) else {}
        mechanism_cfg = dict(mechanism_cfg or {})

        assessment = context_plan.get("token_assessment") if isinstance(context_plan.get("token_assessment"), dict) else {}
        threshold_mode = str(mechanism_cfg.get("threshold_mode") or "unknown").lower()
        minimum = mechanism_cfg.get("minimum_cacheable_tokens")
        below = False
        uncertain = False
        if threshold_mode == "not_applicable":
            pass
        elif isinstance(minimum, int) and minimum >= 0:
            lower = assessment.get("lower")
            upper = assessment.get("upper")
            if isinstance(lower, int) and lower >= minimum:
                pass
            elif isinstance(upper, int) and upper < minimum:
                below = True
            else:
                uncertain = True
        else:
            # Missing/unknown thresholds are not equivalent to zero.
            uncertain = True

        stable_prefix_ids = context_plan.get("stable_prefix_segment_ids") or []
        if not stable_prefix_ids:
            below = True
            uncertain = False

        if below:
            return self._none(mode, context_plan, policy_hash, profile_hash, cache_contract_hash, scope, "below_minimum")
        if uncertain and not bool(mechanism_cfg.get("final_threshold_guard", False)):
            return self._none(mode, context_plan, policy_hash, profile_hash, cache_contract_hash, scope, "context_assessment_uncertain")

        if mode == "auto" and bool(policy.get("auto_disabled", False)):
            return self._none(mode, context_plan, policy_hash, profile_hash, cache_contract_hash, scope, "auto_policy_skip")

        pending_final_guard = bool(uncertain and mechanism_cfg.get("final_threshold_guard", False))
        return CacheIntentPlan(
            requested_mode=mode,  # type: ignore[arg-type]
            planned_mechanism=mechanism,  # type: ignore[arg-type]
            resolution_status=("pending" if pending_final_guard else "finalized"),
            decision_reason=("final_threshold_pending" if pending_final_guard else "selected"),
            context_plan_hash=str(context_plan["context_plan_hash"]),
            policy_hash=policy_hash,
            profile_hash=profile_hash,
            cache_contract_hash=cache_contract_hash,
            scope=scope,
            allow_uncached_same_context=bool(
                policy.get("auto_prepare_failure") == "uncached_same_context" and mode == "auto"
            ),
            final_threshold_guard=bool(mechanism_cfg.get("final_threshold_guard", False)),
            candidate_rejections=(),
            mechanism_config=mechanism_cfg,
            resolver_version=self.resolver_version,
        )

    def _none(
        self,
        mode: str,
        context_plan: dict[str, Any],
        policy_hash: str | None,
        profile_hash: str | None,
        cache_contract_hash: str | None,
        scope: str,
        reason: str,
    ) -> CacheIntentPlan:
        return CacheIntentPlan(
            requested_mode=mode,  # type: ignore[arg-type]
            planned_mechanism=None,
            resolution_status="finalized",
            decision_reason=reason,
            context_plan_hash=str(context_plan["context_plan_hash"]),
            policy_hash=policy_hash,
            profile_hash=profile_hash,
            cache_contract_hash=cache_contract_hash,
            scope=scope,
            resolver_version=self.resolver_version,
        )
