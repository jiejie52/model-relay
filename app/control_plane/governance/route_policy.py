"""Compile governed route policy into supplies; never runs in request selection."""
from dataclasses import replace
from fnmatch import fnmatchcase
from urllib.parse import urlsplit


def validate_rules(rules):
    if not isinstance(rules, list):
        raise ValueError("route_policies must be an array")
    keys = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {
            "provider", "model_pattern", "deployment_id", "allowed_channels", "allowed_hosts", "unavailable_action"
        }:
            raise ValueError("Invalid governed route policy fields")
        key = tuple(rule[k] for k in ("provider", "model_pattern", "deployment_id"))
        if any(not isinstance(v, str) or not v.strip() for v in key) or key in keys:
            raise ValueError("Missing or duplicate route policy scope")
        keys.add(key)
        if rule["unavailable_action"] not in {"try_next", "reject_new_session"}:
            raise ValueError("Invalid route policy fallback action")
        for name in ("allowed_channels", "allowed_hosts"):
            if not isinstance(rule[name], list) or not rule[name] or any(
                not isinstance(x, str) or not x.strip() for x in rule[name]
            ):
                raise ValueError("Route policy authorization lists must not be empty")


def matching_rules(rules, supply):
    return [r for r in rules if r["provider"].lower() == supply.provider.lower()
        and fnmatchcase(supply.model_pattern.lower(), r["model_pattern"].lower())
        and r["deployment_id"] in ("*", supply.deployment_id)]


def compile_rules(plane, rules):
    validate_rules(rules)
    supplies = []
    for supply in plane.offerings.values():
        matching = matching_rules(rules, supply)
        spec = plane.connection(supply.connection_id)
        if any(spec.channel_id not in rule["allowed_channels"] for rule in matching):
            supply = replace(supply, enabled=False)
        if matching:
            actions = {r["unavailable_action"] for r in matching}
            if len(actions) != 1:
                raise ValueError("Overlapping route policies have conflicting fallback actions")
            supply = replace(supply, metadata={**supply.metadata, "unavailable_action": actions.pop()})
        if supply.enabled:
            for rule in matching:
                validate_endpoints(plane, supply, rule)
        supplies.append(supply)
    return supplies


def validate_endpoints(plane, supply, rule):
    spec = plane.connection(supply.connection_id)
    if spec.channel_id not in rule["allowed_channels"]:
        raise ValueError("Published supply violates governed channel policy")
    allowed = set(rule["allowed_hosts"])
    endpoints = [spec.rest_base_url or spec.base_url, spec.upload_base_url,
                 spec.sdk_endpoint_options.get("base_url")]
    if not endpoints[0]:
        raise ValueError("Governed supply has no endpoint")
    for endpoint in filter(None, endpoints):
        url = urlsplit(endpoint)
        if (url.scheme != "https" or url.hostname not in allowed or url.port not in (None, 443)
                or url.username or url.password or url.query or url.fragment):
            raise ValueError("Supply endpoint violates governed host policy")
    profile = plane.protocol_profile(supply.protocol_profile_id)
    hosts = profile.metadata.get("allowed_hosts", []) if profile else []
    if not hosts or not set(hosts).issubset(allowed):
        raise ValueError("Profile host authorization exceeds governed route policy")


def validate_published_rules(plane):
    rules = plane.release_metadata.get("route_policies", [])
    validate_rules(rules)
    for supply in plane.offerings.values():
        if supply.enabled:
            for rule in matching_rules(rules, supply):
                validate_endpoints(plane, supply, rule)
