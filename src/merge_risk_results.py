from __future__ import annotations

from typing import Any

from .normalize_risks import normalize_and_dedupe_risks, normalize_text


def _stream_default_source_type(item: dict[str, Any], stream: str) -> str:
    raw = str(item.get("risk_source_type", "") or "").strip()
    if raw in {"anchored", "missing_clause", "multi_clause"}:
        return raw
    if stream == "anchored":
        return "anchored"
    if bool(item.get("is_multi_clause_risk")):
        return "multi_clause"
    return "missing_clause"


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _dedupe_key(item: dict[str, Any]) -> str:
    source_type = str(item.get("risk_source_type", "anchored") or "anchored").strip()
    label = normalize_text(str(item.get("risk_label", ""))).lower()
    issue = normalize_text(str(item.get("issue", ""))).lower()
    evidence = normalize_text(str(item.get("evidence_text", ""))).lower()
    if source_type == "anchored":
        clause_uid = str(item.get("clause_uid", "") or "").strip()
        return f"anchored||{clause_uid}||{label}||{evidence}"
    if source_type == "missing_clause":
        return f"missing_clause||{label}||{issue}"
    related_uids = sorted(_as_str_list(item.get("related_clause_uids")))
    if related_uids:
        rel = "|".join(related_uids)
    else:
        rel = "|".join(sorted(_as_str_list(item.get("related_clause_ids"))))
    return f"multi_clause||{rel}||{label}"


def _raise_level(level: str, floor: str) -> str:
    rank = {"low": 1, "medium": 2, "high": 3}
    lv = str(level or "medium").lower()
    current = rank.get(lv, 2)
    target = rank.get(floor, 2)
    if current >= target:
        return "high" if current >= 3 else "medium" if current == 2 else "low"
    return floor


def _rule_callback(item: dict[str, Any]) -> None:
    label = normalize_text(str(item.get("risk_label", "")))
    issue = normalize_text(str(item.get("issue", "")))
    merged = f"{label} {issue}"
    level = str(item.get("risk_level", "medium") or "medium").lower()

    if "赔偿责任上限" in merged and any(k in merged for k in ["无", "未约定", "缺失"]):
        item["risk_level"] = _raise_level(level, "high")
        item.setdefault("quality_flags", []).append("rule_escalated_liability_cap_missing")
        return

    if any(k in merged for k in ["甲方所在地", "管辖", "诉讼"]) and any(k in merged for k in ["不利", "单方", "偏向"]):
        item["risk_level"] = "medium" if level == "high" else ("low" if level == "low" else "medium")
        item.setdefault("quality_flags", []).append("rule_capped_jurisdiction_risk")
        return

    if "验收标准" in merged and any(k in merged for k in ["不明确", "违约", "整改"]):
        item["risk_level"] = _raise_level(level, "medium")
        item.setdefault("quality_flags", []).append("rule_escalated_acceptance_unclear")


def _merge_duplicate_item(existing: dict[str, Any], item: dict[str, Any]) -> None:
    for field in ["related_clause_ids", "related_clause_uids", "clause_uids", "display_clause_ids", "clause_ids", "quality_flags"]:
        merged = list(dict.fromkeys(_as_str_list(existing.get(field)) + _as_str_list(item.get(field))))
        existing[field] = merged
    sources = list(dict.fromkeys(_as_str_list(existing.get("workflow_sources")) + _as_str_list(item.get("workflow_sources"))))
    existing["workflow_sources"] = sources


def merge_risk_results(
    *,
    anchored_payload: dict[str, Any],
    missing_multi_payload: dict[str, Any],
    clauses: list[dict[str, Any]],
) -> dict[str, Any]:
    combined_items: list[dict[str, Any]] = []
    for stream, payload in (("anchored", anchored_payload), ("missing_multi", missing_multi_payload)):
        for raw in payload.get("risk_items") or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item["risk_source_type"] = _stream_default_source_type(item, stream)
            item["workflow_sources"] = [stream]
            combined_items.append(item)

    normalized = normalize_and_dedupe_risks({"risk_items": combined_items}, clauses)
    deduped: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for item in normalized.get("risk_items") or []:
        _rule_callback(item)
        key = _dedupe_key(item)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            deduped.append(item)
        else:
            _merge_duplicate_item(existing, item)

    for idx, item in enumerate(deduped, start=1):
        item["risk_id"] = idx
    return {"risk_items": deduped}
