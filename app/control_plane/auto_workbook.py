"""Automatic deployment lifecycle; Excel is the sole authoring source.

No caller supplies release manifests, approval JSON or activation pointers.
Compile-time static contract checks and local credential presence determine
supply readiness; no upstream probe HTTP requests are issued at startup.
"""
import asyncio
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path

from app.control_plane.definitions import ModelControlPlane
from app.control_plane.governance.deployment_validation import (
    ProbeFailure,
    qualify_supply_static,
    validation_version,
    verify_supply,
)
from app.control_plane.governance.workbook import COMPILER_VERSION, compile_workbook


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def serialize(plane):
    return {
        "revision": plane.revision,
        "status": plane.status,
        "release_metadata": dict(plane.release_metadata),
        **{
            name: [item.canonical() for _, item in sorted(getattr(plane, name).items())]
            for name in (
                "connections",
                "capability_contracts",
                "protocol_profiles",
                "cache_policies",
                "continuation_policies",
            )
        },
        "offerings": [item.canonical() for _, item in sorted(plane.offerings.items())],
    }


def deserialize(value):
    parsers = {
        "connections": "_connection_from_mapping",
        "capability_contracts": "_contract_from_mapping",
        "protocol_profiles": "_profile_from_mapping",
        "cache_policies": "_cache_policy_from_mapping",
        "continuation_policies": "_continuation_policy_from_mapping",
    }
    parts = {
        name: [getattr(ModelControlPlane, parser)(item) for item in (value.get(name) or [])]
        for name, parser in parsers.items()
    }
    return ModelControlPlane(
        revision=value["revision"],
        status=value["status"],
        release_metadata=value["release_metadata"],
        **parts,
        offerings=[ModelControlPlane._offering_from_mapping(item, "*") for item in value["offerings"]],
    )


@dataclass
class WorkbookConfiguration:
    plane: object
    retained: list
    report: dict


def public_configuration_status(report):
    return {
        key: report[key]
        for key in (
            "source",
            "state",
            "compiler_version",
            "workbook_sha256",
            "configuration_hash",
            "ready_supplies",
            "pending_supplies",
            "control_plane_hash",
        )
        if key in report
    }


async def prepare_configuration(settings, backend, *, verifier=verify_supply):
    report = {
        "source": "excel",
        "compiler_version": COMPILER_VERSION,
        "path": settings.model_workbook_file,
        "state": "ready",
        "issues": [],
        "supplies": [],
    }
    base = ModelControlPlane._builtin(settings)
    empty = lambda: ModelControlPlane(
        revision="configuration-pending",
        connections=base.connections.values(),
        capability_contracts=base.capability_contracts.values(),
        protocol_profiles=base.protocol_profiles.values(),
        cache_policies=base.cache_policies.values(),
        continuation_policies=base.continuation_policies.values(),
        offerings=[],
    )
    prefix = settings.relay_storage_prefix + "/configuration/" + sha256(settings.deployment_id.encode()).hexdigest()[:20] + "/"
    archive_ok = True
    retained = []
    try:
        names = await asyncio.wait_for(
            backend.storage_list(prefix + "snapshots/"),
            settings.configuration_io_timeout_seconds,
        )
        for name in names:
            if not name.endswith(".json"):
                continue
            raw = await asyncio.wait_for(
                backend.storage_get(prefix + "snapshots/" + name),
                settings.configuration_io_timeout_seconds,
            )
            if sha256(raw).hexdigest() + ".json" != name:
                raise ValueError("archive_digest_mismatch")
            retained.append(deserialize(json.loads(raw)))
    except Exception as exc:
        archive_ok = False
        report["issues"].append({"stage": "snapshot_history", "reason": type(exc).__name__})

    try:
        content = Path(settings.model_workbook_file).read_bytes()
        plane, targets = compile_workbook(content, settings)
        report.update(
            workbook_sha256=sha256(content).hexdigest(),
            configuration_hash=plane.release_metadata["configuration_hash"],
        )
    except Exception as exc:
        report.update(
            state="pending",
            issues=report["issues"]
            + [{"stage": "workbook_compile", "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}],
        )
        return WorkbookConfiguration(empty(), retained, report)

    async def qualify(supply):
        location = targets[supply.offering_id]
        if not supply.enabled:
            return supply.offering_id, {"status": "disabled", **location}
        if not archive_ok:
            return supply.offering_id, {"status": "pending", "reason": "snapshot_history_unavailable", **location}
        if verifier is verify_supply:
            result = qualify_supply_static(plane, settings, supply)
        else:
            try:
                result = await verifier(settings, plane, supply)
            except Exception as exc:
                result = {"status": "pending", "reason": "validation_" + type(exc).__name__}
        return supply.offering_id, {**result, **location}

    results = dict(await asyncio.gather(*(qualify(s) for s in plane.offerings.values() if s.offering_id in targets)))
    report["supplies"] = [{"offering_id": sid, **record} for sid, record in sorted(results.items())]
    report["ready_supplies"] = sum(r["status"] == "ready" for r in results.values())
    report["pending_supplies"] = sum(r["status"] in ("pending", "unavailable") for r in results.values())

    contracts = dict(plane.capability_contracts)
    supplies = []
    for supply in plane.offerings.values():
        if supply.offering_id in results:
            ready = results[supply.offering_id]["status"] == "ready"
            if ready:
                contract = contracts[supply.capability_contract_id]
                cache = {
                    **contract.cache,
                    "verification_status": "verified",
                    "mechanism_profiles": {
                        k: {
                            **v,
                            "verification_status": (
                                v.get("verification_status")
                                if v.get("verification_status") == "trial"
                                else "verified"
                            ),
                        }
                        for k, v in contract.cache.get("mechanism_profiles", {}).items()
                    },
                }
                continuation = dict(contract.continuation or {})
                if continuation.get("anchor_class") not in (None, "", "none"):
                    facets = set(results[supply.offering_id].get("facets") or ())
                    continuation["verification_status"] = (
                        "verified" if "stateful_continuation" in facets else "unverified"
                    )
                    continuation["supports_multiturn_delta"] = bool(
                        continuation.get("supports_multiturn_delta") and "multiturn_delta" in facets
                    )
                    continuation["supports_branching"] = bool(
                        continuation.get("supports_branching") and "branching" in facets
                    )
                contracts[contract.contract_id] = replace(contract, cache=cache, continuation=continuation)
            supply = replace(
                supply,
                metadata={
                    **supply.metadata,
                    "deployment_validation_state": results[supply.offering_id]["status"],
                    "deployment_validation_reason": results[supply.offering_id].get("reason"),
                },
            )
        supplies.append(supply)

    active = ModelControlPlane(
        revision=plane.revision,
        connections=plane.connections.values(),
        capability_contracts=contracts.values(),
        protocol_profiles=plane.protocol_profiles.values(),
        cache_policies=plane.cache_policies.values(),
        continuation_policies=plane.continuation_policies.values(),
        offerings=supplies,
        release_metadata=dict(plane.release_metadata),
    )
    if any(r["status"] in ("pending", "unavailable") for r in results.values()):
        report["state"] = "partial"
    if not archive_ok:
        report["state"] = "pending"
    if archive_ok:
        raw = encode(serialize(active))
        try:
            await asyncio.wait_for(
                backend.storage_put(prefix + "snapshots/" + sha256(raw).hexdigest() + ".json", raw),
                settings.configuration_io_timeout_seconds,
            )
        except Exception:
            report["state"] = "pending"
            report["issues"].append({"stage": "snapshot_archive", "reason": "storage_unavailable"})
            # Do not admit new managed calls whose frozen client versions cannot be retained.
            for record in results.values():
                if record["status"] == "ready":
                    record.update(status="pending", reason="snapshot_archive_unavailable")
            report["supplies"] = [{"offering_id": sid, **record} for sid, record in sorted(results.items())]
            report["ready_supplies"] = 0
            report["pending_supplies"] = sum(r["status"] in ("pending", "unavailable") for r in results.values())
            active = ModelControlPlane(
                revision=active.revision,
                connections=active.connections.values(),
                capability_contracts=active.capability_contracts.values(),
                protocol_profiles=active.protocol_profiles.values(),
                cache_policies=active.cache_policies.values(),
                continuation_policies=active.continuation_policies.values(),
                offerings=[
                    replace(
                        s,
                        metadata={
                            **s.metadata,
                            "deployment_validation_state": results[s.offering_id]["status"],
                            "deployment_validation_reason": results[s.offering_id].get("reason"),
                        },
                    )
                    if s.offering_id in targets
                    else s
                    for s in active.offerings.values()
                ],
                release_metadata=dict(active.release_metadata),
            )
    report["control_plane_hash"] = active.control_plane_hash
    return WorkbookConfiguration(active, retained, report)
