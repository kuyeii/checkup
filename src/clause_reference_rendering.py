from __future__ import annotations

import re
from typing import Any

from .normalize_clauses import extract_top_level_from_clause_ref

CLAUSE_UID_PATTERN = r"segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）\-:]+"
CLAUSE_UID_RE = re.compile(CLAUSE_UID_PATTERN)
_AUTO_SPLIT_REF_RE = re.compile(r"(?:^|\.)u\d+(?:$|::)", re.IGNORECASE)
_SEGMENT_TITLE_PREFIX_RE = re.compile(r"^\s*第[一二三四五六七八九十百千万零〇0-9]+条[\s、.．:：-]*")
_SECTION_TITLE_PREFIX_RE = re.compile(r"^\s*[一二三四五六七八九十百千万零〇0-9]+[、.．:：-]\s*")
_DUPLICATE_LABEL_RE = re.compile(
    r"(?P<label>第[一二三四五六七八九十百千万零〇0-9]+条(?:（[^）]{1,40}）)?(?:相关约定)?)(?:\s*[、，,；;/]\s*(?P=label))+"
)
_PARENT_LABEL_WITH_OPTIONAL_TITLE_RE = re.compile(r"(第[一二三四五六七八九十百千万零〇0-9]+条(?:（[^）]{1,40}）)?(?:相关约定)?)条款")
_SAFE_REF_BOUNDARY = r"A-Za-z0-9_.:：-"
_USER_VISIBLE_RISK_TEXT_FIELDS = (
    "issue",
    "factual_basis",
    "reasoning_basis",
    "basis_minimal",
    "basis_summary",
    "basis",
    "suggestion",
    "suggestion_minimal",
    "suggestion_optimized",
)


class ClauseReferenceTextSanitizer:
    def __init__(self, clauses: list[dict[str, Any]]):
        self._alias_map = _build_clause_ref_alias_map(clauses)
        self._token_re = _compile_alias_regex(self._alias_map)
        self._list_re = _compile_alias_list_regex(self._alias_map)

    def sanitize_text(self, text: str) -> str:
        raw = str(text or "")
        if not raw or not self._alias_map or self._token_re is None or self._list_re is None:
            return raw

        def replace_list(match: re.Match[str]) -> str:
            chunk = match.group(0)
            labels: list[str] = []
            seen: set[str] = set()
            for token_match in self._token_re.finditer(chunk):
                ref = token_match.group(0)
                entry = self._alias_map.get(ref) or {}
                label = str(entry.get("group_label") or entry.get("single_label") or ref).strip()
                if not label or label in seen:
                    continue
                seen.add(label)
                labels.append(label)
            return "、".join(labels) if labels else chunk

        rewritten = self._list_re.sub(replace_list, raw)
        rewritten = self._token_re.sub(self._replace_single, rewritten)
        rewritten = _DUPLICATE_LABEL_RE.sub(lambda m: m.group("label"), rewritten)
        rewritten = _PARENT_LABEL_WITH_OPTIONAL_TITLE_RE.sub(r"\1", rewritten)
        rewritten = re.sub(r"\s+", " ", rewritten)
        return rewritten.strip()

    def sanitize_risk_item(self, item: dict[str, Any]) -> bool:
        changed = False
        for field in _USER_VISIBLE_RISK_TEXT_FIELDS:
            value = item.get(field)
            if not isinstance(value, str):
                continue
            next_value = self.sanitize_text(value)
            if next_value != value:
                item[field] = next_value
                changed = True
        return changed

    def _replace_single(self, match: re.Match[str]) -> str:
        ref = match.group(0)
        entry = self._alias_map.get(ref) or {}
        return str(entry.get("single_label") or ref)


def sanitize_user_visible_risk_fields(item: dict[str, Any], clauses: list[dict[str, Any]] | None = None, sanitizer: ClauseReferenceTextSanitizer | None = None) -> bool:
    active = sanitizer or ClauseReferenceTextSanitizer(clauses or [])
    return active.sanitize_risk_item(item)


def sanitize_risk_payload_user_visible_text(payload: dict[str, Any], clauses: list[dict[str, Any]]) -> bool:
    sanitizer = ClauseReferenceTextSanitizer(clauses)
    changed = False

    for container_key in ("risk_result", None):
        container = payload.get(container_key) if container_key else payload
        if not isinstance(container, dict):
            continue
        risk_items = container.get("risk_items")
        if not isinstance(risk_items, list):
            continue
        for item in risk_items:
            if not isinstance(item, dict):
                continue
            if sanitizer.sanitize_risk_item(item):
                changed = True
    return changed


def _compile_alias_regex(alias_map: dict[str, dict[str, str]]) -> re.Pattern[str] | None:
    if not alias_map:
        return None
    alternatives = "|".join(re.escape(token) for token in sorted(alias_map, key=len, reverse=True))
    return re.compile(rf"(?<![{_SAFE_REF_BOUNDARY}])(?:{alternatives})(?![{_SAFE_REF_BOUNDARY}])")


def _compile_alias_list_regex(alias_map: dict[str, dict[str, str]]) -> re.Pattern[str] | None:
    token_re = _compile_alias_regex(alias_map)
    if token_re is None:
        return None
    token_pattern = token_re.pattern
    return re.compile(rf"{token_pattern}(?:\s*[、，,；;/]\s*{token_pattern})+")


def _build_clause_ref_alias_map(clauses: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    alias_map: dict[str, dict[str, str]] = {}
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        single_label, group_label = _build_clause_display_labels(clause)
        if not single_label and not group_label:
            continue
        entry = {
            "single_label": single_label or group_label,
            "group_label": group_label or single_label,
        }

        uid = str(clause.get("clause_uid") or "").strip()
        if uid:
            alias_map.setdefault(uid, entry)

        for field in ("display_clause_id", "clause_id", "source_clause_id"):
            ref = str(clause.get(field) or "").strip()
            if not _should_alias_ref(ref):
                continue
            alias_map.setdefault(ref, entry)
    return alias_map


def _should_alias_ref(ref: str) -> bool:
    text = str(ref or "").strip()
    if not text:
        return False
    if CLAUSE_UID_RE.fullmatch(text):
        return True
    if _is_auto_split_ref(text):
        return True
    if text.startswith("第") and text.endswith("条"):
        return True
    return False


def _build_clause_display_labels(clause: dict[str, Any]) -> tuple[str, str]:
    uid = str(clause.get("clause_uid") or "").strip()
    display_clause_id = str(clause.get("display_clause_id") or "").strip()
    clause_id = str(clause.get("clause_id") or "").strip()
    source_clause_id = str(clause.get("source_clause_id") or "").strip()
    title = _extract_clause_title(clause)

    auto_split = any(_is_auto_split_ref(value) for value in (uid, display_clause_id, clause_id, source_clause_id))
    if auto_split:
        parent = _extract_parent_clause_ref(clause)
        if not parent:
            return "相关条款内容", "相关条款内容"
        base = f"第{parent}条"
        if title:
            return f"{base}（{title}）相关约定", f"{base}（{title}）相关约定"
        return f"{base}相关约定", f"{base}相关约定"

    for candidate in (display_clause_id, source_clause_id, clause_id):
        if candidate and not _is_auto_split_ref(candidate):
            return candidate, candidate

    if uid:
        uid_tail = uid.split("::", 1)[-1].strip()
        if uid_tail and not _is_auto_split_ref(uid_tail):
            return uid_tail, uid_tail

    parent = _extract_parent_clause_ref(clause)
    if parent:
        if title:
            return f"第{parent}条（{title}）", f"第{parent}条（{title}）"
        return f"第{parent}条", f"第{parent}条"
    return "相关条款内容", "相关条款内容"


def _extract_parent_clause_ref(clause: dict[str, Any]) -> str:
    for candidate in (
        clause.get("display_clause_id"),
        clause.get("clause_id"),
        clause.get("source_clause_id"),
        str(clause.get("clause_uid") or "").split("::", 1)[-1],
    ):
        top = extract_top_level_from_clause_ref(candidate)
        if top:
            return top
    return ""


def _extract_clause_title(clause: dict[str, Any]) -> str:
    candidates = [
        str(clause.get("clause_title") or "").strip(),
        _strip_segment_title_prefix(str(clause.get("segment_title") or "").strip()),
    ]
    for candidate in candidates:
        cleaned = candidate.strip()
        if not cleaned:
            continue
        if cleaned in {"条款", "合同", "协议"}:
            continue
        return cleaned[:40]
    return ""


def _strip_segment_title_prefix(title: str) -> str:
    text = str(title or "").strip()
    if not text:
        return ""
    text = _SEGMENT_TITLE_PREFIX_RE.sub("", text, count=1)
    text = _SECTION_TITLE_PREFIX_RE.sub("", text, count=1)
    return text.strip(" ：:.-—")


def _is_auto_split_ref(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if CLAUSE_UID_RE.fullmatch(text):
        text = text.split("::", 1)[-1]
    return bool(_AUTO_SPLIT_REF_RE.search(text))
