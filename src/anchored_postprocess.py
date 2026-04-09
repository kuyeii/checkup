from __future__ import annotations

from typing import Any


_WEAK_REASONING_PHRASES = {
    "需要进一步人工审核",
    "建议进一步人工审核",
    "需进一步人工审核",
}

_WEAK_FACTUAL_PHRASES = {
    "根据原文",
    "依据原文",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _ensure_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def postprocess_anchored_risk_items(*, raw_items: list[dict[str, Any]], input_payload: dict[str, Any]) -> dict[str, Any]:
    accepted_items: list[dict[str, Any]] = []
    dropped_items: list[dict[str, Any]] = []
    validation_errors: list[str] = []

    for idx, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            reason = f"item_{idx}: not_object"
            dropped_items.append({"item": raw, "reason": reason})
            validation_errors.append(reason)
            continue

        source_type = _clean(raw.get("risk_source_type"))
        if source_type and source_type != "anchored":
            reason = f"item_{idx}: non_anchored_source_type={source_type}"
            dropped_items.append({"item": raw, "reason": reason})
            validation_errors.append(reason)
            continue

        risk_label = _clean(raw.get("risk_label"))
        issue = _clean(raw.get("issue"))
        evidence_text = _clean(raw.get("evidence_text"))
        factual_basis = _clean(raw.get("factual_basis"))
        reasoning_basis = _clean(raw.get("reasoning_basis"))
        if not risk_label:
            reason = f"item_{idx}: missing_risk_label"
            dropped_items.append({"item": raw, "reason": reason})
            validation_errors.append(reason)
            continue
        if not issue:
            reason = f"item_{idx}: missing_issue"
            dropped_items.append({"item": raw, "reason": reason})
            validation_errors.append(reason)
            continue
        if not evidence_text:
            reason = f"item_{idx}: missing_evidence_text"
            dropped_items.append({"item": raw, "reason": reason})
            validation_errors.append(reason)
            continue
        if not factual_basis:
            reason = f"item_{idx}: missing_factual_basis"
            dropped_items.append({"item": raw, "reason": reason})
            validation_errors.append(reason)
            continue
        if not reasoning_basis:
            reason = f"item_{idx}: missing_reasoning_basis"
            dropped_items.append({"item": raw, "reason": reason})
            validation_errors.append(reason)
            continue

        record = dict(raw)
        record["risk_source_type"] = "anchored"
        clause_uid = _clean(input_payload.get("clause_uid"))
        record["clause_uid"] = clause_uid
        record["clause_uids"] = [clause_uid] if clause_uid else []
        clause_id = _clean(input_payload.get("display_clause_id")) or _clean(input_payload.get("clause_id"))
        record["clause_id"] = clause_id
        record["display_clause_id"] = clause_id
        record["anchor_text"] = _clean(input_payload.get("source_excerpt")) or _clean(input_payload.get("clause_text"))
        record.setdefault("related_clause_ids", [])
        record.setdefault("related_clause_uids", [])
        record.setdefault("quality_flags", [])
        record["related_clause_ids"] = _ensure_list(record.get("related_clause_ids"))
        record["related_clause_uids"] = _ensure_list(record.get("related_clause_uids"))
        quality_flags = _ensure_list(record.get("quality_flags"))

        if any(phrase in reasoning_basis for phrase in _WEAK_REASONING_PHRASES):
            quality_flags.append("weak_reasoning_basis")
        if any(phrase in factual_basis for phrase in _WEAK_FACTUAL_PHRASES):
            quality_flags.append("weak_factual_basis")
        record["quality_flags"] = list(dict.fromkeys(quality_flags))
        accepted_items.append(record)

    return {
        "accepted_items": accepted_items,
        "dropped_items": dropped_items,
        "validation_errors": validation_errors,
    }
