from __future__ import annotations

import hashlib
import re
from typing import Any

CHINESE_NUM_MAP = {
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "百": 100,
}

BOILERPLATE_PATTERNS = [
    r"此处需要根据.*?添加",
    r"正式合同中须删除此处填写说明",
    r"提醒：",
    r"如有，则保留；如无，则删除",
    r"填写说明",
    r"模板",
    r"待填写",
]

_NON_WORD_RE = re.compile(r"[\W_]+", flags=re.UNICODE)

_NUMERAL_PATTERN = r"[一二三四五六七八九十百0-9]+"
ALLOWED_CLAUSE_KINDS = {"contract_clause", "placeholder_clause", "note_clause"}


def chinese_to_int(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    total = 0
    current = 0
    for ch in text:
        val = CHINESE_NUM_MAP.get(ch)
        if val is None:
            return None
        if val in {10, 100}:
            if current == 0:
                current = 1
            total += current * val
            current = 0
        else:
            current = current * 10 + val if current and current >= 10 else current + val
    total += current
    return total or None


def normalize_numeral(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    if token.isdigit():
        return token
    value = chinese_to_int(token)
    return str(value) if value is not None else token


def extract_top_level_from_segment_title(segment_title: str, segment_id: str) -> str:
    title = (segment_title or "").strip()

    m = re.match(rf"第({_NUMERAL_PATTERN})条", title)
    if m:
        return normalize_numeral(m.group(1))

    m = re.match(rf"({_NUMERAL_PATTERN})、", title)
    if m:
        return normalize_numeral(m.group(1))

    m = re.search(r"segment_(\d+)$", segment_id)
    if m:
        return m.group(1)

    return "0"


def extract_top_level_from_clause_ref(raw_clause_id: Any) -> str | None:
    text = str(raw_clause_id or "").strip()
    if not text:
        return None
    text = text.replace("，", ".").replace("、", "").replace("。", "")
    text = re.sub(r"^第", "", text)
    text = re.sub(r"条$", "", text)
    if text.startswith("unlabeled_"):
        return None

    parts = [p for p in text.split(".") if p]
    if not parts:
        return None
    first = normalize_numeral(parts[0])
    return first or None


def _split_parts(raw_clause_id: Any) -> list[str]:
    text = str(raw_clause_id or "").strip()
    if not text:
        return []
    text = text.replace("，", ".").replace("。", ".")
    text = re.sub(r"^第", "", text)
    text = re.sub(r"条$", "", text)
    text = text.replace("、", "")
    if text.startswith("unlabeled_"):
        m = re.search(r"unlabeled_(\d+)", text)
        return [f"u{m.group(1)}"] if m else ["u"]
    return [normalize_numeral(p) for p in text.split(".") if p]


def derive_clause_ids(raw_clause_id: Any, top_level: str, fallback_index: int) -> tuple[str, str, str]:
    parts = _split_parts(raw_clause_id)
    fallback_local = f"u{fallback_index}"

    if not parts:
        local_id = fallback_local
        full_id = f"{top_level}.{local_id}"
        return full_id, local_id, full_id

    # Pure top-level reference, e.g. 六 / 7 / 第七条
    if len(parts) == 1 and parts[0] == top_level:
        return top_level, "", top_level

    # Prefixed with current top-level, e.g. 17.1 / 十七.2
    if parts[0] == top_level and len(parts) > 1:
        local_id = ".".join(parts[1:])
        full_id = f"{top_level}.{local_id}"
        return full_id, local_id, full_id

    # Local numbering within segment, e.g. 2 / 3.1 under section 9 / 15
    local_id = ".".join(parts)
    full_id = f"{top_level}.{local_id}"
    return full_id, local_id, full_id


def is_boilerplate_instruction(text: str, title: str = "") -> bool:
    sample = f"{title}\n{text}".strip()
    if not sample:
        return False
    return any(re.search(pattern, sample) for pattern in BOILERPLATE_PATTERNS)


def _is_effectively_blank_clause(text: str, source_excerpt: str) -> bool:
    # Placeholder detection focuses on body content, not title.
    body = _NON_WORD_RE.sub("", str(text or "").strip())
    if body:
        return False
    excerpt = _NON_WORD_RE.sub("", str(source_excerpt or "").strip())
    return not excerpt


def _normalize_clause_kind(kind: Any, *, text: str, source_excerpt: str, title: str, is_boilerplate: bool) -> str:
    raw = str(kind or "").strip().lower()
    if raw in ALLOWED_CLAUSE_KINDS:
        return raw
    if raw in {"template_instruction", "instruction", "template"}:
        return "note_clause"
    return classify_clause_kind(text, source_excerpt, title, is_boilerplate)


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        v = float(text)
    except Exception:
        return None
    # Phase 2: keep confidence values in [0, 1] for stable downstream behavior.
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


def classify_clause_kind(text: str, source_excerpt: str, title: str, is_boilerplate: bool) -> str:
    if _is_effectively_blank_clause(text, source_excerpt):
        return "placeholder_clause"
    if is_boilerplate:
        return "note_clause"
    return "contract_clause"


def stable_text_hash(text: str) -> str:
    digest = hashlib.sha1((text or "").encode("utf-8")).hexdigest()
    return digest[:10]


def normalize_clause_record(clause: dict[str, Any], *, default_segment_id: str = "segment_unknown", default_segment_title: str = "") -> dict[str, Any]:
    """Normalize Clause Schema v2 fields while preserving original payload keys."""
    record = dict(clause)
    clause_text = str(record.get("clause_text", "") or "").strip()
    clause_title = str(record.get("clause_title", "") or "")
    segment_id = str(record.get("segment_id", default_segment_id) or default_segment_id)
    segment_title = str(record.get("segment_title", default_segment_title) or default_segment_title)
    source_excerpt = str(record.get("source_excerpt", "") or "").strip() or clause_text
    boilerplate = is_boilerplate_instruction(clause_text, clause_title)
    clause_kind = _normalize_clause_kind(
        record.get("clause_kind"),
        text=clause_text,
        source_excerpt=source_excerpt,
        title=clause_title,
        is_boilerplate=boilerplate,
    )
    numbering_confidence = _to_optional_float(record.get("numbering_confidence"))
    title_confidence = _to_optional_float(record.get("title_confidence"))

    record["segment_id"] = segment_id
    record["segment_title"] = segment_title
    record["clause_text"] = clause_text
    record["clause_title"] = clause_title
    record["clause_kind"] = clause_kind
    record["source_excerpt"] = source_excerpt
    record["numbering_confidence"] = numbering_confidence
    record["title_confidence"] = title_confidence
    return record


def normalize_clause_records(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_clause_record(c) for c in clauses]


def normalize_clauses(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_uids: dict[str, int] = {}

    prepared = normalize_clause_records(clauses)
    for index, clause in enumerate(prepared, start=1):
        segment_id = str(clause.get("segment_id", "segment_unknown"))
        segment_title = str(clause.get("segment_title", "") or "")
        source_clause_id = str(clause.get("clause_id", "") or "")
        clause_title = str(clause.get("clause_title", "") or "")
        clause_text = str(clause.get("clause_text", "") or "").strip()
        source_excerpt = str(clause.get("source_excerpt", "") or "").strip() or clause_text

        top_level = extract_top_level_from_segment_title(segment_title, segment_id)
        clause_id, local_clause_id, display_clause_id = derive_clause_ids(source_clause_id, top_level, index)

        boilerplate = is_boilerplate_instruction(clause_text, clause_title)
        clause_kind = str(clause.get("clause_kind", "contract_clause") or "contract_clause")
        numbering_confidence = _to_optional_float(clause.get("numbering_confidence"))
        title_confidence = _to_optional_float(clause.get("title_confidence"))
        base_uid = f"{segment_id}::{clause_id}"
        uid = base_uid
        if uid in seen_uids:
            seen_uids[uid] += 1
            uid = f"{base_uid}::{stable_text_hash(clause_text)}::{seen_uids[base_uid]}"
        else:
            seen_uids[uid] = 1

        normalized.append(
            {
                "clause_uid": uid,
                "segment_id": segment_id,
                "segment_title": segment_title,
                "clause_id": clause_id,
                "display_clause_id": display_clause_id,
                "local_clause_id": local_clause_id,
                "source_clause_id": source_clause_id,
                "clause_title": clause_title,
                "clause_text": clause_text,
                "clause_kind": clause_kind,
                "source_excerpt": source_excerpt,
                "numbering_confidence": numbering_confidence,
                "title_confidence": title_confidence,
                "is_boilerplate_instruction": boilerplate,
                "text_hash": stable_text_hash(clause_text),
            }
        )

    return normalized
