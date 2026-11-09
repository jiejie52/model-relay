"""Deployment-time model governance CLI. No live provider calls are made."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from hashlib import sha256

from app.config import Settings
from app.control_plane.governance.v2_import import parse_v2_declarations
from app.control_plane.governance.snapshot_compiler import approve_candidate, build_candidate


def _json_file(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _new_output(path: str, content: dict) -> None:
    # Fail instead of silently replacing a previously reviewed or released
    # artifact. The caller must explicitly choose a fresh output filename.
    data = (json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        file_path.unlink(missing_ok=True)
        raise
    print(f"Wrote {file_path} (sha256={sha256(data).hexdigest()})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Model Relay V2 workbook governance")
    commands = parser.add_subparsers(dest="command", required=True)
    default_workbook = "config/model_management_v2.xlsx"
    inspect = commands.add_parser("validate", help="Validate workbook; never activate")
    inspect.add_argument("--workbook", default=default_workbook)
    candidate = commands.add_parser("candidate", help="Create a non-executable review candidate")
    candidate.add_argument("--workbook", default=default_workbook)
    candidate.add_argument("--deployment-map", required=True, help="Trusted environment->deployment JSON mapping")
    candidate.add_argument("--supply-registry", help="Trusted, certified supply registry JSON")
    candidate.add_argument("--revision", required=True)
    candidate.add_argument("--out", required=True)
    cert = commands.add_parser("certification-input", help="Prepare unsigned draft certification identities; grants no traffic")
    cert.add_argument("--source", required=True)
    cert.add_argument("--out", required=True)
    declaration = commands.add_parser("declaration", help="Compile independent connection and supply declarations")
    declaration.add_argument("--source", required=True)
    declaration.add_argument("--certifications", help="Controlled certification records keyed by offering_id")
    declaration.add_argument("--revision", required=True)
    declaration.add_argument("--out", required=True)
    activate = commands.add_parser("activate", help="Atomically switch deployment pointer; restart nodes to load")
    activate.add_argument("--pointer", required=True)
    activate.add_argument("--release", required=True)
    activate.add_argument("--expected-current-sha256", required=True, help="Empty string for first activation")
    activate.add_argument("--relative-paths", action="store_true", help="Write relocatable references relative to the pointer directory")
    publish = commands.add_parser("publish", help="Bind external operator approval to a frozen JSON release")
    publish.add_argument("--candidate", required=True)
    publish.add_argument("--approval", required=True, help="Approval record from controlled deployment workflow")
    publish.add_argument("--out", required=True)
    approval_input = commands.add_parser("approval-input", help="Prepare exact candidate digest for the external approval workflow")
    approval_input.add_argument("--candidate", required=True)
    approval_input.add_argument("--out", required=True)
    diagnose = commands.add_parser("status", help="Describe the actual pinned release and supply readiness; never activates")
    diagnose.add_argument("--expect-file-sha256")
    peers = commands.add_parser("compare-health", help="Compare saved API and Worker health JSON documents")
    peers.add_argument("--api", required=True)
    peers.add_argument("--worker", required=True)
    args = parser.parse_args()
    if args.command == "validate":
        result = parse_v2_declarations(args.workbook)
        print(json.dumps({"status": result["status"], "models": len(result["models"]),
            "routes": len(result["routes"]), "caches": len(result["caches"])}, ensure_ascii=False))
    elif args.command == "activate":
        # Validate the exact file as this deployment will load it before CAS.
        settings = Settings()
        from app.control_plane.definitions import ModelControlPlane
        content = Path(args.release).read_bytes()
        ModelControlPlane.from_settings(settings.model_copy(update={
            "model_control_plane_file": str(Path(args.release).resolve()),
            "model_control_plane_file_sha256": sha256(content).hexdigest(),
            "model_control_plane_json": None, "model_control_plane_release_pointer": None}))
        from app.control_plane.governance.releases import activate_release
        print(json.dumps(activate_release(pointer=args.pointer, release_path=args.release,
            expected_sha256=args.expected_current_sha256, relative_paths=args.relative_paths)))
    elif args.command == "approval-input":
        from app.control_plane.governance.snapshot_compiler import digest
        value = _json_file(args.candidate)
        if value.get("status") != "review":
            raise ValueError("Expected a review candidate")
        _new_output(args.out, {"candidate_sha256": digest(value),
            "base_control_plane_hash": value["release_metadata"]["base_control_plane_hash"],
            "approved_by": None, "approval_ref": None})
    elif args.command == "status":
        from app.control_plane.loading import load_runtime_configuration, describe_configuration
        loaded = load_runtime_configuration(Settings())
        if args.expect_file_sha256 and args.expect_file_sha256 != loaded.diagnostics["release_file_sha256"]:
            raise ValueError("Active release differs from the expected deployment file digest")
        print(json.dumps(describe_configuration(loaded), ensure_ascii=False))
    elif args.command == "compare-health":
        api, worker = _json_file(args.api), _json_file(args.worker)
        left, right = api.get("control_plane_release"), worker.get("control_plane_release")
        keys = ("release_file_sha256", "control_plane_hash", "snapshot_id", "route_revision")
        if not left or not right or any(not left.get(k) or left.get(k) != right.get(k) for k in keys):
            raise ValueError("API and Worker did not load the same approved release")
        print(json.dumps({"status": "matched", **{k: left[k] for k in keys}}))
    elif args.command == "certification-input":
        from app.control_plane.governance.declarations import certification_request
        _new_output(args.out, certification_request(_json_file(args.source), settings=Settings()))
    elif args.command == "declaration":
        from app.control_plane.governance.declarations import build_declaration_candidate
        _new_output(args.out, build_declaration_candidate(_json_file(args.source), settings=Settings(), revision=args.revision,
            certifications=_json_file(args.certifications) if args.certifications else None))
    elif args.command == "candidate":
        settings = Settings()  # uses deployment's existing .env; no network access
        result = build_candidate(args.workbook, settings=settings,
            deployment_map=_json_file(args.deployment_map), revision=args.revision,
            supply_registry=(_json_file(args.supply_registry) if args.supply_registry else None))
        _new_output(args.out, result)
        print("Review-only: this artifact has status=review and cannot be loaded by API/Worker")
    else:
        _new_output(args.out, approve_candidate(_json_file(args.candidate), _json_file(args.approval)))
        print("Deploy only after external approval and pin MODEL_CONTROL_PLANE_FILE_SHA256")


if __name__ == "__main__":
    main()
