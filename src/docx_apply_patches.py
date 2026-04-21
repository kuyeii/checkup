from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from lxml import etree

from .docx_comments import (
    NS,
    _paragraph_text_for_match,
    _pick_explicit_target_candidates,
    _read_xml,
    _unwrap_clauses,
    _xml_bytes,
    w,
)

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
TERMINAL_PUNCT = set("。！？；:：.!?;")
ENUMERATION_DELIMS = set("、，,")
LOOSE_MATCH_RE = re.compile(r'''[\s，。！？；：、“”‘’（）【】《》「」『』\[\]{}()<>.,!?;:'"`~!@#$%^&*_\-+=|\\/]+''')


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _loose_compact_text(text: str) -> str:
    return LOOSE_MATCH_RE.sub("", str(text or "")).lower()


def _compact_text_with_index_map(text: str) -> tuple[str, list[int]]:
    compact_chars: list[str] = []
    index_map: list[int] = []
    for idx, ch in enumerate(str(text or "")):
        if ch.isspace():
            continue
        compact_chars.append(ch)
        index_map.append(idx)
    return "".join(compact_chars), index_map


def _text_contains_candidate(text: str, candidate: str) -> bool:
    raw_text = str(text or "")
    raw_candidate = str(candidate or "")
    if not raw_candidate:
        return False
    if raw_candidate in raw_text:
        return True
    return _compact_text(raw_candidate) in _compact_text(raw_text)


def _unwrap_risks(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "risk_result" in payload:
        payload = payload["risk_result"]
    if isinstance(payload, dict) and isinstance(payload.get("risk_items"), list):
        return payload["risk_items"]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    raise ValueError("Unsupported risk payload structure")


def _is_accepted_status(value: Any) -> bool:
    return str(value or "").strip().lower() in {"accepted", "ai_applied"}


def _pick_candidates(risk: dict[str, Any]) -> list[str]:
    accepted_patch = risk.get("accepted_patch") if isinstance(risk.get("accepted_patch"), dict) else {}
    accepted_before = str(accepted_patch.get("before_text") or "").strip()

    ai_rewrite = risk.get("ai_rewrite") if isinstance(risk.get("ai_rewrite"), dict) else {}
    ai_apply = risk.get("ai_apply") if isinstance(risk.get("ai_apply"), dict) else {}
    locator = risk.get("locator") if isinstance(risk.get("locator"), dict) else {}
    status = str(risk.get("status") or "").strip().lower()
    decision = str(risk.get("ai_rewrite_decision") or "").strip().lower()
    ai_state = str(ai_rewrite.get("state") or ai_apply.get("state") or "").strip().lower()
    ai_first = _is_accepted_status(status) and ai_state == "succeeded" and (not decision or decision == "accepted")

    locator_resolved_target_text = str(risk.get("locator_resolved_target_text") or "").strip()

    if ai_first:
        ranked_sources: list[tuple[int, str]] = [
            (0, accepted_before),
            (0, str(ai_apply.get("target_text") or "").strip()),
            (0, str(ai_rewrite.get("target_text") or "").strip()),
            (1, str(locator.get("matched_text") or "").strip()),
            (2, str(risk.get("target_text") or "").strip()),
            (3, str(risk.get("main_text") or "").strip()),
            (4, str(risk.get("evidence_text") or "").strip()),
            (5, locator_resolved_target_text),
            (6, str(risk.get("anchor_text") or "").strip()),
        ]
    else:
        ranked_sources = [
            (0, accepted_before),
            (1, str(locator.get("matched_text") or "").strip()),
            (2, str(ai_rewrite.get("target_text") or "").strip()),
            (2, str(ai_apply.get("target_text") or "").strip()),
            (3, str(risk.get("target_text") or "").strip()),
            (4, str(risk.get("main_text") or "").strip()),
            (5, str(risk.get("evidence_text") or "").strip()),
            (6, locator_resolved_target_text),
            (7, str(risk.get("anchor_text") or "").strip()),
        ]

    by_compact: dict[str, tuple[int, str]] = {}
    for rank, raw in ranked_sources:
        text = str(raw or "").strip()
        compact = _compact_text(text)
        if len(compact) < 1:
            continue
        prev = by_compact.get(compact)
        if prev is None:
            by_compact[compact] = (rank, text)
            continue
        prev_rank, prev_text = prev
        if rank < prev_rank or (rank == prev_rank and len(compact) > len(_compact_text(prev_text))):
            by_compact[compact] = (rank, text)

    if not by_compact:
        return []

    ranked = [(rank, text, _compact_text(text)) for rank, text in by_compact.values()]
    explicit_ai_targets = [
        text
        for rank, text, compact in ranked
        if rank == 0 and len(compact) >= 1
    ] if ai_first else []
    explicit_ai_targets.sort(key=lambda item: -len(_compact_text(item)))

    strong = [it for it in ranked if len(it[2]) >= 4]
    pool = strong if strong else ranked
    pool.sort(key=lambda item: (item[0], -len(item[2])))

    ordered: list[str] = []
    seen: set[str] = set()
    for text in explicit_ai_targets + [text for _rank, text, _compact in pool]:
        compact = _compact_text(text)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        ordered.append(text)
    return ordered


def _pick_locator_validation_candidates(risk: dict[str, Any]) -> list[str]:
    """
    Candidates used only to validate whether an existing locator still points to
    the intended paragraph. We intentionally exclude locator-derived texts here:
    a stale/wrong locator must never self-validate and drag the revision onto an
    unrelated paragraph during DOCX export.
    """
    accepted_patch = risk.get("accepted_patch") if isinstance(risk.get("accepted_patch"), dict) else {}
    ai_rewrite = risk.get("ai_rewrite") if isinstance(risk.get("ai_rewrite"), dict) else {}
    ai_apply = risk.get("ai_apply") if isinstance(risk.get("ai_apply"), dict) else {}

    ranked_sources = [
        (0, str(accepted_patch.get("before_text") or "").strip()),
        (1, str(ai_apply.get("target_text") or "").strip()),
        (1, str(ai_rewrite.get("target_text") or "").strip()),
        (2, str(risk.get("target_text") or "").strip()),
        (3, str(risk.get("main_text") or "").strip()),
        (4, str(risk.get("evidence_text") or "").strip()),
        (5, str(risk.get("anchor_text") or "").strip()),
    ]

    by_compact: dict[str, tuple[int, str]] = {}
    for rank, raw in ranked_sources:
        text = str(raw or "").strip()
        compact = _compact_text(text)
        if len(compact) < 1:
            continue
        prev = by_compact.get(compact)
        if prev is None or rank < prev[0] or (rank == prev[0] and len(compact) > len(_compact_text(prev[1]))):
            by_compact[compact] = (rank, text)

    ranked = [(rank, text, _compact_text(text)) for rank, text in by_compact.values()]
    ranked.sort(key=lambda item: (item[0], -len(item[2])))
    return [text for _rank, text, _compact in ranked]


def _set_text(node: etree._Element, text: str) -> None:
    if text[:1].isspace() or text[-1:].isspace():
        node.set(XML_SPACE, "preserve")
    node.text = text


def _clone_rpr(rpr: etree._Element | None) -> etree._Element | None:
    if rpr is None:
        return None
    return deepcopy(rpr)


def _rpr_signature(rpr: etree._Element | None) -> bytes:
    if rpr is None:
        return b""
    return etree.tostring(rpr, encoding="utf-8")


def _rpr_has_underline(rpr: etree._Element | None) -> bool:
    if rpr is None:
        return False
    u = rpr.find(w("u"))
    if u is None:
        return False
    val = str(u.get(w("val")) or "single").strip().lower()
    return val != "none"


def _force_underline_in_rpr(rpr: etree._Element | None) -> etree._Element:
    base = _clone_rpr(rpr) or etree.Element(w("rPr"))
    u = base.find(w("u"))
    if u is None:
        u = etree.SubElement(base, w("u"))
    u.set(w("val"), "single")
    return base


def _force_no_underline_in_rpr(rpr: etree._Element | None) -> etree._Element | None:
    if rpr is None:
        return None
    base = _clone_rpr(rpr)
    if base is None:
        return None
    u = base.find(w("u"))
    if u is not None:
        u.set(w("val"), "none")
    return base


def _append_plain_run(paragraph: etree._Element, text: str, rpr: etree._Element | None = None) -> None:
    if not text:
        return
    r_el = etree.SubElement(paragraph, w("r"))
    rpr_copy = _clone_rpr(rpr)
    if rpr_copy is not None:
        r_el.append(rpr_copy)
    t_el = etree.SubElement(r_el, w("t"))
    _set_text(t_el, text)


def _append_deleted_run(
    paragraph: etree._Element,
    text: str,
    rev_id: int,
    author: str,
    rev_date: str,
    rpr: etree._Element | None = None,
) -> None:
    if not text:
        return
    del_el = etree.SubElement(paragraph, w("del"))
    del_el.set(w("id"), str(rev_id))
    del_el.set(w("author"), author)
    del_el.set(w("date"), rev_date)
    r_el = etree.SubElement(del_el, w("r"))
    rpr_copy = _clone_rpr(rpr)
    if rpr_copy is not None:
        r_el.append(rpr_copy)
    t_el = etree.SubElement(r_el, w("delText"))
    _set_text(t_el, text)


def _append_inserted_run(
    paragraph: etree._Element,
    text: str,
    rev_id: int,
    author: str,
    rev_date: str,
    rpr: etree._Element | None = None,
) -> None:
    if not text:
        return
    ins_el = etree.SubElement(paragraph, w("ins"))
    ins_el.set(w("id"), str(rev_id))
    ins_el.set(w("author"), author)
    ins_el.set(w("date"), rev_date)
    r_el = etree.SubElement(ins_el, w("r"))
    rpr_copy = _clone_rpr(rpr)
    if rpr_copy is not None:
        r_el.append(rpr_copy)
    t_el = etree.SubElement(r_el, w("t"))
    _set_text(t_el, text)


def _paragraph_run_pieces(paragraph: etree._Element) -> list[tuple[str, etree._Element | None]]:
    pieces: list[tuple[str, etree._Element | None]] = []
    runs = paragraph.xpath("./w:r", namespaces=NS)
    for run in runs:
        rpr = run.find(w("rPr"))
        text_nodes = run.xpath("./w:t", namespaces=NS)
        if not text_nodes:
            continue
        for t in text_nodes:
            tx = t.text or ""
            if not tx:
                continue
            pieces.append((tx, _clone_rpr(rpr)))
    return pieces


def _slice_pieces(
    pieces: list[tuple[str, etree._Element | None]],
    start: int,
    end: int,
) -> list[tuple[str, etree._Element | None]]:
    if end <= start:
        return []
    out: list[tuple[str, etree._Element | None]] = []
    cursor = 0
    for text, rpr in pieces:
        nxt = cursor + len(text)
        if nxt <= start:
            cursor = nxt
            continue
        if cursor >= end:
            break
        seg_start = max(0, start - cursor)
        seg_end = min(len(text), end - cursor)
        seg_text = text[seg_start:seg_end]
        if seg_text:
            out.append((seg_text, _clone_rpr(rpr)))
        cursor = nxt
    return out


def _append_piece_runs(paragraph: etree._Element, pieces: list[tuple[str, etree._Element | None]]) -> None:
    if not pieces:
        return
    cur_text = ""
    cur_rpr: etree._Element | None = None
    cur_sig: bytes | None = None
    for text, rpr in pieces:
        sig = _rpr_signature(rpr)
        if cur_sig is None:
            cur_sig = sig
            cur_rpr = _clone_rpr(rpr)
            cur_text = text
            continue
        if sig == cur_sig:
            cur_text += text
            continue
        _append_plain_run(paragraph, cur_text, cur_rpr)
        cur_text = text
        cur_rpr = _clone_rpr(rpr)
        cur_sig = sig
    if cur_sig is not None and cur_text:
        _append_plain_run(paragraph, cur_text, cur_rpr)


def _target_has_underlined_digits(pieces: list[tuple[str, etree._Element | None]]) -> bool:
    for text, rpr in pieces:
        if not text:
            continue
        if not _rpr_has_underline(rpr):
            continue
        if any(ch.isdigit() for ch in text):
            return True
    return False


def _first_nonempty_rpr(pieces: list[tuple[str, etree._Element | None]]) -> etree._Element | None:
    for _text, rpr in pieces:
        if rpr is not None:
            return _clone_rpr(rpr)
    return None


def _append_inserted_run_keep_underlined_digits(
    paragraph: etree._Element,
    text: str,
    rev_id: int,
    author: str,
    rev_date: str,
    base_rpr: etree._Element | None = None,
) -> None:
    if not text:
        return
    ins_el = etree.SubElement(paragraph, w("ins"))
    ins_el.set(w("id"), str(rev_id))
    ins_el.set(w("author"), author)
    ins_el.set(w("date"), rev_date)

    for seg in re.finditer(r"\d+|[^\d]+", text):
        token = seg.group(0)
        if not token:
            continue
        r_el = etree.SubElement(ins_el, w("r"))
        if token[0].isdigit():
            r_el.append(_force_underline_in_rpr(base_rpr))
        else:
            rpr = _force_no_underline_in_rpr(base_rpr)
            if rpr is not None:
                r_el.append(rpr)
        t_el = etree.SubElement(r_el, w("t"))
        _set_text(t_el, token)


def _pick_best_target_span(
    old_text: str,
    target_text: str,
    pieces: list[tuple[str, etree._Element | None]],
) -> tuple[int, int] | None:
    if not target_text:
        return None

    starts: list[tuple[int, int]] = []
    from_idx = 0
    while from_idx <= len(old_text) - len(target_text):
        idx = old_text.find(target_text, from_idx)
        if idx < 0:
            break
        starts.append((idx, idx + len(target_text)))
        from_idx = idx + len(target_text)

    if not starts:
        compact_old, compact_old_map = _compact_text_with_index_map(old_text)
        compact_target = _compact_text(target_text)
        if compact_target and len(compact_old) >= len(compact_target):
            compact_from = 0
            while compact_from <= len(compact_old) - len(compact_target):
                compact_idx = compact_old.find(compact_target, compact_from)
                if compact_idx < 0:
                    break
                compact_end = compact_idx + len(compact_target)
                start_idx = compact_old_map[compact_idx]
                end_idx = compact_old_map[compact_end - 1] + 1
                starts.append((start_idx, end_idx))
                compact_from = compact_idx + len(compact_target)

    if not starts:
        return None
    if len(starts) == 1:
        return starts[0]

    punct = set("，。；：,.!?！？ \t\r\n")
    best: tuple[float, tuple[int, int]] | None = None
    for idx, end in starts:
        target_pieces = _slice_pieces(pieces, idx, end)
        score = 0.0
        if _target_has_underlined_digits(target_pieces):
            score += 1000.0
        left = old_text[idx - 1] if idx > 0 else ""
        right = old_text[end] if end < len(old_text) else ""
        if idx == 0 or left in punct:
            score += 20.0
        if end == len(old_text) or right in punct:
            score += 20.0
        compact_len = len(_compact_text(old_text[idx:end]))
        score += compact_len / 100.0
        score -= idx / 10000.0
        if best is None or score > best[0]:
            best = (score, (idx, end))

    if best is None:
        return None
    return best[1]


def _paragraph_text_len(pieces: list[tuple[str, etree._Element | None]]) -> int:
    return sum(len(text) for text, _rpr in pieces)


def _join_piece_text(pieces: list[tuple[str, etree._Element | None]]) -> str:
    return "".join(text for text, _rpr in pieces)


def _cleanup_short_equal_between_inserts(
    opcodes: list[tuple[str, int, int, int, int]],
    old_text: str,
) -> list[tuple[str, int, int, int, int]]:
    """
    Make export diff granularity closer to front-end (diff-match-patch cleanup):
    convert insert + short equal + insert into a single replace on that short equal span.
    Example: insert('乙方提交') + equal('项目') + insert('成果后...') => replace('项目' -> '乙方提交项目成果后...')
    """
    if len(opcodes) < 3:
        return opcodes
    out: list[tuple[str, int, int, int, int]] = []
    i = 0
    while i < len(opcodes):
        if i + 2 < len(opcodes):
            t1, a1, a2, b1, b2 = opcodes[i]
            t2, c1, c2, d1, d2 = opcodes[i + 1]
            t3, e1, e2, f1, f2 = opcodes[i + 2]
            if t1 == "insert" and t2 == "equal" and t3 == "insert":
                equal_text = old_text[c1:c2]
                equal_compact_len = len(_compact_text(equal_text))
                if 1 <= equal_compact_len <= 4:
                    out.append(("replace", c1, c2, b1, f2))
                    i += 3
                    continue
        out.append(opcodes[i])
        i += 1
    return out


def _expand_delete_span_for_enumeration(old_text: str, start: int, end: int, revised_text: str) -> tuple[int, int]:
    if str(revised_text or ""):
        return start, end
    if start < 0 or end <= start or end > len(old_text):
        return start, end

    left = old_text[start - 1] if start > 0 else ""
    right = old_text[end] if end < len(old_text) else ""

    if right and right in ENUMERATION_DELIMS:
        return start, min(len(old_text), end + 1)

    if left and left in ENUMERATION_DELIMS:
        return max(0, start - 1), end

    return start, end


def _replace_paragraph_with_revision(
    paragraph: etree._Element,
    old_text: str,
    target_text: str,
    revised_text: str,
    rev_id: int,
    author: str,
    rev_date: str,
) -> bool:
    pieces = _paragraph_run_pieces(paragraph)
    if not pieces:
        return False
    span = _pick_best_target_span(old_text, target_text, pieces)
    if span is None:
        return False
    idx, end = span
    total_len = _paragraph_text_len(pieces)
    if end > total_len:
        return False

    revised_for_diff = revised_text
    idx, end = _expand_delete_span_for_enumeration(old_text, idx, end, revised_for_diff)
    effective_end = end

    # Dedupe trailing punctuation at right boundary (avoid "。。")
    if revised_for_diff and end < len(old_text):
        tail = revised_for_diff[-1]
        boundary = old_text[end]
        if tail and boundary and tail == boundary and tail in TERMINAL_PUNCT:
            revised_for_diff = revised_for_diff[:-1]
            effective_end = min(total_len, end + 1)

    replaced_pieces = _slice_pieces(pieces, idx, effective_end)
    if not replaced_pieces and not revised_for_diff:
        return False
    new_text = old_text[:idx] + revised_for_diff + old_text[effective_end:]
    if new_text == old_text:
        return False

    ppr = paragraph.find(w("pPr"))
    for child in list(paragraph):
        if ppr is not None and child is ppr:
            continue
        paragraph.remove(child)

    matcher = difflib.SequenceMatcher(a=old_text, b=new_text, autojunk=False)
    opcodes = _cleanup_short_equal_between_inserts(list(matcher.get_opcodes()), old_text)
    changed = False

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            eq_pieces = _slice_pieces(pieces, i1, i2)
            _append_piece_runs(paragraph, eq_pieces)
            continue

        changed = True
        src_pieces = _slice_pieces(pieces, i1, i2)
        src_text = _join_piece_text(src_pieces)

        if tag in {"delete", "replace"} and src_text:
            src_rpr = _first_nonempty_rpr(src_pieces)
            _append_deleted_run(paragraph, src_text, rev_id, author, rev_date, src_rpr)

        if tag in {"insert", "replace"}:
            ins_text = new_text[j1:j2]
            if ins_text:
                style_pieces = src_pieces
                if not style_pieces:
                    left = max(0, i1 - 1)
                    right = min(len(old_text), i1 + 1)
                    style_pieces = _slice_pieces(pieces, left, right)
                base_rpr = _first_nonempty_rpr(style_pieces)
                keep_digits = _target_has_underlined_digits(style_pieces)
                if keep_digits:
                    _append_inserted_run_keep_underlined_digits(paragraph, ins_text, rev_id, author, rev_date, base_rpr)
                else:
                    _append_inserted_run(paragraph, ins_text, rev_id, author, rev_date, base_rpr)
    return changed


def _first_explicit_text(payloads: list[tuple[dict[str, Any], str]]) -> str | None:
    for payload, field in payloads:
        if not isinstance(payload, dict):
            continue
        if field not in payload:
            continue
        value = payload.get(field)
        if value is None:
            continue
        return str(value).strip()
    return None


def _collect_risk_clause_uids(risk: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("clause_uid",):
        raw = str(risk.get(key) or "").strip()
        if raw:
            values.append(raw)
    for key in ("clause_uids", "related_clause_uids"):
        raw_vals = risk.get(key)
        if not isinstance(raw_vals, list):
            continue
        values.extend(str(v or "").strip() for v in raw_vals if str(v or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _load_clause_text_by_uid_for_export(risk_path: Path) -> dict[str, str]:
    run_dir = risk_path.parent
    clauses_path = run_dir / "merged_clauses.json"
    if not clauses_path.exists():
        return {}
    try:
        clauses = _unwrap_clauses(_load_json(clauses_path))
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        uid = str(clause.get("clause_uid") or "").strip()
        if not uid:
            continue
        text = str(clause.get("clause_text") or clause.get("source_excerpt") or "").strip()
        if text:
            mapping[uid] = text
    return mapping


def _pick_append_source_hints(risk: dict[str, Any], clause_text_by_uid: dict[str, str]) -> list[str]:
    ranked: list[tuple[int, str]] = []
    for uid in _collect_risk_clause_uids(risk):
        ranked.append((0, str(clause_text_by_uid.get(uid) or "").strip()))
    ranked.extend(
        [
            (1, str(risk.get("main_text") or "").strip()),
            (2, str(risk.get("evidence_text") or "").strip()),
            (3, str(risk.get("anchor_text") or "").strip()),
            (4, str(risk.get("target_text") or "").strip()),
        ]
    )
    by_compact: dict[str, tuple[int, str]] = {}
    for rank, raw in ranked:
        compact = _compact_text(raw)
        if len(compact) < 4:
            continue
        prev = by_compact.get(compact)
        if prev is None or rank < prev[0] or (rank == prev[0] and len(compact) > len(_compact_text(prev[1]))):
            by_compact[compact] = (rank, raw)
    ordered = sorted(by_compact.values(), key=lambda item: (item[0], -len(_compact_text(item[1]))))
    return [text for _rank, text in ordered]


def _compute_append_only_suffix(source_text: str, revised_text: str) -> str | None:
    source = str(source_text or "")
    revised = str(revised_text or "")
    if not source or not revised or source == revised:
        return None

    matcher = difflib.SequenceMatcher(a=source, b=revised, autojunk=False)
    suffix_parts: list[str] = []
    source_exhausted = False
    consumed_source = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            consumed_source += i2 - i1
            if source_exhausted and _compact_text(source[i1:i2]):
                return None
            if consumed_source >= len(source):
                source_exhausted = True
            continue
        if tag == "delete":
            return None
        if tag == "replace":
            if _compact_text(source[i1:i2]) or _compact_text(revised[j1:j2]):
                return None
            continue
        if tag == "insert":
            inserted = revised[j1:j2]
            if not source_exhausted and _compact_text(inserted):
                return None
            suffix_parts.append(inserted)
    suffix = "".join(suffix_parts).strip()
    if not source_exhausted or not suffix:
        return None
    return suffix


def _extract_append_only_suffix_from_cluster_text(cluster_text: str, revised_text: str) -> str | None:
    cluster_loose = _loose_compact_text(cluster_text)
    revised = str(revised_text or "")
    if not cluster_loose or not revised:
        return None

    lines = revised.splitlines()
    last_source_line = -1
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        loose = _loose_compact_text(line)
        if not loose:
            if re.fullmatch(r"[|:\-]+", re.sub(r"\s+", "", line or "")):
                last_source_line = idx
            continue
        if loose in cluster_loose:
            last_source_line = idx
    if last_source_line < 0 or last_source_line >= len(lines) - 1:
        return None
    suffix = "\n".join(lines[last_source_line + 1 :]).lstrip().strip()
    return suffix or None


def _extract_append_only_suffix_from_source_hint(source_text: str, revised_text: str) -> str | None:
    source = str(source_text or "")
    revised = str(revised_text or "")
    if not source or not revised or source == revised:
        return None

    source_lines = source.splitlines()
    revised_lines = revised.splitlines()
    source_sig: list[tuple[int, str]] = []
    revised_sig: list[tuple[int, str]] = []

    for idx, raw_line in enumerate(source_lines):
        loose = _loose_compact_text(raw_line)
        if loose:
            source_sig.append((idx, loose))
    for idx, raw_line in enumerate(revised_lines):
        loose = _loose_compact_text(raw_line)
        if loose:
            revised_sig.append((idx, loose))

    if not source_sig or len(revised_sig) <= 1:
        return None

    best_match_len = 0
    best_suffix_start: int | None = None
    for source_start in range(len(source_sig)):
        match_len = 0
        while (
            source_start + match_len < len(source_sig)
            and match_len < len(revised_sig)
            and source_sig[source_start + match_len][1] == revised_sig[match_len][1]
        ):
            match_len += 1
        if match_len == 0:
            continue
        if source_start + match_len != len(source_sig):
            continue
        if match_len >= len(revised_sig):
            continue
        matched_chars = sum(len(source_sig[source_start + offset][1]) for offset in range(match_len))
        if match_len < 2 and matched_chars < 24:
            continue
        suffix_start = revised_sig[match_len][0]
        if match_len > best_match_len or (match_len == best_match_len and (best_suffix_start is None or suffix_start < best_suffix_start)):
            best_match_len = match_len
            best_suffix_start = suffix_start

    if best_suffix_start is None:
        return None

    suffix = "\n".join(revised_lines[best_suffix_start:]).lstrip().strip()
    return suffix or None

def _collect_sequential_paragraph_cluster(
    paragraphs: list[etree._Element],
    paragraph_texts: list[str],
    start_idx: int,
    source_text: str,
) -> list[int]:
    compact_source = _loose_compact_text(source_text)
    if not compact_source or start_idx < 0 or start_idx >= len(paragraphs):
        return []

    cluster: list[int] = []
    cursor = 0
    matched_any = False
    skipped_after_match = 0
    for idx in range(start_idx, len(paragraphs)):
        raw_text = paragraph_texts[idx]
        compact_para = _loose_compact_text(raw_text)
        if not compact_para:
            if not matched_any:
                continue
            skipped_after_match += 1
            if skipped_after_match >= 3:
                break
            continue
        found_at = compact_source.find(compact_para, cursor)
        if found_at < 0:
            if not matched_any:
                continue
            skipped_after_match += 1
            if skipped_after_match >= 2:
                break
            continue
        matched_any = True
        skipped_after_match = 0
        cluster.append(idx)
        cursor = found_at + len(compact_para)
        if cursor >= len(compact_source):
            break
    if not cluster:
        return []
    joined_compact = _loose_compact_text("\n".join(paragraph_texts[idx] for idx in cluster))
    if not joined_compact or joined_compact not in compact_source:
        return []
    return cluster


def _first_nonempty_paragraph_rpr(paragraph: etree._Element) -> etree._Element | None:
    pieces = _paragraph_run_pieces(paragraph)
    return _first_nonempty_rpr(pieces)


def _clear_paragraph_runs(paragraph: etree._Element) -> None:
    ppr = paragraph.find(w("pPr"))
    for child in list(paragraph):
        if ppr is not None and child is ppr:
            continue
        paragraph.remove(child)


def _populate_paragraph_with_insert(
    paragraph: etree._Element,
    text: str,
    rev_id: int,
    author: str,
    rev_date: str,
    base_rpr: etree._Element | None = None,
) -> bool:
    if not text:
        return False
    _clear_paragraph_runs(paragraph)
    _append_inserted_run(paragraph, text, rev_id, author, rev_date, base_rpr)
    return True


def _nearest_ancestor(el: etree._Element | None, tag: str) -> etree._Element | None:
    current = el
    while current is not None:
        if current.tag == tag:
            return current
        current = current.getparent()
    return None


def _create_inserted_paragraph_after(
    anchor: etree._Element,
    text: str,
    rev_id: int,
    author: str,
    rev_date: str,
    base_rpr: etree._Element | None = None,
) -> etree._Element | None:
    parent = anchor.getparent()
    if parent is None:
        return None
    new_para = etree.Element(w("p"))
    _append_inserted_run(new_para, text, rev_id, author, rev_date, base_rpr)
    insert_at = parent.index(anchor) + 1
    parent.insert(insert_at, new_para)
    return new_para


def _try_apply_append_only_cluster_patch(
    *,
    paragraphs: list[etree._Element],
    paragraph_texts: list[str],
    risk: dict[str, Any],
    clause_text_by_uid: dict[str, str],
    revised_text: str,
    rev_id: int,
    author: str,
    rev_date: str,
) -> dict[str, Any] | None:
    source_hints = _pick_append_source_hints(risk, clause_text_by_uid)
    if not source_hints or not revised_text:
        return None

    start_candidates: list[int] = []
    locator = risk.get("locator") if isinstance(risk.get("locator"), dict) else {}
    raw_para_idx = locator.get("paragraph_index")
    try:
        para_idx = int(raw_para_idx)
    except Exception:
        para_idx = -1
    if 0 <= para_idx < len(paragraphs):
        for idx in range(max(0, para_idx - 3), para_idx + 1):
            if idx not in start_candidates:
                start_candidates.append(idx)

    explicit_candidates = _pick_explicit_target_candidates(risk)
    for idx, text in enumerate(paragraph_texts):
        if idx in start_candidates:
            continue
        found = next((candidate for candidate in explicit_candidates if candidate and _text_contains_candidate(text, candidate)), "")
        if found:
            start_candidates.append(idx)
        if len(start_candidates) >= 3:
            break

    if not start_candidates:
        return None

    for start_idx in start_candidates:
        preferred_para_text = paragraph_texts[start_idx]
        for source_hint in source_hints:
            cluster = _collect_sequential_paragraph_cluster(paragraphs, paragraph_texts, start_idx, source_hint)
            if not cluster:
                continue
            cluster_text = "\n".join(paragraph_texts[idx] for idx in cluster)
            suffix = None
            for suffix_hint in source_hints:
                suffix = _extract_append_only_suffix_from_source_hint(suffix_hint, revised_text)
                if suffix:
                    break
            if not suffix:
                suffix = (
                    _extract_append_only_suffix_from_cluster_text(cluster_text, revised_text)
                    or _compute_append_only_suffix(source_hint, revised_text)
                )
            if not suffix:
                continue
            if len(cluster) <= 1 and _text_contains_candidate(preferred_para_text, source_hint):
                continue

            last_idx = cluster[-1]
            base_rpr = _first_nonempty_paragraph_rpr(paragraphs[last_idx])

            target_para: etree._Element | None = None
            if last_idx + 1 < len(paragraphs):
                next_text = paragraph_texts[last_idx + 1]
                next_para = paragraphs[last_idx + 1]
                if not str(next_text or "").strip() and _nearest_ancestor(next_para, w("tbl")) is None:
                    target_para = next_para
                    if _populate_paragraph_with_insert(target_para, suffix, rev_id, author, rev_date, base_rpr):
                        return {
                            "paragraph_index": last_idx + 1,
                            "target_text": "",
                            "revised_text": suffix,
                            "mode": "append_after_cluster_existing_paragraph",
                            "cluster_indices": cluster,
                        }

            last_para = paragraphs[last_idx]
            last_tbl = _nearest_ancestor(last_para, w("tbl"))
            if last_tbl is not None:
                inserted = _create_inserted_paragraph_after(last_tbl, suffix, rev_id, author, rev_date, base_rpr)
            else:
                inserted = _create_inserted_paragraph_after(last_para, suffix, rev_id, author, rev_date, base_rpr)
            if inserted is not None:
                return {
                    "paragraph_index": last_idx + 1,
                    "target_text": "",
                    "revised_text": suffix,
                    "mode": "append_after_cluster_new_paragraph",
                    "cluster_indices": cluster,
                }
    return None


def _has_exportable_patch(
    *,
    status: str,
    decision: str,
    ai_state: str,
    accepted_patch: dict[str, Any],
) -> bool:
    if not _is_accepted_status(status):
        return False
    if decision and decision != "accepted":
        return False

    accepted_kind = str(accepted_patch.get("kind") or "").strip().lower()
    export_mode = str(accepted_patch.get("export_mode") or accepted_patch.get("mode") or "").strip().lower()

    if export_mode in {"comment_only", "annotation_only"}:
        return False

    # Backward compatibility: older reviewed payloads persisted suggestion inserts
    # as accepted_patch with after_text. These are annotation-only decisions and
    # must never be exported as DOCX text revisions.
    if accepted_kind == "suggest_insert":
        return False

    if export_mode in {"document_patch", "doc_patch", "text_patch"}:
        return "after_text" in accepted_patch

    return ai_state == "succeeded"


def export_ai_patches_to_docx(
    input_docx: Path,
    risk_path: Path,
    output_docx: Path,
    author: str = "合同审查系统",
) -> dict[str, Any]:
    risks = _unwrap_risks(_load_json(risk_path))
    clause_text_by_uid = _load_clause_text_by_uid_for_export(risk_path)

    with zipfile.ZipFile(input_docx, "r") as zin:
        overrides: dict[str, bytes] = {}
        doc_root = _read_xml(zin, "word/document.xml")
        paragraphs = doc_root.xpath(".//w:p", namespaces=NS)

        applied: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []
        failed = 0
        revision_id = 0
        revision_date = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

        for risk in risks:
            if not isinstance(risk, dict):
                continue

            status = str(risk.get("status") or "").strip().lower()
            decision = str(risk.get("ai_rewrite_decision") or "").strip().lower()
            ai_rewrite = risk.get("ai_rewrite") if isinstance(risk.get("ai_rewrite"), dict) else {}
            ai_apply = risk.get("ai_apply") if isinstance(risk.get("ai_apply"), dict) else {}
            ai_state = str(ai_rewrite.get("state") or ai_apply.get("state") or "").strip().lower()

            accepted_patch = risk.get("accepted_patch") if isinstance(risk.get("accepted_patch"), dict) else {}
            if not _has_exportable_patch(
                status=status,
                decision=decision,
                ai_state=ai_state,
                accepted_patch=accepted_patch,
            ):
                continue

            revised_text = _first_explicit_text(
                [
                    (accepted_patch, "after_text"),
                    (ai_rewrite, "revised_text"),
                    (ai_apply, "revised_text"),
                ]
            )
            search_candidates = _pick_explicit_target_candidates(risk)
            if revised_text is None:
                failed += 1
                unmatched.append({"risk_id": risk.get("risk_id"), "reason": "missing_revised_or_target"})
                continue

            paragraphs = doc_root.xpath(".//w:p", namespaces=NS)
            paragraph_texts = [_paragraph_text_for_match(p) for p in paragraphs]

            append_report = _try_apply_append_only_cluster_patch(
                paragraphs=paragraphs,
                paragraph_texts=paragraph_texts,
                risk=risk,
                clause_text_by_uid=clause_text_by_uid,
                revised_text=revised_text,
                rev_id=revision_id,
                author=author,
                rev_date=revision_date,
            )
            if append_report is not None:
                applied.append(
                    {
                        "risk_id": risk.get("risk_id"),
                        "paragraph_index": append_report.get("paragraph_index"),
                        "target_text": append_report.get("target_text", ""),
                        "revised_text": append_report.get("revised_text", revised_text),
                        "revision_id": revision_id,
                        "mode": append_report.get("mode"),
                        "cluster_indices": append_report.get("cluster_indices", []),
                    }
                )
                revision_id += 1
                continue

            if not search_candidates:
                failed += 1
                unmatched.append({"risk_id": risk.get("risk_id"), "reason": "missing_revised_or_target"})
                continue

            para: etree._Element | None = None
            chosen_target = ""

            locator = risk.get("locator") if isinstance(risk.get("locator"), dict) else {}
            para_idx_raw = locator.get("paragraph_index")
            try:
                para_idx = int(para_idx_raw)
            except Exception:
                para_idx = -1
            if 0 <= para_idx < len(paragraphs):
                para = paragraphs[para_idx]
                old_text = _paragraph_text_for_match(para)
                locator_candidates = _pick_locator_validation_candidates(risk)
                chosen_target = next((c for c in locator_candidates if c and _text_contains_candidate(old_text, c)), "")
                if not chosen_target:
                    para = None

            if para is None or not chosen_target:
                for p in paragraphs:
                    old_text = _paragraph_text_for_match(p)
                    found = next((c for c in search_candidates if c and _text_contains_candidate(old_text, c)), "")
                    if found:
                        para = p
                        chosen_target = found
                        break

            if para is None or not chosen_target:
                failed += 1
                unmatched.append({"risk_id": risk.get("risk_id"), "reason": "target_not_found"})
                continue

            old_para_text = _paragraph_text_for_match(para)
            ok = _replace_paragraph_with_revision(
                para,
                old_text=old_para_text,
                target_text=chosen_target,
                revised_text=revised_text,
                rev_id=revision_id,
                author=author,
                rev_date=revision_date,
            )
            if not ok:
                failed += 1
                unmatched.append({"risk_id": risk.get("risk_id"), "reason": "replace_failed"})
                continue

            applied.append(
                {
                    "risk_id": risk.get("risk_id"),
                    "paragraph_index": paragraphs.index(para),
                    "target_text": chosen_target,
                    "revised_text": revised_text,
                    "revision_id": revision_id,
                }
            )
            revision_id += 1

        overrides["word/document.xml"] = _xml_bytes(doc_root)

        with zipfile.ZipFile(output_docx, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                name = info.filename
                if name in overrides:
                    zout.writestr(name, overrides[name])
                else:
                    zout.writestr(name, zin.read(name))

    return {
        "output_docx": str(output_docx),
        "applied": len(applied),
        "failed": failed,
        "unmatched": unmatched,
        "items": applied,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply accepted AI rewrite revisions into DOCX")
    ap.add_argument("input_docx")
    ap.add_argument("risk_json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--author", default="合同审查系统")
    args = ap.parse_args()
    report = export_ai_patches_to_docx(
        input_docx=Path(args.input_docx),
        risk_path=Path(args.risk_json),
        output_docx=Path(args.out),
        author=args.author,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
