from __future__ import annotations

from typing import Any


REQUIRED_RISK_FIELDS = {
    "risk_id",
    "dimension",
    "risk_label",
    "risk_level",
    "issue",
    "basis",
    "evidence_text",
    "suggestion",
    "clause_id",
    "display_clause_id",
    "anchor_text",
    "needs_human_review",
    "status",
    "clause_uid",
    "clause_uids",
    "display_clause_ids",
    "clause_ids",
    "is_multi_clause_risk",
    "basis_rule_id",
    "basis_summary",
    "review_required_reason",
    "auto_apply_allowed",
    "is_boilerplate_related",
    "mapping_conflict",
    "risk_source_type",
    "suggestion_minimal",
    "suggestion_optimized",
    "evidence_confidence",
    "quality_flags",
    "related_clause_ids",
    "related_clause_uids",
}

ALLOWED_RISK_SOURCE_TYPES = {"anchored", "missing_clause", "multi_clause"}


def _ensure_list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _infer_risk_source_type(item: dict[str, Any]) -> str:
    raw = str(item.get("risk_source_type", "") or "").strip()
    if raw in ALLOWED_RISK_SOURCE_TYPES:
        return raw
    if bool(item.get("is_multi_clause_risk")):
        return "multi_clause"
    return "anchored"


def _apply_v2_defaults(item: dict[str, Any]) -> None:
    item["risk_source_type"] = _infer_risk_source_type(item)
    item["suggestion"] = str(item.get("suggestion", "") or "")
    item["suggestion_minimal"] = str(item.get("suggestion_minimal", "") or "").strip() or item["suggestion"]
    item["suggestion_optimized"] = str(item.get("suggestion_optimized", "") or "").strip()
    item["quality_flags"] = _ensure_list_of_strings(item.get("quality_flags"))
    item["related_clause_ids"] = _ensure_list_of_strings(item.get("related_clause_ids"))
    item["related_clause_uids"] = _ensure_list_of_strings(item.get("related_clause_uids"))
    if item.get("evidence_confidence") in ("", None):
        item["evidence_confidence"] = None


def validate_risk_result(payload: dict[str, Any]) -> tuple[bool, str]:
    risk_items = payload.get("risk_items")
    if not isinstance(risk_items, list):
        return False, "risk_items 不是数组"

    for idx, item in enumerate(risk_items, start=1):
        if not isinstance(item, dict):
            return False, f"第 {idx} 条风险不是对象"
        _apply_v2_defaults(item)
        missing = sorted(REQUIRED_RISK_FIELDS - set(item.keys()))
        if missing:
            return False, f"第 {idx} 条风险缺少字段: {', '.join(missing)}"
        if item.get("needs_human_review") is not True:
            return False, f"第 {idx} 条风险 needs_human_review 必须为 true"
        if item.get("auto_apply_allowed") is not False:
            return False, f"第 {idx} 条风险 auto_apply_allowed 必须为 false"
        if item.get("status") != "pending":
            return False, f"第 {idx} 条风险 status 必须为 pending"
        if not isinstance(item.get("review_required_reason"), list) or not item.get("review_required_reason"):
            return False, f"第 {idx} 条风险 review_required_reason 必须为非空数组"
        if item.get("evidence_confidence") is not None and not isinstance(item.get("evidence_confidence"), (int, float)):
            return False, f"第 {idx} 条风险 evidence_confidence 必须为数值或 null"
        risk_source_type = str(item.get("risk_source_type", "anchored") or "anchored").strip()
        if risk_source_type == "anchored":
            if not str(item.get("clause_uid", "")).strip():
                return False, f"第 {idx} 条风险 clause_uid 不能为空"
            if not isinstance(item.get("clause_uids"), list) or not item.get("clause_uids"):
                return False, f"第 {idx} 条风险 clause_uids 必须为非空数组"
            if item.get("clause_uid") != item.get("clause_uids")[0]:
                return False, f"第 {idx} 条风险 clause_uid 必须等于 clause_uids 的首项"
            if not isinstance(item.get("display_clause_ids"), list) or not item.get("display_clause_ids"):
                return False, f"第 {idx} 条风险 display_clause_ids 必须为非空数组"
            if not isinstance(item.get("clause_ids"), list) or not item.get("clause_ids"):
                return False, f"第 {idx} 条风险 clause_ids 必须为非空数组"
            if bool(item.get("is_multi_clause_risk")) != (len(item.get("clause_uids")) > 1):
                return False, f"第 {idx} 条风险 is_multi_clause_risk 与 clause_uids 数量不一致"
        elif risk_source_type == "missing_clause":
            evidence_text = str(item.get("evidence_text", "") or "").strip()
            issue = str(item.get("issue", "") or "").strip()
            risk_label = str(item.get("risk_label", "") or "").strip()
            if not evidence_text and not issue and not risk_label:
                return False, f"第 {idx} 条缺失型风险必须包含 evidence_text、issue 或 risk_label"
        elif risk_source_type == "multi_clause":
            related_clause_ids = _ensure_list_of_strings(item.get("related_clause_ids"))
            related_clause_uids = _ensure_list_of_strings(item.get("related_clause_uids"))
            if not related_clause_ids and not related_clause_uids:
                return False, f"第 {idx} 条跨条款风险必须包含 related_clause_ids 或 related_clause_uids"
            issue = str(item.get("issue", "") or "").strip()
            basis_summary = str(item.get("basis_summary", "") or "").strip()
            if not issue and not basis_summary:
                return False, f"第 {idx} 条跨条款风险必须包含 issue 或 basis_summary"
        else:
            return False, f"第 {idx} 条风险 risk_source_type 非法: {risk_source_type}"
    return True, ""
