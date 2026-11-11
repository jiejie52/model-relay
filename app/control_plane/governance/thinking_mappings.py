"""Offline compilation of the workbook's sole thinking declaration source."""
from app.thinking_contracts import INTENTS, THINKING_CONTRACT_VERSION, ThinkingContractError, validate_thinking_contract
from app.control_plane.governance.v2_import import DeclarationError

FIELDS = ("合同ID", "Relay请求档位", "是否允许调用", "有效思考模式", "原生投影动作",
          "有效执行档位", "有效预算", "满足硬约束", "映射原因编码", "映射说明")
CONTRACT_OPTIONAL = ("显式AUTO是否省略参数", "思考激活禁用温度", "思考激活禁用TopP", "思考激活固定温度",
                     "思考预算下限", "思考预算上限", "思考预算小于输出上限")


def flag(row, field, default=None):
    value = row.get(field)
    if value in (None, "") and default is not None: return default
    if type(value) is bool: return value
    if isinstance(value, str) and value.strip().upper() in {"TRUE", "FALSE", "是", "否"}:
        return value.strip().upper() in {"TRUE", "是"}
    raise DeclarationError(f'{row["_sheet"]}!第{row["_row"]}行 {field}: 必须为 TRUE/FALSE')


def compile_thinking_mappings(rows, contracts):
    groups = {cid: {} for cid in contracts}
    for row in rows:
        cid, intent = str(row.get("合同ID") or "").strip(), str(row.get("Relay请求档位") or "").strip().upper()
        prefix = f'{row["_sheet"]}!第{row["_row"]}行 contract_id={cid} '
        if cid not in groups: raise DeclarationError(prefix + "合同ID: 未登记的合同")
        if intent not in INTENTS or intent in groups[cid]:
            raise DeclarationError(prefix + "Relay请求档位: 未知或重复档位")
        def text(field):
            v = row.get(field)
            return str(v).strip() if v is not None and str(v).strip() else None
        budget = row.get("有效预算")
        if budget == "": budget = None
        if budget is not None and type(budget) is not int:
            raise DeclarationError(prefix + "有效预算: 必须是整数")
        groups[cid][intent] = {"rule_id": cid + ":" + intent,
            "is_allowed": flag(row, "是否允许调用"), "effective_mode": text("有效思考模式"),
            "wire_action": text("原生投影动作"), "effective_level": text("有效执行档位"),
            "effective_budget": budget, "satisfies_require": flag(row, "满足硬约束"),
            "mapping_reason": text("映射原因编码"), "description": text("映射说明"),
            "_source_row": row["_row"]}
    result = {}
    for cid, mappings in groups.items():
        row = contracts[cid]
        strategy = str(row.get("思考投影") or "none").strip()
        lo, hi = row.get("思考预算下限"), row.get("思考预算上限")
        if lo in (None, ""): lo = -1 if strategy == "gemini_thinking_budget" else 1
        if hi in (None, ""): hi = 2**31 - 1
        if type(lo) is not int or type(hi) is not int or lo > hi:
            raise DeclarationError(f'能力合同!第{row["_row"]}行 contract_id={cid} 思考预算上下限: 无效范围')
        active = {"forbid_temperature": flag(row, "思考激活禁用温度", False),
                  "forbid_top_p": flag(row, "思考激活禁用TopP", False),
                  "budget_lt_max_output": flag(row, "思考预算小于输出上限", False)}
        fixed = row.get("思考激活固定温度")
        if fixed not in (None, ""):
            import math
            if isinstance(fixed, bool) or not isinstance(fixed, (int, float)) or not math.isfinite(fixed):
                raise DeclarationError(f'能力合同!第{row["_row"]}行 contract_id={cid} 思考激活固定温度: 必须为有限数值')
            if active["forbid_temperature"]:
                raise DeclarationError(f'能力合同!第{row["_row"]}行 contract_id={cid} 思考激活固定温度: 与禁止温度冲突')
            active["temperature"] = fixed
        spec = {"version": THINKING_CONTRACT_VERSION, "wire_strategy": strategy, "mappings": mappings,
            "omit_on_explicit_auto": flag(row, "显式AUTO是否省略参数", True),
            "minimum_budget": lo, "maximum_budget": hi, "when_thinking_active": active}
        try:
            spec["derived_capabilities"] = validate_thinking_contract(spec)
        except ThinkingContractError as exc:
            source_row = mappings.get(exc.intent, {}).get("_source_row", row["_row"])
            raise DeclarationError(f'思考映射清单!第{source_row}行 contract_id={cid} {exc.field}: {exc}') from exc
        for r in mappings.values(): r.pop("_source_row")
        result[cid] = spec
    return result
