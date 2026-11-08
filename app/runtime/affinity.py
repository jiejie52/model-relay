"""Session affinity evidence and fenced pre-dispatch rotation."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.shared_contracts import CacheDecisionError
from app.core.idempotency import canonical_json_bytes, stable_hash


class AffinityDriftDetected(RuntimeError):
    """A previously unexpired explicit handle became inaccessible."""


def session_affinity_token(settings: Any, session: dict[str, Any], epoch: int) -> str:
    route = (session.get("metadata") or {}).get("_relay_route") or {}
    if session.get("sticky_scope") == "conversation_family" and session.get("system_semantic_hash"):
        identity = [
            str(session["tenant_id"]), "conversation_family", str(session["conversation_hash"]),
            route.get("offering_id") or session["connection_id"], route.get("account_scope_hash"),
            route.get("protocol_profile_hash"), session["system_semantic_hash"], epoch,
        ]
    else:
        identity = [str(session[key]) for key in ("tenant_id", "id", "connection_id")] + [epoch]
    secret = settings.relay_api_token.get_secret_value().encode()
    return hmac.new(secret, canonical_json_bytes(identity), hashlib.sha256).hexdigest()


def build_wire_context(settings: Any, snapshot: dict[str, Any], session: dict[str, Any],
                       request_id: str, previous_chain: list[dict[str, Any]]) -> dict[str, Any]:
    upgrade = snapshot.get("cache_upgrade") or {}
    channel = str(snapshot.get("channel_id") or "")
    epoch = int(session.get("affinity_epoch") or 1)
    v66 = int(upgrade.get("version") or 0) >= 2
    if v66 and session.get("sticky_scope") == "conversation_family":
        token_session = {**session, "system_semantic_hash": stable_hash(snapshot.get("instructions") or "")}
        token = session_affinity_token(settings, token_session, epoch)
    else:
        token = session.get("session_affinity_token") or session_affinity_token(settings, session, epoch)
    profile = (snapshot.get("protocol_profile") or {}).get("cache") or {}
    header = profile.get("affinity_header")
    if header is None and channel == "aihubmix":
        header = "X-Aihubmix-Session-Id"
    hosts = profile.get("affinity_hosts")
    if hosts is None and channel == "aihubmix":
        hosts = [urlsplit(url).hostname for url in (
            settings.aihubmix_openai_base_url, settings.aihubmix_claude_base_url,
            settings.aihubmix_gemini_base_url, settings.aihubmix_gemini_sdk_base_url,
        ) if url]
    policy = snapshot.get("cache_policy") or {}
    managed = bool(upgrade and upgrade.get("grok_mode") != "legacy_v1"
                   and snapshot.get("requested_cache_mode") != "off" and not policy.get("deny")
                   and not (snapshot.get("requested_cache_mode") == "auto" and policy.get("auto_disabled")))
    route = (session.get("metadata") or {}).get("_relay_route") or {}
    return {
        "request_id": request_id, "session_id": str(session["id"]),
        "provider": snapshot.get("provider"), "channel": channel,
        "scope": stable_hash([session["id"], snapshot.get("route_binding_hash"), epoch,
                              route.get("account_scope_hash"), snapshot.get("protocol_profile_hash")]),
        "phase": "cache_prepare", "normalize": managed,
        "affinity_eviction_strategy": (
            (snapshot.get("cache_contract") or {}).get("affinity_eviction_strategy")
            or profile.get("affinity_eviction_strategy")
            or ("STATEFUL_HANDLE_STATUS_ONLY" if v66 and snapshot.get("protocol") == "gemini_native"
                else "ROUTER_STICKY_AND_HIT_RATE")
        ),
        "unordered_tools": profile.get("unordered_tools") is True,
        "semantic_fingerprint": (snapshot.get("context_plan") or {}).get("semantic_fingerprint"),
        "fingerprint_version": int(upgrade.get("fingerprint_version") or 1),
        "previous_chain": previous_chain, "affinity_epoch": epoch,
        "affinity_token": token if managed and header and hosts else None,
        "affinity_hosts": tuple(hosts or ()), "affinity_header": header,
        "secondary_affinity_header": (
            "x-session-affinity" if profile.get("send_generic_affinity_header") is True else None
        ),
    }


def _audits(rows: list[dict[str, Any]], epoch: int) -> list[dict[str, Any]]:
    return [audit if isinstance(audit, dict) and audit.get("affinity_epoch") == epoch else {}
            for row in rows for audit in [(row.get("cache_usage") or {}).get("affinity_audit")]]


def drift_reason(rows: list[dict[str, Any]], *, epoch: int,
                 now: datetime | None = None, min_tokens: int = 2048,
                 warm_seconds: int = 180, consecutive: int = 2,
                 strategy: str = "ROUTER_STICKY_AND_HIT_RATE") -> str | None:
    audits = _audits(rows, epoch)
    if not audits or not audits[0]:
        return None
    if audits[0].get("handle_rejected_before_expiry") is True:
        return "unexpired_handle_inaccessible"
    if strategy == "STATEFUL_HANDLE_STATUS_ONLY":
        return None
    headers = audits[0].get("route") or {}
    if str(headers.get("x-aihubmix-router-fallback")).lower() == "true":
        return "upstream_router_fallback"
    if str(headers.get("x-aihubmix-router-sticky")).lower() == "false":
        return "upstream_router_unstuck"
    if len(audits) < consecutive + 1:
        return None
    # Each zero must be a real observation on a continuous, recent prefix.
    # Unknowns, incompatible Requests, and old epochs break the streak.
    pairs = zip(rows[:consecutive], rows[1:consecutive + 1], audits[:consecutive])
    first = True
    clock = now or datetime.now(timezone.utc)
    for row, older, audit in pairs:
        diff = audit.get("prefix_diff") or {}
        if (audit.get("implicit_active") is not True or type(audit.get("read_tokens")) is not int
                or audit["read_tokens"] != 0 or diff.get("lcp_estimated_tokens", 0) < min_tokens
                or diff.get("comparable") is False
                or audit.get("compared_request_id") != str(older["id"])):
            return None
        try:
            current = datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
            before = datetime.fromisoformat(str(older["completed_at"]).replace("Z", "+00:00"))
            if not 0 <= (current - before).total_seconds() <= warm_seconds:
                return None
            if first and not 0 <= (clock - current).total_seconds() <= warm_seconds:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        first = False
    return "consecutive_warm_prefix_misses"


async def rotate_affinity(repo: Any, request_row: dict[str, Any], session: dict[str, Any],
                          fence: Any, reason: str) -> dict[str, Any]:
    epoch = int(session.get("affinity_epoch") or 1)
    token = session_affinity_token(repo.settings, session, epoch + 1)
    if not await repo.rotate_cache_affinity(str(request_row["id"]), epoch, token, reason, fence):
        raise CacheDecisionError("CACHE_AFFINITY_FENCE_REJECTED", "Cache affinity rotation lost its execution fence")
    return {**session, "affinity_epoch": epoch + 1, "session_affinity_token": token}
