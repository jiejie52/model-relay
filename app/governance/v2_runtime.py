"""V2 declaration -> review candidate -> operator-approved frozen snapshot.

This is an offline deployment-time compiler, never a request-time Excel reader.
It can only activate supplies already registered in the server control plane,
with a separate certification record. No credentials or provider calls are made.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from ..config import Settings
from ..control_plane import ModelControlPlane
from .v2_import import DeclarationError, parse_v2_declarations

_PROVIDER_NAMES = {
    "Gemini": "gemini",
    "Google": "gemini",
    "OpenAI": "openai",
    "Anthropic": "anthropic",
    "Claude": "anthropic",
    "Grok": "grok",
    "Moonshot": "kimi",
    "Kimi": "kimi",
    "GLM": "glm",
    "Xiaomi": "xiaomi",
    # Catalog parsing does not imply an executable Adapter or registered supply.
    # These labels allow disabled/unpublished candidates to be reviewed; traffic
    # still requires an exact backend Offering, Profile and separate certification.
    "Qwen": "qwen",
    "DeepSeek": "deepseek",
    "MiniMax": "minimax",
    "Mistral": "mistral",
    "Doubao": "doubao",
    "Baidu": "baidu",
    "Tencent": "hunyuan",
    "Meta Llama": "llama",
}
_MECHANISMS = {"显式资源": "stateful_resource", "隐式前缀": "implicit_prefix", "不使用": None}
_MODALITIES = {"文本": "text", "图片": "image", "文件": "document", "音频": "audio", "视频": "video"}
_THINKING = {"自动": "auto", "低": "low", "中": "medium", "高": "high", "超高": "xhigh", "最大": "max", "无": "none", "关闭": "off"}
_STRUCTURED = {"原生 JSON Schema": "native", "后校验": "post_validate", "不使用": "none"}


def _controlled_values(field: str, value: str, mapping: Mapping[str, str]) -> list[str]:
    values = [v.strip() for v in value.replace(",", "、").split("、") if v.strip()]
    if not values or any(v not in mapping for v in values):
        raise DeclarationError(f"unknown controlled vocabulary for {field}: {value!r}")
    return [mapping[v] for v in values]



def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def _read_workbook(source: bytes | str | Path) -> bytes:
    return bytes(source) if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()


def _registered_supplies(registry: Mapping[str, Any] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    if registry is None:
        return {}
    if not isinstance(registry, dict) or registry.get("schema_version") != "relay-v2-supply-registry/1":
        raise DeclarationError("backend supply registry version is invalid")
    supplies = registry.get("supplies")
    if not isinstance(supplies, list):
        raise DeclarationError("backend supply registry must have a supplies array")
    result = {}
    for entry in supplies:
        if not isinstance(entry, dict):
            raise DeclarationError("invalid backend supply registry entry")
        key = tuple(str(entry.get(field) or "").strip() for field in ("model", "environment", "channel"))
        if not all(key) or key in result:
            raise DeclarationError("missing or duplicated backend supply registry key")
        result[key] = entry
    return result


def build_candidate(
    workbook: bytes | str | Path,
    *,
    settings: Settings,
    deployment_map: Mapping[str, str],
    revision: str,
    supply_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile validated V2 intentions, never produce a published object.

    A row with enable intention cannot become active without an exact registered
    offering and a separately managed certification record. Published but
    disabled models still reserve their model name against legacy wildcards.
    """
    content = _read_workbook(workbook)
    declarations = parse_v2_declarations(content)
    base = ModelControlPlane._builtin(settings)
    if not isinstance(deployment_map, dict) or not deployment_map:
        raise DeclarationError("an operator-managed environment -> deployment map is required")
    deployment_map = {str(k).strip(): str(v).strip() for k, v in deployment_map.items()}
    if not all(deployment_map.values()) or len(set(deployment_map.values())) != len(deployment_map):
        raise DeclarationError("deployment map needs nonempty, distinct deployment IDs")
    registry = _registered_supplies(supply_registry)
    if not str(revision).strip():
        raise DeclarationError("a nonempty candidate revision is required")

    model_rows = {}
    for row in declarations["models"]:
        model = row["对外模型名"]
        provider = _PROVIDER_NAMES.get(row["提供方"])
        if provider is None:
            raise DeclarationError(f"unregistered provider {row['提供方']!r} for model {model}")
        model_rows[model] = (provider, row)

    cache_rows = {(row["对外模型名"], row["渠道名称"]): row for row in declarations["caches"]}
    groups: dict[tuple[str, str, str], dict[str, str]] = {}
    offerings = []
    overridden_contracts = []
    seen_physical_supplies: dict[tuple[str, str, str], str] = {}
    for row in declarations["routes"]:
        model = row["对外模型名"]
        environment = row["环境"]
        channel = row["渠道名称"]
        deployment = deployment_map.get(environment)
        if not deployment:
            raise DeclarationError(f"no backend deployment mapping for environment {environment!r}")
        provider, model_row = model_rows[model]
        publish_intent = model_row["发布意向"]
        if publish_intent == "暂不发布":
            if row["启用意向"] == "启用":
                raise DeclarationError(f"model {model}: cannot enable a route for an unpublished model")
            continue
        groups[(provider, model, deployment)] = {"provider": provider, "model": model, "deployment_id": deployment}
        if publish_intent == "暂停":
            if row["启用意向"] == "启用":
                raise DeclarationError(f"model {model}: suspended models cannot enable routes")
            continue
        if row["启用意向"] != "启用":
            continue

        cache = cache_rows.get((model, channel))
        if cache is None:
            raise DeclarationError(f"model {model} channel {channel}: cache declaration required")
        if cache["启用意向"] == "启用":
            # 4.3.2 cannot honor workbook-driven cache defaults/force policy.
            raise DeclarationError(
                f"model {model} channel {channel}: cannot publish an enabled V2 cache policy "
                "until per-session cache defaults/force authorization are implemented"
            )

        registered = registry.get((model, environment, channel))
        if not registered:
            raise DeclarationError(f"model {model} channel {channel}: no backend-registered supply")
        if registered.get("upstream_model") != row["渠道侧模型名"]:
            raise DeclarationError(f"model {model} channel {channel}: unapproved upstream model alias")
        if registered.get("certification_status") != "passed" or not str(registered.get("evidence_ref") or "").strip():
            raise DeclarationError(f"model {model} channel {channel}: provider certification is missing")
        if not str(registered.get("budget_authorization_ref") or "").strip():
            raise DeclarationError(f"model {model} channel {channel}: budget authorization is missing")
        source_id = str(registered.get("offering_id") or "")
        original = base.offerings.get(source_id)
        if original is None:
            raise DeclarationError(f"model {model} channel {channel}: offering is not in the server registry")
        if (original.provider, original.model_pattern, original.deployment_id) != (provider, model, deployment):
            raise DeclarationError(f"model {model} channel {channel}: certified offering identity mismatch")
        connection = base.connection(original.connection_id)
        profile = base.protocol_profile(original.protocol_profile_id)
        contract = base.contract(original.capability_contract_id)
        if not connection or not profile or not contract or profile.protocol != connection.protocol:
            raise DeclarationError(f"model {model} channel {channel}: registered protocol contract unavailable")
        physical_key = model, deployment, original.connection_id
        previous_channel = seen_physical_supplies.get(physical_key)
        if previous_channel is not None and previous_channel != channel:
            raise DeclarationError(f"model {model}: two channel labels map to one physical connection")
        seen_physical_supplies[physical_key] = channel
        required_modalities = _controlled_values("input modalities", model_row["输入类型"], _MODALITIES)
        required_thinking = _controlled_values("thinking", model_row["思考档位"], _THINKING)
        structured = _STRUCTURED.get(model_row["结构化输出"])
        if structured is None:
            raise DeclarationError(f"model {model}: unknown structured output guarantee")
        supported, reason = contract.supports_requirements({
            "input_modalities": required_modalities,
            "think_levels": required_thinking,
            "structured_output": structured,
        })
        if not supported:
            raise DeclarationError(f"model {model} channel {channel}: capability mismatch: {reason}")
        target_output = model_row["目标输出上限 Token"]
        if target_output is not None:
            validated_limit = registered.get("validated_max_output_tokens")
            if isinstance(validated_limit, bool) or not isinstance(validated_limit, int) or validated_limit < target_output:
                raise DeclarationError(f"model {model} channel {channel}: output limit lacks certification")
            output_rule = contract.supported_options.get("max_output_tokens")
            if not output_rule:
                raise DeclarationError(f"model {model} channel {channel}: max output option unavailable")
            contract_limit = output_rule.get("max")
            if contract_limit is not None and target_output > contract_limit:
                raise DeclarationError(f"model {model} channel {channel}: contract output token limit too small")
        # When the V2 row disables cache, narrow the frozen capability contract
        # to an empty cache contract. This prevents an existing legacy 'auto'
        # request from secretly enabling provider cache on the selected supply.
        disabled_contract_id = f"v2-nocache:{deployment}:{model}:{channel}"
        disabled_contract = replace(
            contract,
            contract_id=disabled_contract_id,
            cache={"version": "relay-cache-contract/1", "supported_mechanisms": [], "verification_status": "disabled"},
            metadata={**contract.metadata, "managed_v2_cache_disabled": True},
        )
        overridden_contracts.append(disabled_contract.canonical())
        metadata = dict(original.metadata)
        metadata.update({
            "managed_v2": True,
            "unavailable_action": ("reject_new_session" if row["不可用时"] == "拒绝新会话" else "try_next"),
            "v2_channel_label": channel,
            "v2_environment": environment,
            "certification_evidence_ref": str(registered["evidence_ref"]),
            "budget_authorization_ref": str(registered["budget_authorization_ref"]),
        })
        # Stable declared order: smaller 'sequence' outranks larger. Priorities
        # stay inside their own exact managed model group.
        managed_offering = replace(
            original,
            offering_id=f"v2:{deployment}:{model}:{channel}",
            priority=10_000 - row["顺位"],
            capability_contract_id=disabled_contract_id,
            metadata=metadata,
        )
        if managed_offering.priority <= 0:
            raise DeclarationError(f"model {model}: route sequence out of supported range")
        offerings.append(managed_offering.canonical())

    return {
        "status": "review",
        "mode": "merge",
        "revision": str(revision).strip(),
        "release_metadata": {
            "source": "v2-workbook",
            "workbook_sha256": sha256(content).hexdigest(),
            "declarations_sha256": digest(declarations),
            "base_control_plane_hash": base.control_plane_hash,
            "managed_model_groups": [groups[key] for key in sorted(groups)],
            "deployment_map_sha256": digest(deployment_map),
            "approved_supply_evidence_refs": sorted(
                {item["metadata"]["certification_evidence_ref"] for item in offerings}
            ),
            "budget_authorization_refs": sorted(
                {item["metadata"]["budget_authorization_ref"] for item in offerings}
            ),
        },
        "capability_contracts": overridden_contracts,
        "offerings": offerings,
    }


def approve_candidate(candidate: Mapping[str, Any], approval: Mapping[str, Any]) -> dict[str, Any]:
    """Bind an externally approved candidate to a frozen release artifact.

    This function validates an operator-supplied approval manifest; it does NOT
    authenticate the approver. Access to the manifest and deploy process must
    be controlled externally (CI/CD or an authorized release operator).
    """
    if not isinstance(candidate, dict) or candidate.get("status") != "review":
        raise DeclarationError("only review candidates may be approved")
    if candidate.get("release_metadata", {}).get("source") != "v2-workbook":
        raise DeclarationError("candidate is not a V2 workbook compilation")
    if not isinstance(approval, dict) or approval.get("candidate_sha256") != digest(candidate):
        raise DeclarationError("approval is not bound to these exact candidate bytes/content")
    if approval.get("base_control_plane_hash") != candidate["release_metadata"]["base_control_plane_hash"]:
        raise DeclarationError("approval base snapshot does not match the compiled candidate")
    approver = str(approval.get("approved_by") or "").strip()
    approval_ref = str(approval.get("approval_ref") or "").strip()
    if not approver or not approval_ref:
        raise DeclarationError("externally assigned approver and approval reference are required")
    released = dict(candidate)
    released["status"] = "published"
    released["release_metadata"] = {
        **candidate["release_metadata"],
        "approved_by": approver,
        "approval_ref": approval_ref,
        "candidate_sha256": digest(candidate),
    }
    return released
