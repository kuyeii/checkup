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
    _read_xml,
    _xml_bytes,
    w,
)

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
TERMINAL_PUNCT = set("。！？；:：.!?;")
ENUMERATION_DELIMS = set("、，,")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


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
    if accepted_before:
        return [accepted_before]

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
            (0, str(ai_apply.get("target_text") or "").strip()),
            (0, str(ai_rewrite.get("target_text") or "").strip()),
            (1, str(locator.get("matched_text") or "").strip()),
            (2, str(risk.get("evidence_text") or "").strip()),
            (3, str(risk.get("target_text") or "").strip()),
            (4, locator_resolved_target_text),
            (5, str(risk.get("anchor_text") or "").strip()),
        ]
    else:
        ranked_sources = [
            (0, str(locator.get("matched_text") or "").strip()),
            (1, str(risk.get("evidence_text") or "").strip()),
            (2, str(ai_rewrite.get("target_text") or "").strip()),
            (2, str(ai_apply.get("target_text") or "").strip()),
            (3, str(risk.get("target_text") or "").strip()),
            (4, locator_resolved_target_text),
            (5, str(risk.get("anchor_text") or "").strip()),
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
    starts: list[int] = []
    from_idx = 0
    while from_idx <= len(old_text) - len(target_text):
        idx = old_text.find(target_text, from_idx)
        if idx < 0:
            break
        starts.append(idx)
        from_idx = idx + len(target_text)
    if not starts:
        return None
    if len(starts) == 1:
        s = starts[0]
        return (s, s + len(target_text))

    punct = set("，。；：,.!?！？ \t\r\n")
    best: tuple[float, int] | None = None
    for idx in starts:
        end = idx + len(target_text)
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
        score -= idx / 10000.0
        if best is None or score > best[0]:
            best = (score, idx)

    if best is None:
        return None
    start = best[1]
    return (start, start + len(target_text))


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


def export_ai_patches_to_docx(
    input_docx: Path,
    risk_path: Path,
    output_docx: Path,
    author: str = "合同审查系统",
) -> dict[str, Any]:
    risks = _unwrap_risks(_load_json(risk_path))

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

            if not _is_accepted_status(status):
                continue
            if decision and decision != "accepted":
                continue
            if ai_state != "succeeded":
                continue

            accepted_patch = risk.get("accepted_patch") if isinstance(risk.get("accepted_patch"), dict) else {}
            revised_text = _first_explicit_text(
                [
                    (accepted_patch, "after_text"),
                    (ai_rewrite, "revised_text"),
                    (ai_apply, "revised_text"),
                ]
            )
            candidates = _pick_candidates(risk)
            if revised_text is None or not candidates:
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
                chosen_target = next((c for c in candidates if c and c in old_text), "")

            if para is None or not chosen_target:
                for p in paragraphs:
                    old_text = _paragraph_text_for_match(p)
                    found = next((c for c in candidates if c and c in old_text), "")
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
