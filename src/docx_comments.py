from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

NS = {"w": W_NS, "r": R_NS, "pr": PKG_REL_NS}

CLAUSE_REF_SPLIT_RE = re.compile(r"\s*[、，,；;/]\s*")


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _xml_bytes(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone="yes")


def _read_xml(zin: zipfile.ZipFile, name: str) -> etree._Element:
    return etree.fromstring(zin.read(name))


def _ensure_content_types(ct_root: etree._Element) -> None:
    xpath = "//*[local-name()='Override' and @PartName='/word/comments.xml']"
    if ct_root.xpath(xpath):
        return
    override = etree.Element("Override")
    override.set("PartName", "/word/comments.xml")
    override.set(
        "ContentType",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    )
    ct_root.append(override)


def _ensure_document_rels(rels_root: etree._Element) -> None:
    rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    for rel in rels_root.xpath("//pr:Relationship", namespaces=NS):
        if rel.get("Type") == rel_type and rel.get("Target") == "comments.xml":
            return
    ids = []
    for rel in rels_root.xpath("//pr:Relationship", namespaces=NS):
        m = re.fullmatch(r"rId(\d+)", rel.get("Id", ""))
        if m:
            ids.append(int(m.group(1)))
    new_id = f"rId{(max(ids) + 1) if ids else 1}"
    rel = etree.SubElement(rels_root, f"{{{PKG_REL_NS}}}Relationship")
    rel.set("Id", new_id)
    rel.set("Type", rel_type)
    rel.set("Target", "comments.xml")


def _ensure_comments_root(existing: bytes | None) -> etree._Element:
    if existing is not None:
        return etree.fromstring(existing)
    return etree.Element(w("comments"), nsmap={"w": W_NS})


def _next_comment_id(comments_root: etree._Element) -> int:
    ids = []
    for c in comments_root.xpath("//w:comment", namespaces=NS):
        try:
            ids.append(int(c.get(w("id"))))
        except Exception:
            pass
    return (max(ids) + 1) if ids else 0


def _paragraph_text_for_match(p: etree._Element) -> str:
    parts = p.xpath(".//w:t/text() | .//w:delText/text()", namespaces=NS)
    return "".join(parts)


def _paragraph_visible_text_for_match(p: etree._Element) -> str:
    parts = p.xpath(".//w:t/text()", namespaces=NS)
    return "".join(parts)


def _add_comment_to_paragraph(p: etree._Element, comment_id: int) -> None:
    crs = etree.Element(w("commentRangeStart"))
    crs.set(w("id"), str(comment_id))
    p.insert(0, crs)

    cre = etree.Element(w("commentRangeEnd"))
    cre.set(w("id"), str(comment_id))
    p.append(cre)

    r_el = etree.SubElement(p, w("r"))
    cr = etree.SubElement(r_el, w("commentReference"))
    cr.set(w("id"), str(comment_id))


def _append_comment(comments_root: etree._Element, comment_id: int, text: str, author: str) -> None:
    c = etree.SubElement(comments_root, w("comment"))
    c.set(w("id"), str(comment_id))
    c.set(w("author"), author)
    c.set(w("date"), _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z")
    p = etree.SubElement(c, w("p"))
    r_el = etree.SubElement(p, w("r"))
    t = etree.SubElement(r_el, w("t"))
    t.text = text


@dataclass(slots=True)
class ParagraphIndex:
    index: int
    text: str
    element: Any


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _candidate_snippets(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    candidates: list[str] = []
    for part in re.split(r"[\n\r]+", text):
        part = part.strip()
        if part:
            candidates.append(part)
    if text not in candidates:
        candidates.insert(0, text)

    expanded: list[str] = []
    for c in candidates:
        expanded.append(c)
        if len(c) > 120:
            expanded.append(c[:120])
        if len(c) > 80:
            expanded.append(c[:80])
        if len(c) > 40:
            expanded.append(c[:40])
    # Keep unique, longest-first
    seen = set()
    unique = []
    for c in sorted(expanded, key=len, reverse=True):
        c2 = _normalize_ws(c)
        if c2 and c2 not in seen:
            seen.add(c2)
            unique.append(c2)
    return unique


def _find_best_paragraph(paragraphs: list[ParagraphIndex], snippets: list[str]) -> tuple[ParagraphIndex | None, str | None]:
    best: ParagraphIndex | None = None
    best_snippet: str | None = None
    best_score = -1
    for snip in snippets:
        sn = _normalize_ws(snip)
        if not sn:
            continue
        for para in paragraphs:
            pt = _normalize_ws(para.text)
            if not pt:
                continue
            matched = False
            score = -1
            if sn in pt:
                matched = True
                score = min(len(sn), len(pt))
            elif len(pt) >= 6 and pt in sn:
                matched = True
                score = len(pt)
            if matched and score > best_score:
                best = para
                best_snippet = sn
                best_score = score
    return best, best_snippet


def _find_first_paragraph_by_priority(
    paragraphs: list[ParagraphIndex],
    snippets: list[str],
) -> tuple[ParagraphIndex | None, str | None]:
    for snippet in snippets:
        para, matched = _find_best_paragraph(paragraphs, [snippet])
        if para is not None:
            return para, matched
    return None, None


def _build_clause_fallback_groups(
    clause_metas: list[dict[str, Any]] | None,
    risk_source_type: str,
) -> list[list[tuple[str, str]]]:
    groups: list[list[tuple[str, str]]] = []
    for clause in clause_metas or []:
        group: list[tuple[str, str]] = []
        for raw, strategy in [
            (clause.get("clause_text"), "clause_text"),
            (clause.get("clause_title"), "clause_title"),
        ]:
            if isinstance(raw, str):
                group.extend((snippet, strategy) for snippet in _candidate_snippets(raw))
        if risk_source_type == "missing_clause":
            for raw in (clause.get("display_clause_id"), clause.get("clause_id"), clause.get("source_clause_id")):
                if isinstance(raw, str):
                    group.extend((snippet, "clause_ref") for snippet in _candidate_snippets(raw))
        if not group:
            continue
        deduped: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for snippet, strategy in group:
            key = (_normalize_ws(snippet), strategy)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            deduped.append((snippet, strategy))
        if deduped:
            groups.append(deduped)
    return groups


def _find_clause_fallback_paragraph(
    paragraphs: list[ParagraphIndex],
    clause_metas: list[dict[str, Any]] | None,
    risk_source_type: str,
) -> tuple[ParagraphIndex | None, str | None, str | None]:
    for group in _build_clause_fallback_groups(clause_metas, risk_source_type):
        para, matched = _find_best_paragraph(paragraphs, [snippet for snippet, _strategy in group])
        if para is None:
            continue
        matched_strategy = next(
            (
                strategy
                for snippet, strategy in group
                if _normalize_ws(snippet) == _normalize_ws(str(matched or ""))
            ),
            "clause_fallback",
        )
        return para, matched, matched_strategy
    return None, None, None


def _resolve_locator_paragraph(
    paragraphs: list[ParagraphIndex],
    risk: dict[str, Any],
) -> ParagraphIndex | None:
    locator = risk.get("locator") if isinstance(risk.get("locator"), dict) else {}
    raw_index = locator.get("paragraph_index")
    try:
        paragraph_index = int(raw_index)
    except Exception:
        return None
    if paragraph_index < 0 or paragraph_index >= len(paragraphs):
        return None
    return paragraphs[paragraph_index]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _unwrap_risk_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "risk_result" in payload:
        payload = payload["risk_result"]
    if isinstance(payload, dict) and isinstance(payload.get("risk_items"), list):
        return payload["risk_items"]
    if isinstance(payload, list):
        return payload
    raise ValueError("Unsupported risk payload structure")


def _unwrap_clauses(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("clauses"), list):
        return payload["clauses"]
    raise ValueError("Unsupported clauses payload structure")


def _build_clause_indexes(clauses: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_uid: dict[str, dict[str, Any]] = {}
    by_id: dict[str, list[dict[str, Any]]] = {}
    for clause in clauses:
        uid = str(clause.get("clause_uid") or "").strip()
        if uid:
            by_uid[uid] = clause
        for key in [
            clause.get("clause_id"),
            clause.get("display_clause_id"),
            clause.get("local_clause_id"),
            clause.get("source_clause_id"),
        ]:
            v = str(key or "").strip()
            if v:
                by_id.setdefault(v, []).append(clause)
    return by_uid, by_id


def _resolve_clauses_for_risk(risk: dict[str, Any], by_uid: dict[str, dict[str, Any]], by_id: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()

    for key in ("clause_uids", "related_clause_uids"):
        vals = risk.get(key)
        if isinstance(vals, list):
            for uid in vals:
                uid_s = str(uid or "").strip()
                if not uid_s:
                    continue
                clause = by_uid.get(uid_s)
                clause_uid = str((clause or {}).get("clause_uid") or "").strip()
                if clause and clause_uid and clause_uid not in seen:
                    seen.add(clause_uid)
                    resolved.append(clause)
    raw_uid = str(risk.get("clause_uid") or "").strip()
    if raw_uid:
        clause = by_uid.get(raw_uid)
        clause_uid = str((clause or {}).get("clause_uid") or "").strip()
        if clause and clause_uid and clause_uid not in seen:
            seen.add(clause_uid)
            resolved.append(clause)
    if resolved:
        return resolved

    refs: list[str] = []
    for key in ("clause_ids", "related_clause_ids", "display_clause_ids"):
        vals = risk.get(key)
        if isinstance(vals, list):
            refs.extend(str(v or "").strip() for v in vals if str(v or "").strip())
    for key in ("clause_id", "display_clause_id"):
        raw = str(risk.get(key) or "").strip()
        if raw:
            refs.extend([p.strip() for p in CLAUSE_REF_SPLIT_RE.split(raw) if p.strip()])

    for ref in refs:
        for clause in by_id.get(ref, []):
            uid = str(clause.get("clause_uid") or "").strip()
            if uid and uid not in seen:
                seen.add(uid)
                resolved.append(clause)
    return resolved


def _first_non_empty_text(values: list[Any]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""



def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _text_contains_candidate(text: str, candidate: str) -> bool:
    raw_text = str(text or "")
    raw_candidate = str(candidate or "")
    if not raw_candidate:
        return False
    if raw_candidate in raw_text:
        return True
    return _compact_text(raw_candidate) in _compact_text(raw_text)


def _collect_explicit_target_snippets(
    risk: dict[str, Any],
    *,
    include_revised_text: bool = False,
) -> list[tuple[str, str]]:
    accepted_patch = risk.get("accepted_patch") if isinstance(risk.get("accepted_patch"), dict) else {}
    ai_rewrite = risk.get("ai_rewrite") if isinstance(risk.get("ai_rewrite"), dict) else {}
    ai_apply = risk.get("ai_apply") if isinstance(risk.get("ai_apply"), dict) else {}

    ranked_sources: list[tuple[int, str, str]] = []
    if include_revised_text:
        ranked_sources.extend(
            [
                (0, str(accepted_patch.get("after_text") or "").strip(), "accepted_patch_after_text"),
                (1, str(ai_apply.get("revised_text") or "").strip(), "ai_apply_revised_text"),
                (1, str(ai_rewrite.get("revised_text") or "").strip(), "ai_rewrite_revised_text"),
            ]
        )
    ranked_sources.extend(
        [
            (2, str(accepted_patch.get("before_text") or "").strip(), "accepted_patch_before_text"),
            (3, str(ai_apply.get("target_text") or "").strip(), "ai_apply_target_text"),
            (3, str(ai_rewrite.get("target_text") or "").strip(), "ai_rewrite_target_text"),
            (4, str(risk.get("target_text") or "").strip(), "target_text"),
            # main_text is the primary clause excerpt selected by the pipeline.
            # For missing-clause / multi-clause risks this is often the most
            # precise single-clause anchor, while evidence_text may concatenate
            # several related clauses and therefore be unsuitable for paragraph
            # level DOCX matching.
            (5, str(risk.get("main_text") or "").strip(), "main_text"),
            (6, str(risk.get("evidence_text") or "").strip(), "evidence_text"),
            (7, str(risk.get("anchor_text") or "").strip(), "anchor_text"),
        ]
    )

    by_compact: dict[str, tuple[int, str, str]] = {}
    for rank, raw, strategy in ranked_sources:
        for snippet in _candidate_snippets(raw):
            compact = _compact_text(snippet)
            if not compact:
                continue
            prev = by_compact.get(compact)
            if prev is None or rank < prev[0] or (rank == prev[0] and len(compact) > len(_compact_text(prev[1]))):
                by_compact[compact] = (rank, snippet, strategy)

    ordered = sorted(by_compact.values(), key=lambda item: (item[0], -len(_compact_text(item[1]))))
    return [(snippet, strategy) for _rank, snippet, strategy in ordered]


def _pick_explicit_target_candidates(risk: dict[str, Any]) -> list[str]:
    return [snippet for snippet, _strategy in _collect_explicit_target_snippets(risk)]


def _resolve_risk_paragraph(
    paragraphs: list[ParagraphIndex],
    risk: dict[str, Any],
    clause_metas: list[dict[str, Any]] | None = None,
    *,
    allow_clause_fallback: bool = True,
    include_revised_text: bool = False,
) -> tuple[ParagraphIndex | None, str | None, str | None]:
    explicit_snippets = _collect_explicit_target_snippets(risk, include_revised_text=include_revised_text)
    explicit_candidates = [snippet for snippet, _strategy in explicit_snippets]

    locator_para = _resolve_locator_paragraph(paragraphs, risk)
    if locator_para is not None and explicit_candidates:
        matched = next((candidate for candidate in explicit_candidates if _text_contains_candidate(locator_para.text, candidate)), None)
        if matched:
            strategy = next((strategy for candidate, strategy in explicit_snippets if _normalize_ws(candidate) == _normalize_ws(matched)), "locator_validated")
            return locator_para, matched, strategy

    if explicit_candidates:
        para, matched = _find_first_paragraph_by_priority(paragraphs, explicit_candidates)
        if para is not None:
            strategy = next((strategy for candidate, strategy in explicit_snippets if _normalize_ws(candidate) == _normalize_ws(str(matched or ""))), "explicit_target")
            return para, matched, strategy

    if allow_clause_fallback:
        risk_source_type = str(risk.get("risk_source_type", "anchored") or "anchored").strip().lower()
        para, matched, strategy = _find_clause_fallback_paragraph(paragraphs, clause_metas, risk_source_type)
        if para is not None:
            return para, matched, strategy or "clause_fallback"

    if locator_para is not None and not explicit_candidates:
        locator = risk.get("locator") if isinstance(risk.get("locator"), dict) else {}
        matched = str(locator.get("matched_text") or "").strip() or _normalize_ws(locator_para.text)
        return locator_para, matched, "locator_only"

    return None, None, None


def _pick_suggestion_text(risk: dict[str, Any]) -> str:
    return _first_non_empty_text(
        [
            risk.get("suggestion"),
            risk.get("suggestion_optimized"),
            risk.get("suggestion_minimal"),
            risk.get("basis"),
        ]
    )



def _pick_basis_text(risk: dict[str, Any]) -> str:
    return _first_non_empty_text(
        [
            risk.get("basis_minimal"),
            risk.get("basis_summary"),
            risk.get("basis"),
        ]
    )


def _normalize_comment_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(text or "").strip())



def _build_comment_text(risk: dict[str, Any], clauses: list[dict[str, Any]]) -> str:
    accepted_patch = risk.get("accepted_patch") if isinstance(risk.get("accepted_patch"), dict) else {}
    accepted_kind = str(accepted_patch.get("kind") or "").strip().lower()
    accepted_comment_text = _normalize_comment_text(str(accepted_patch.get("comment_text") or ""))
    if accepted_kind == "suggest_insert" and accepted_comment_text:
        return accepted_comment_text

    issue = str(risk.get("issue") or risk.get("risk_label") or risk.get("title") or "").strip() or "—"
    basis = _pick_basis_text(risk) or "—"
    suggestion = _pick_suggestion_text(risk) or "—"
    suggestion_label = "【建议插入】" if accepted_kind == "suggest_insert" else "【已修改】"

    return "\n".join(
        [
            f"【问题】{issue}",
            f"【依据】{basis}",
            f"{suggestion_label}：{suggestion}",
        ]
    )

def _uses_single_anchor_comment(risk: dict[str, Any]) -> bool:
    accepted_patch = risk.get("accepted_patch") if isinstance(risk.get("accepted_patch"), dict) else {}
    accepted_kind = str(accepted_patch.get("kind") or "").strip().lower()
    export_mode = str(accepted_patch.get("export_mode") or accepted_patch.get("mode") or "").strip().lower()
    return accepted_kind == "suggest_insert" or export_mode in {"comment_only", "annotation_only"}


def _resolve_single_anchor_comment_paragraph(
    paragraphs: list[ParagraphIndex],
    risk: dict[str, Any],
    clause_metas: list[dict[str, Any]] | None = None,
) -> tuple[ParagraphIndex | None, str | None, str | None]:
    para, matched, strategy = _resolve_risk_paragraph(
        paragraphs,
        risk,
        clause_metas,
        allow_clause_fallback=True,
        include_revised_text=True,
    )
    if para is not None:
        return para, matched, strategy

    locator_para = _resolve_locator_paragraph(paragraphs, risk)
    if locator_para is not None:
        locator = risk.get("locator") if isinstance(risk.get("locator"), dict) else {}
        matched = str(locator.get("matched_text") or "").strip() or _normalize_ws(locator_para.text)
        return locator_para, matched, "locator_fallback"

    return None, None, None


def _is_included_status(status: str, include_statuses: tuple[str, ...]) -> bool:
    normalized = str(status or "").strip().lower()
    allowed = {str(x or "").strip().lower() for x in include_statuses}
    return normalized in allowed


def export_comments_to_docx(
    input_docx: Path,
    output_docx: Path,
    clauses_path: Path,
    risk_path: Path,
    author: str = "合同审查系统",
    include_statuses: tuple[str, ...] = ("pending", "accepted", "ai_applied"),
) -> dict[str, Any]:
    clauses = _unwrap_clauses(_load_json(clauses_path))
    risks = _unwrap_risk_payload(_load_json(risk_path))
    by_uid, by_id = _build_clause_indexes(clauses)

    with zipfile.ZipFile(input_docx, "r") as zin:
        overrides: dict[str, bytes] = {}
        doc_root = _read_xml(zin, "word/document.xml")
        comments_bytes = zin.read("word/comments.xml") if "word/comments.xml" in zin.namelist() else None
        comments_root = _ensure_comments_root(comments_bytes)

        ct_root = _read_xml(zin, "[Content_Types].xml")
        _ensure_content_types(ct_root)
        overrides["[Content_Types].xml"] = _xml_bytes(ct_root)

        rels_name = "word/_rels/document.xml.rels"
        rels_root = _read_xml(zin, rels_name)
        _ensure_document_rels(rels_root)
        overrides[rels_name] = _xml_bytes(rels_root)

        paragraphs = [
            ParagraphIndex(index=i, text=_paragraph_visible_text_for_match(p), element=p)
            for i, p in enumerate(doc_root.xpath(".//w:p", namespaces=NS))
        ]

        next_id = _next_comment_id(comments_root)
        added = []
        unmatched = []
        missing_clause_items = []
        touched = set()

        for risk in risks:
            status = str(risk.get("status") or "pending").lower()
            if not _is_included_status(status, include_statuses):
                continue
            risk_source_type = str(risk.get("risk_source_type", "anchored") or "anchored").strip().lower()
            clause_metas = _resolve_clauses_for_risk(risk, by_uid, by_id)
            comment_text = _build_comment_text(risk, clause_metas)
            single_anchor_comment = _uses_single_anchor_comment(risk)
            comment_targets = [None] if single_anchor_comment else (clause_metas if clause_metas else [None])
            risk_added = 0

            for clause in comment_targets:
                clause_scope = clause_metas if clause is None else [clause]
                if single_anchor_comment:
                    para, matched, _match_strategy = _resolve_single_anchor_comment_paragraph(
                        paragraphs,
                        risk,
                        clause_scope,
                    )
                else:
                    para, matched, _match_strategy = _resolve_risk_paragraph(
                        paragraphs,
                        risk,
                        clause_scope,
                        allow_clause_fallback=True,
                        include_revised_text=True,
                    )
                if para is None:
                    unmatched.append({
                        "risk_id": risk.get("risk_id"),
                        "clause_uid": clause.get("clause_uid") if clause else None,
                        "display_clause_id": clause.get("display_clause_id") if clause else risk.get("display_clause_id"),
                        "risk_label": risk.get("risk_label"),
                    })
                    continue
                dedup_key = (risk.get("risk_id"), para.index)
                if dedup_key in touched:
                    continue
                touched.add(dedup_key)
                cid = next_id
                next_id += 1
                _add_comment_to_paragraph(para.element, cid)
                _append_comment(comments_root, cid, comment_text, author=author)
                added.append({
                    "comment_id": cid,
                    "risk_id": risk.get("risk_id"),
                    "paragraph_index": para.index,
                    "matched_text": matched,
                    "clause_uid": clause.get("clause_uid") if clause else risk.get("clause_uid"),
                    "display_clause_id": clause.get("display_clause_id") if clause else risk.get("display_clause_id"),
                })
                risk_added += 1

            if risk_added == 0 and not clause_metas:
                unmatched.append({
                    "risk_id": risk.get("risk_id"),
                    "clause_uid": risk.get("clause_uid"),
                    "display_clause_id": risk.get("display_clause_id"),
                    "risk_label": risk.get("risk_label"),
                })

        overrides["word/document.xml"] = _xml_bytes(doc_root)
        overrides["word/comments.xml"] = _xml_bytes(comments_root)

        with zipfile.ZipFile(output_docx, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                name = info.filename
                if name in overrides:
                    zout.writestr(name, overrides[name])
                else:
                    zout.writestr(name, zin.read(name))
            if "word/comments.xml" not in zin.namelist():
                zout.writestr("word/comments.xml", overrides["word/comments.xml"])

    return {
        "output_docx": str(output_docx),
        "added_comments": len(added),
        "unmatched": unmatched,
        "comments": added,
        "missing_clause_skipped_count": len(missing_clause_items),
        "missing_clause_items": missing_clause_items,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Export DOCX with Word comments from normalized risk results")
    ap.add_argument("input_docx")
    ap.add_argument("clauses_json")
    ap.add_argument("risk_json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--author", default="合同审查系统")
    ap.add_argument("--statuses", default="pending,accepted,ai_applied")
    args = ap.parse_args()

    statuses = tuple(s.strip() for s in args.statuses.split(",") if s.strip())
    report = export_comments_to_docx(
        input_docx=Path(args.input_docx),
        output_docx=Path(args.out),
        clauses_path=Path(args.clauses_json),
        risk_path=Path(args.risk_json),
        author=args.author,
        include_statuses=statuses,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
