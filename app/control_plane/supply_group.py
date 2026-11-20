"""Exact managed scope declarations; no channel-specific supply synthesis."""
def managed_groups(offerings):
    return [{"provider": p, "model": m, "deployment_id": d} for p, m, d in sorted({
        (x.provider, x.model_pattern, x.deployment_id) for x in offerings
        if "*" not in x.model_pattern and "?" not in x.model_pattern})]
