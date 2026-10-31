"""Pure, conservative, frozen-fact cache economics for Request planning.

This module never reads Provider state or predicts business workflow stages.
New CachedContent creation is an optional, billable side effect; an unknown
price or reuse forecast cannot silently become a positive business case.
"""
from __future__ import annotations

import math
from typing import Any

from ..core.idempotency import stable_hash


CACHE_READ_INPUT_RATIO = 0.1
EXPLICIT_CREATE_INPUT_RATIO = 1.0


def reuse_evidence_key(snapshot: dict[str, Any], context_plan: dict[str, Any]) -> str:
    """Same-Session comparable static context; deliberately excludes history growth.

    Includes Request instruction semantics, ordered Material fingerprints and
    frozen Supply/Profile, never similarity or merely a common Material ID.
    """
    segments = context_plan.get("segments") or []
    static = [
        {
            "domain": x.get("domain"),
            "role": x.get("role"),
            "hash": x.get("canonical_content_hash"),
            "representation": x.get("material_representation_digest"),
        }
        for x in segments
        if isinstance(x, dict) and x.get("domain") in {"instructions", "material"}
    ]
    return stable_hash({
        "session_id": snapshot.get("session_id"),
        "provider": snapshot.get("provider"),
        "model": snapshot.get("model"),
        "offering_id": snapshot.get("offering_id"),
        "protocol_profile_hash": snapshot.get("protocol_profile_hash"),
        "static": static,
    })


def estimate_future_reuses(
    *, policy: dict[str, Any], observed_compatible_uses: int
) -> dict[str, Any]:
    """Use only frozen administrative expectations, or an expressly enabled
    conservative history heuristic. Past use alone never proves future work.
    """
    value = policy.get("expected_future_reuses")
    if type(value) is int and 0 <= value <= 10000:
        return {
            "expected_future_reuses": value,
            "source": "frozen_cache_policy",
            "observed_compatible_uses": max(0, observed_compatible_uses),
        }
    history_enabled = policy.get("allow_history_forecast") is True
    minimum = policy.get("minimum_observations", 3)
    limit = policy.get("max_history_forecast", 2)
    if (
        history_enabled
        and type(minimum) is int and minimum >= 3
        and type(limit) is int and 0 <= limit <= 20
        and observed_compatible_uses >= minimum
    ):
        return {
            "expected_future_reuses": min(limit, (observed_compatible_uses - 1) // 2),
            "source": "frozen_policy_opt_in_history_heuristic",
            "observed_compatible_uses": observed_compatible_uses,
        }
    return {
        "expected_future_reuses": 0,
        "source": "unknown_future_demand",
        "observed_compatible_uses": max(0, observed_compatible_uses),
    }


def estimate_ttl_bounded_future_reuses(
    *, policy: dict[str, Any], evidence: dict[str, Any], ttl_seconds: int,
) -> dict[str, Any]:
    """Estimate only the additional uses that can plausibly occur inside TTL.

    An explicit positive forecast remains an operator-owned override. Otherwise
    a conservative cadence estimate is derived from recent successful requests
    with the exact same Session reuse-evidence key. A stale or timestamp-less
    sample never becomes a future-use forecast.
    """
    policy = policy if isinstance(policy, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    ttl = ttl_seconds if type(ttl_seconds) is int and ttl_seconds > 0 else 3600
    observed = evidence.get("observed_compatible_uses", 0)
    observed = observed if type(observed) is int and observed >= 0 else 0
    explicit = policy.get("expected_future_reuses")
    if type(explicit) is int and explicit > 0:
        return {
            "expected_future_reuses": min(explicit, 10000),
            "source": "frozen_cache_policy_override",
            "observed_compatible_uses": observed,
            "ttl_seconds": ttl,
        }

    if policy.get("enable_observed_ttl_forecast", True) is False:
        return {
            "expected_future_reuses": 0,
            "source": "observed_ttl_forecast_disabled",
            "observed_compatible_uses": observed,
            "ttl_seconds": ttl,
        }

    minimum = policy.get("minimum_observations", 3)
    maximum = policy.get("max_history_forecast", 8)
    confidence = _nonnegative(policy.get("history_forecast_confidence", 0.5))
    window = _nonnegative(evidence.get("observed_window_seconds"))
    age = _nonnegative(evidence.get("observed_age_seconds"))
    if type(minimum) is not int or minimum < 2:
        minimum = 3
    if type(maximum) is not int or maximum < 0:
        maximum = 8
    if confidence is None:
        confidence = 0.5
    confidence = min(1.0, confidence)

    if (
        observed < minimum
        or window is None or window <= 0
        or age is None or age > ttl
        or maximum == 0 or confidence == 0
    ):
        return {
            "expected_future_reuses": 0,
            "source": "insufficient_recent_same_session_evidence",
            "observed_compatible_uses": observed,
            "observed_window_seconds": window,
            "observed_age_seconds": age,
            "ttl_seconds": ttl,
        }

    requests_per_second = max(0.0, (observed - 1) / window)
    forecast = int(requests_per_second * ttl * confidence)
    forecast = min(maximum, max(0, forecast))
    return {
        "expected_future_reuses": forecast,
        "source": "same_session_observed_cadence_within_ttl",
        "observed_compatible_uses": observed,
        "observed_window_seconds": round(window, 3),
        "observed_age_seconds": round(age, 3),
        "observed_requests_per_hour": round(requests_per_second * 3600, 4),
        "forecast_confidence": confidence,
        "ttl_seconds": ttl,
    }


def _nonnegative(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    return amount


def evaluate_new_resource(
    *, tokens: int, ttl_seconds: int, config: dict[str, Any],
    forced: bool,
) -> dict[str, Any]:
    """Compare *current+predicted* explicit reads against implicit/uncached.

    Policy prices are per million tokens and storage per million-token-minute.
    A hard creation cost cap is honored even for forced explicit requests.
    No guessed prices or implicit hits may authorize automatic creation.
    """
    if type(tokens) is not int or tokens <= 0 or type(ttl_seconds) is not int or ttl_seconds <= 0:
        return {"allow": False, "reason": "resource_measurement_invalid"}
    prices = config.get("price_schedule") if isinstance(config.get("price_schedule"), dict) else {}
    currency = str(prices.get("currency") or "").strip().upper()
    revision = str(prices.get("price_revision") or "").strip()
    input_rate = _nonnegative(prices.get("input_cost_per_million_tokens"))
    read_rate = _nonnegative(prices.get("cache_read_cost_per_million_tokens"))
    write_rate = _nonnegative(prices.get("cache_write_cost_per_million_tokens"))
    storage_rate = _nonnegative(prices.get("storage_cost_per_million_tokens_minute"))
    price_complete = (
        bool(currency and revision)
        and all(v is not None for v in (input_rate, read_rate, write_rate, storage_rate))
        and input_rate > read_rate
    )
    hard_cap = _nonnegative(config.get("max_new_resource_cost"))
    if not price_complete:
        return {
            "allow": bool(forced and hard_cap is None),
            "reason": "explicit_requested_without_economic_estimate" if forced and hard_cap is None
                      else "cost_schedule_unavailable",
            "cost_status": "unknown",
        }

    creation = tokens * write_rate / 1_000_000
    storage = tokens * (ttl_seconds / 60) * storage_rate / 1_000_000
    overhead = creation + storage
    base = {
        "currency": currency, "price_revision": revision,
        "prefix_token_count": tokens, "ttl_seconds": ttl_seconds,
        "estimated_creation_cost": round(creation, 10),
        "estimated_storage_cost": round(storage, 10),
        "estimated_new_resource_cost": round(overhead, 10),
        "cost_status": "estimated_from_frozen_prices",
    }
    if hard_cap is not None and overhead > hard_cap:
        return {**base, "allow": False, "reason": "new_resource_budget_exceeded", "hard_cost_cap": hard_cap}
    if forced:
        return {**base, "allow": True, "reason": "caller_forced_explicit_within_policy_budget"}

    prediction = config.get("reuse_prediction") if isinstance(config.get("reuse_prediction"), dict) else {}
    future = prediction.get("expected_future_reuses")
    if type(future) is not int or future < 0:
        future = 0
    future = min(future, 10000)
    # Missing data is treated as a fully effective implicit alternative when
    # an implicit mechanism is independently certified, not as a free miss.
    alternative_implicit = config.get("certified_implicit_alternative") is True
    if alternative_implicit:
        hit_rate = _nonnegative(config.get("estimated_implicit_hit_rate"))
        if hit_rate is None or hit_rate > 1:
            hit_rate = 1.0
        alternative_rate = input_rate - hit_rate * (input_rate - read_rate)
    else:
        alternative_rate = input_rate
    savings_per_read = tokens * max(0, alternative_rate - read_rate) / 1_000_000
    expected_savings = (1 + future) * savings_per_read
    margin = _nonnegative(config.get("safety_margin_ratio"))
    if margin is None:
        margin = .20
    margin = min(margin, 5.0)
    net = expected_savings - overhead * (1 + margin)
    return {
        **base,
        "allow": net > 0,
        "reason": "expected_net_savings_positive" if net > 0 else "insufficient_economic_evidence",
        "expected_future_reuses": future,
        "reuse_evidence_source": prediction.get("source") or "unknown",
        "estimated_implicit_hit_rate": hit_rate if alternative_implicit else None,
        "expected_total_reads": 1 + future,
        "estimated_savings_before_new_resource_cost": round(expected_savings, 10),
        "estimated_net_savings_after_margin": round(net, 10),
    }


def stable_attachment_evidence(
    *, context_plan: dict[str, Any], material_rows: list[dict[str, Any]],
    large_min_bytes: int = 1024 * 1024,
    context_policy: str = "conversation",
) -> dict[str, Any]:
    """Freeze a *candidate* signal; bytes are never mistaken for Gemini tokens.

    Only explicitly present, content-addressed, Session-stable attachments can
    bias automatic explicit-resource selection. Request-only materials remain
    eligible for safe projection assessment but cannot authorize an unsafe
    Gemini user-Content split. Exact qualification is checked by the projector.
    """
    threshold = large_min_bytes if type(large_min_bytes) is int and large_min_bytes > 0 else 1024 * 1024
    stable = {
        str(segment.get("source_ref") or "")
        for segment in (context_plan.get("segments") or [])
        if context_policy == "conversation"
        and isinstance(segment, dict)
        and segment.get("domain") == "material"
        and segment.get("stability") == "session_stable"
        and bool(segment.get("canonical_content_hash"))
    }
    sizes: list[int] = []
    for row in material_rows:
        if not isinstance(row, dict) or str(row.get("id") or "") not in stable:
            continue
        raw_size = row.get("actual_size")
        if raw_size is None:
            raw_size = row.get("size_bytes")
        try:
            size = int(raw_size)
        except (TypeError, ValueError, OverflowError):
            continue
        if size > 0:
            sizes.append(size)
    return {
        "stable_attachment_count": len(sizes),
        "stable_attachment_bytes": sum(sizes),
        "largest_stable_attachment_bytes": max(sizes, default=0),
        "large_attachment_min_bytes": threshold,
        "large_stable_attachment_candidate": sum(sizes) >= threshold,
        "assessment": "canonical_bytes_only_not_provider_tokens",
    }


def evaluate_current_read_resource(
    *, tokens: int, ttl_seconds: int, config: dict[str, Any],
    forced: bool, already_cached_tokens: int = 0,
) -> dict[str, Any]:
    """Version 6.1 economic gate: real *current* read first, future only bonus.

    Provider creation/write fees are separate from TTL storage. Storage does
    not enter the optimization objective, by policy, but is included in a
    configured hard resource-spend limit. An unknown independent fee or an
    unknown implicit hit rate is never silently assumed to be zero.

    `already_cached_tokens` supports the marginal case for replacing a valid
    immutable resource with a longer snapshot: only the new coverage can save
    this Request's input fees, while creation charges cover the whole snapshot.
    """
    if (type(tokens) is not int or tokens <= 0 or type(ttl_seconds) is not int
        or ttl_seconds <= 0 or type(already_cached_tokens) is not int
        or already_cached_tokens < 0 or already_cached_tokens > tokens):
        return {"allow": False, "reason": "resource_measurement_invalid"}
    prices = config.get("price_schedule") if isinstance(config.get("price_schedule"), dict) else {}
    currency = str(prices.get("currency") or "").strip().upper()
    revision = str(prices.get("price_revision") or "").strip()
    input_rate = _nonnegative(prices.get("input_cost_per_million_tokens"))
    read_rate = _nonnegative(prices.get("cache_read_cost_per_million_tokens"))
    create_rate = _nonnegative(prices.get("cache_write_cost_per_million_tokens"))
    create_source = "contract_price"
    if create_rate is None and config.get("no_separate_creation_fee_verified") is True:
        create_rate = 0.0
        create_source = "frozen_contract_confirmed_no_fee"
    storage_rate = _nonnegative(prices.get("storage_cost_per_million_tokens_minute"))
    cap = _nonnegative(config.get("max_new_resource_cost"))
    complete = (
        bool(currency and revision)
        and input_rate is not None and read_rate is not None
        and input_rate > read_rate and create_rate is not None
    )
    if not complete:
        # Caller intent cannot bypass a hard cap when its cost cannot be proved.
        return {
            "allow": bool(forced and cap is None),
            "reason": (
                "caller_forced_without_cost_estimate" if forced and cap is None
                else "current_read_price_or_creation_fee_unverified"
            ),
            "cost_status": "unknown",
            "optimization_excludes_storage": True,
        }
    creation_cost = tokens * create_rate / 1_000_000
    storage_cost = (
        tokens * ttl_seconds / 60 * storage_rate / 1_000_000
        if storage_rate is not None else None
    )
    base = {
        "currency": currency, "price_revision": revision,
        "prefix_token_count": tokens,
        "already_cached_tokens": already_cached_tokens,
        "newly_cacheable_tokens": tokens - already_cached_tokens,
        "ttl_seconds": ttl_seconds,
        "estimated_creation_cost": round(creation_cost, 10),
        "creation_fee_source": create_source,
        "estimated_storage_cost_observation_only": (
            round(storage_cost, 10) if storage_cost is not None else None
        ),
        "optimization_excludes_storage": True,
        "cost_status": "estimated_from_frozen_prices",
    }
    if cap is not None:
        if storage_cost is None:
            return {**base, "allow": False, "reason": "hard_budget_storage_cost_unverified"}
        if creation_cost + storage_cost > cap:
            return {
                **base, "allow": False, "reason": "new_resource_budget_exceeded",
                "hard_cost_cap": cap,
            }
    if forced:
        return {**base, "allow": True, "reason": "caller_forced_explicit_within_policy_budget"}

    certified_implicit = config.get("certified_implicit_alternative") is True
    hit_rate = _nonnegative(config.get("estimated_implicit_hit_rate"))
    if certified_implicit and (hit_rate is None or hit_rate > 1):
        return {
            **base, "allow": False,
            "reason": "implicit_hit_rate_unverified",
            "cost_status": "implicit_comparison_unavailable",
        }
    alternative_rate = (
        input_rate - hit_rate * (input_rate - read_rate)
        if certified_implicit else input_rate
    )
    effective_tokens = tokens - already_cached_tokens
    saved_per_read = effective_tokens * max(0.0, alternative_rate - read_rate) / 1_000_000
    prediction = config.get("reuse_prediction") if isinstance(config.get("reuse_prediction"), dict) else {}
    future = prediction.get("expected_future_reuses")
    future = min(future, 10000) if type(future) is int and future > 0 else 0
    # No forecast is needed to admit a resource when the first read is already
    # profitable; future reuse can only increase its expected benefit.
    margin = _nonnegative(config.get("safety_margin_ratio"))
    margin = min(5.0, margin) if margin is not None else .2
    current_net = saved_per_read - creation_cost * (1 + margin)
    total_net = saved_per_read * (1 + future) - creation_cost * (1 + margin)
    return {
        **base,
        "allow": total_net > 0,
        "reason": (
            "current_read_savings_positive" if current_net > 0 else
            "future_bonus_makes_net_positive" if total_net > 0 else
            "current_read_and_future_bonus_insufficient"
        ),
        "estimated_implicit_hit_rate": hit_rate if certified_implicit else None,
        "expected_future_reuses": future,
        "reuse_evidence_source": prediction.get("source") or "unknown",
        "estimated_current_read_savings": round(saved_per_read, 10),
        "estimated_current_net_after_margin": round(current_net, 10),
        "estimated_future_savings_bonus": round(saved_per_read * future, 10),
        "estimated_net_savings_after_margin": round(total_net, 10),
    }


def evaluate_relative_cache_strategy(
    *, tokens: int, ttl_seconds: int, config: dict[str, Any],
    forced: bool, already_cached_tokens: int = 0,
    enforce_hard_budget: bool = True,
) -> dict[str, Any]:
    """Compare explicit and implicit caching using input-equivalent units.

    The approved 6.2 policy is intentionally relative rather than tied to a
    mutable vendor price sheet:
      * ordinary input / implicit miss = 1.0 input-price unit;
      * Cache Read / implicit hit = 0.1 input-price unit;
      * explicit resource creation = 1.0 input-price unit.

    Storage is excluded from the optimization objective. If an operator sets a
    hard monetary cap, however, the cap is still checked using the frozen price
    schedule and storage evidence. A certified implicit alternative with no
    measured hit rate is preferred over speculative explicit creation.
    """
    if (
        type(tokens) is not int or tokens <= 0
        or type(ttl_seconds) is not int or ttl_seconds <= 0
        or type(already_cached_tokens) is not int
        or already_cached_tokens < 0 or already_cached_tokens > tokens
    ):
        return {"allow": False, "reason": "resource_measurement_invalid"}

    configured_read_ratio = _nonnegative(config.get("cache_read_input_ratio", CACHE_READ_INPUT_RATIO))
    configured_create_ratio = _nonnegative(config.get("explicit_create_input_ratio", EXPLICIT_CREATE_INPUT_RATIO))
    margin = _nonnegative(config.get("safety_margin_ratio", 0.2))
    if (
        configured_read_ratio is None
        or not math.isclose(configured_read_ratio, CACHE_READ_INPUT_RATIO, rel_tol=0, abs_tol=1e-12)
        or configured_create_ratio is None
        or not math.isclose(configured_create_ratio, EXPLICIT_CREATE_INPUT_RATIO, rel_tol=0, abs_tol=1e-12)
        or margin is None
    ):
        return {"allow": False, "reason": "relative_price_policy_invalid"}
    read_ratio = CACHE_READ_INPUT_RATIO
    create_ratio = EXPLICIT_CREATE_INPUT_RATIO
    margin = min(margin, 5.0)

    implicit_certified = config.get("certified_implicit_alternative") is True
    implicit_trial = config.get("trial_implicit_alternative") is True
    implicit_available = implicit_certified or implicit_trial
    observed_hit_rate = _nonnegative(config.get("observed_implicit_hit_rate"))
    if implicit_available and (observed_hit_rate is None or observed_hit_rate > 1) and not forced:
        return {
            "allow": False,
            "reason": "implicit_hit_rate_unverified",
            "pricing_model": "input_equivalent/1",
            "cache_read_input_ratio": read_ratio,
            "explicit_create_input_ratio": create_ratio,
            "ttl_seconds": ttl_seconds,
            "expected_future_reuses": max(0, int(config.get("expected_future_reuses") or 0)),
            "certified_implicit_alternative": implicit_certified,
            "trial_implicit_alternative": implicit_trial,
            "observed_implicit_hit_rate": None,
            "observed_implicit_hit_rate_source": "no_verified_same_session_implicit_usage",
        }
    if observed_hit_rate is None or observed_hit_rate > 1:
        observed_hit_rate = 0.0
        hit_source = "uncached_input_baseline"
    else:
        hit_source = str(config.get("observed_implicit_hit_rate_source") or "same_session_provider_usage")
    implicit_rate = (
        1.0 - observed_hit_rate * (1.0 - read_ratio)
        if implicit_available else 1.0
    )

    future = config.get("expected_future_reuses", 0)
    if type(future) is not int or future < 0:
        future = 0
    future = min(future, 10000)
    expected_reads = 1 + future
    newly_cacheable = tokens - already_cached_tokens
    savings_per_read = newly_cacheable * max(0.0, implicit_rate - read_ratio)
    expected_savings = savings_per_read * expected_reads
    creation_cost = tokens * create_ratio
    net = expected_savings - creation_cost * (1.0 + margin)

    base: dict[str, Any] = {
        "pricing_model": "input_equivalent/1",
        "cache_read_input_ratio": read_ratio,
        "explicit_create_input_ratio": create_ratio,
        "prefix_token_count": tokens,
        "already_cached_tokens": already_cached_tokens,
        "newly_cacheable_tokens": newly_cacheable,
        "ttl_seconds": ttl_seconds,
        "optimization_excludes_storage": True,
        "certified_implicit_alternative": implicit_certified,
        "trial_implicit_alternative": implicit_trial,
        "observed_implicit_hit_rate": observed_hit_rate if implicit_available else None,
        "observed_implicit_hit_rate_source": hit_source if implicit_available else "not_applicable",
        "implicit_expected_input_rate": round(implicit_rate, 8),
        "expected_future_reuses": future,
        "expected_total_reads": expected_reads,
        "estimated_creation_cost_input_equivalents": round(creation_cost / 1_000_000, 10),
        "estimated_savings_before_creation_cost_input_equivalents": round(expected_savings / 1_000_000, 10),
        "estimated_net_savings_input_equivalents": round(net / 1_000_000, 10),
        "safety_margin_ratio": margin,
    }

    hard_cap = _nonnegative(config.get("max_new_resource_cost"))
    if hard_cap is not None and enforce_hard_budget:
        prices = config.get("price_schedule") if isinstance(config.get("price_schedule"), dict) else {}
        currency = str(prices.get("currency") or "").strip().upper()
        revision = str(prices.get("price_revision") or "").strip()
        input_rate = _nonnegative(prices.get("input_cost_per_million_tokens"))
        storage_rate = _nonnegative(prices.get("storage_cost_per_million_tokens_minute"))
        if not currency or not revision or input_rate is None or storage_rate is None:
            return {
                **base,
                "allow": False,
                "reason": "hard_budget_price_or_storage_evidence_unverified",
                "cost_status": "unknown",
                "hard_cost_cap": hard_cap,
            }
        creation_money = tokens * input_rate * create_ratio / 1_000_000
        storage_money = tokens * (ttl_seconds / 60) * storage_rate / 1_000_000
        if creation_money + storage_money > hard_cap:
            return {
                **base,
                "allow": False,
                "reason": "new_resource_budget_exceeded",
                "currency": currency,
                "price_revision": revision,
                "estimated_creation_cost": round(creation_money, 10),
                "estimated_storage_cost_observation_only": round(storage_money, 10),
                "hard_cost_cap": hard_cap,
            }
        base.update({
            "currency": currency,
            "price_revision": revision,
            "estimated_creation_cost": round(creation_money, 10),
            "estimated_storage_cost_observation_only": round(storage_money, 10),
            "hard_cost_cap": hard_cap,
        })

    if forced:
        return {**base, "allow": True, "reason": "caller_forced_explicit_within_policy_budget"}
    return {
        **base,
        "allow": net > 0,
        "reason": "explicit_expected_cost_lower_than_implicit" if net > 0 else "explicit_not_economical_vs_implicit",
    }
