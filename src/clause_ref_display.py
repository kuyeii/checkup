from __future__ import annotations

import re
from typing import Any

from .normalize_clauses import extract_top_level_from_clause_ref

_CLAUSE_UID_RE = re.compile(r"segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）\-]+")
_SEGMENT_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百千万零〇\d]+条)")
_SYNTHETIC_LOCAL_RE = re.compile(r"(?:^|[._-])u(\d+)$", re.IGNORECASE)
_STANDALONE_REF_RE = re.compile(r"\b[0-9]+(?:\.[A-Za-z0-9]+)+\b")
_RULE_TAG_RE = re.compile(r"[【\[][^【】\[\]\n]{0,160}(?:RULE|TPL|POLICY|CHECK|REG|MODEL|STD|CLAUSE)_[^【】\[\]\n]{1,160}[】\]]")
_ADJACENT_DUPLICATE_LABEL_RE = re.compile(r"(第[^，。；;\s]+(?:条|款|段))(?:\s*[、，,；;]\s*\1)+")
_DOUBLE_WRAPPED_LABEL_RE = re.compile(r"第第(?P<core>[A-Za-z0-9一二三四五六七八九十百千万零〇\d_.()（）\-]+)(?P<suffix>条|款|段)(?P=suffix)")
_CANONICAL_LABEL_RE = re.compile(r"^第(?P<core>.+?)(?P<suffix>条|款|段)$")
_SAFE_REF_BOUNDARY = r"A-Za-z0-9_.:：-"


def _clean(text: Any) -> str:
    return str(text or "").strip()


def is_synthetic_clause_ref(ref: Any) -> bool:
    text = _clean(ref)
    if not text:
        return False
    lowered = text.lower()
    if "unlabeled" in lowered:
        return True
    return bool(_SYNTHETIC_LOCAL_RE.search(text))


def _top_level_label_from_ref(raw_ref: Any) -> str:
    top = extract_top_level_from_clause_ref(raw_ref)
    return f"第{top}条" if top else ""


def _is_safe_global_ref_key(ref: Any) -> bool:
    text = _clean(ref)
    if not text:
        return False
    if _CLAUSE_UID_RE.fullmatch(text):
        return True
    if text.startswith("第") and (text.endswith("条") or text.endswith("款") or text.endswith("段")):
        return True
    if "." in text:
        return True
    return False


def build_clause_display_label(clause: dict[str, Any] | None) -> str:
    if not isinstance(clause, dict):
        return ""

    display_ref = _clean(clause.get("display_clause_id") or clause.get("clause_id") or clause.get("source_clause_id"))
    local_ref = _clean(clause.get("local_clause_id"))
    segment_title = _clean(clause.get("segment_title"))

    if display_ref and not is_synthetic_clause_ref(display_ref):
        if display_ref.startswith("第") and (display_ref.endswith("条") or display_ref.endswith("款") or display_ref.endswith("段")):
            return display_ref
        return f"第{display_ref}条"

    segment_heading = ""
    heading_match = _SEGMENT_HEADING_RE.match(segment_title)
    if heading_match:
        segment_heading = heading_match.group(1)
    if not segment_heading:
        segment_heading = _top_level_label_from_ref(display_ref or clause.get("clause_id") or clause.get("source_clause_id"))

    synthetic_source = local_ref or display_ref
    synthetic_match = _SYNTHETIC_LOCAL_RE.search(synthetic_source)
    if segment_heading:
        # 对 3.u12 / segment_3::3.u12 这类内部子段，仅展示上层条款，避免把 u12 暴露给用户。
        return segment_heading
    if synthetic_match:
        top_label = _top_level_label_from_ref(display_ref or clause.get("clause_id") or clause.get("source_clause_id"))
        if top_label:
            return top_label
    if display_ref:
        return display_ref
    return _clean(clause.get("clause_uid"))


def build_clause_alias_map(clauses: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    if not clauses:
        return alias_map

    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        label = build_clause_display_label(clause)
        if not label:
            continue

        clause_uid = _clean(clause.get("clause_uid"))
        if clause_uid:
            alias_map[clause_uid] = label
        if _is_safe_global_ref_key(label):
            alias_map[label] = label

        for key in (
            clause.get("display_clause_id"),
            clause.get("clause_id"),
            clause.get("local_clause_id"),
            clause.get("source_clause_id"),
        ):
            text = _clean(key)
            if text and _is_safe_global_ref_key(text):
                alias_map[text] = label
    return alias_map


def _protect_rule_tags(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def _replace(match: re.Match[str]) -> str:
        token = f"__RULE_TAG_{len(placeholders)}__"
        placeholders[token] = match.group(0)
        return token

    return _RULE_TAG_RE.sub(_replace, text), placeholders


def _restore_rule_tags(text: str, placeholders: dict[str, str]) -> str:
    restored = text
    for token, original in placeholders.items():
        restored = restored.replace(token, original)
    return restored


def _cleanup_redundant_clause_words(text: str) -> str:
    cleaned = str(text or "")
    while True:
        next_text, count = _ADJACENT_DUPLICATE_LABEL_RE.subn(r"\1", cleaned)
        cleaned = next_text
        if count <= 0:
            break
    while True:
        next_text, count = _DOUBLE_WRAPPED_LABEL_RE.subn(r"第\g<core>\g<suffix>", cleaned)
        cleaned = next_text
        if count <= 0:
            break
    cleaned = re.sub(r"((?:第[^，。；;\s]+(?:条|款|段))(?:\s*[、，,；;]\s*第[^，。；;\s]+(?:条|款|段))+)(?:条款|条文)", r"\1", cleaned)
    cleaned = re.sub(r"((?:第[^，。；;\s]+(?:条|款|段)))(?:条款|条文)", r"\1", cleaned)
    cleaned = re.sub(r"(相关|上述|前述|前款|本条)条款条款", r"\1条款", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"([、，,；;])\1+", r"\1", cleaned)
    return cleaned.strip()


def _render_alias_in_context(source: str, start: int, end: int, label: str) -> str:
    canonical = _clean(label)
    if not canonical:
        return canonical

    match = _CANONICAL_LABEL_RE.fullmatch(canonical)
    if not match:
        return canonical

    core = match.group("core")
    suffix = match.group("suffix")
    has_prefix = start > 0 and source[start - 1] == "第"
    has_suffix = source.startswith(suffix, end)

    if has_prefix and has_suffix:
        return core
    if has_prefix:
        return f"{core}{suffix}"
    if has_suffix:
        return f"第{core}"
    return canonical


def humanize_clause_refs(text: Any, alias_map: dict[str, str] | None) -> str:
    raw = _clean(text)
    if not raw:
        return raw

    protected_text, placeholders = _protect_rule_tags(raw)
    replaced = protected_text

    if alias_map:
        items = sorted(((key, value) for key, value in alias_map.items() if key and value), key=lambda item: -len(item[0]))
        if items:
            pattern = re.compile(
                rf"(?<![{_SAFE_REF_BOUNDARY}])(?:{'|'.join(re.escape(key) for key, _value in items)})(?![{_SAFE_REF_BOUNDARY}])"
            )

            def _replace(match: re.Match[str]) -> str:
                token = match.group(0)
                label = alias_map.get(token, token)
                return _render_alias_in_context(protected_text, match.start(), match.end(), label)

            replaced = pattern.sub(_replace, replaced)

    def _replace_synthetic_ref(match: re.Match[str]) -> str:
        token = match.group(0)
        if not is_synthetic_clause_ref(token):
            return token
        return _top_level_label_from_ref(token) or "相关条款"

    replaced = _STANDALONE_REF_RE.sub(_replace_synthetic_ref, replaced)
    replaced = _cleanup_redundant_clause_words(replaced)
    replaced = _restore_rule_tags(replaced, placeholders)
    return replaced
