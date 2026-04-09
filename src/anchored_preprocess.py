from __future__ import annotations

import re
from typing import Any


_NON_WORD_RE = re.compile(r"[\W_]+", flags=re.UNICODE)


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _clamp_confidence(value: Any) -> float | None:
    text = _clean_str(value)
    if not text:
        return None
    try:
        num = float(text)
    except Exception:
        return None
    if num < 0:
        return 0.0
    if num > 1:
        return 1.0
    return num


def _confidence_to_text(value: float | None) -> str:
    if value is None:
        return "0"
    return format(value, "g")


def _is_effectively_empty_text(text: str) -> bool:
    return not _NON_WORD_RE.sub("", text or "")


def prepare_anchored_clause_input(
    clause: dict[str, Any],
    *,
    review_side: str,
    contract_type_hint: str,
) -> dict[str, Any]:
    clause_uid = _clean_str(clause.get("clause_uid"))
    clause_id = _clean_str(clause.get("clause_id"))
    display_clause_id = _clean_str(clause.get("display_clause_id")) or clause_id
    clause_title = _clean_str(clause.get("clause_title"))
    clause_text = _clean_str(clause.get("clause_text"))
    clause_kind = _clean_str(clause.get("clause_kind")) or "contract_clause"
    source_excerpt = _clean_str(clause.get("source_excerpt")) or clause_text
    segment_id = _clean_str(clause.get("segment_id")) or "segment_unknown"
    segment_title = _clean_str(clause.get("segment_title"))
    numbering_confidence = _confidence_to_text(_clamp_confidence(clause.get("numbering_confidence")))
    title_confidence = _confidence_to_text(_clamp_confidence(clause.get("title_confidence")))

    if clause_kind == "placeholder_clause":
        return {"should_review": False, "skip_reason": "placeholder_clause", "payload": {}}
    if clause_kind == "note_clause":
        return {"should_review": False, "skip_reason": "note_clause", "payload": {}}
    if _is_effectively_empty_text(clause_text):
        return {"should_review": False, "skip_reason": "empty_clause_text", "payload": {}}

    clause_context = "\n".join(
        [
            f"审查视角：{review_side}",
            f"合同类型：{contract_type_hint}",
            f"条款编号：{display_clause_id}",
            f"条款标题：{clause_title}",
            f"条款正文：{clause_text}",
        ]
    )
    payload = {
        "review_side": review_side,
        "contract_type_hint": contract_type_hint,
        "clause_uid": clause_uid,
        "clause_id": clause_id,
        "display_clause_id": display_clause_id,
        "clause_title": clause_title,
        "clause_text": clause_text,
        "clause_kind": clause_kind,
        "source_excerpt": source_excerpt,
        "segment_id": segment_id,
        "segment_title": segment_title,
        "numbering_confidence": numbering_confidence,
        "title_confidence": title_confidence,
        "clause_context": clause_context,
    }
    return {"should_review": True, "skip_reason": "", "payload": payload}
