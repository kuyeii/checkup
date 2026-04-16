from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

from .docx_comments import (
    CLAUSE_REF_SPLIT_RE,
    NS,
    ParagraphIndex,
    _build_clause_indexes,
    _candidate_snippets,
    _find_best_paragraph,
    _paragraph_text_for_match,
    _unwrap_clauses,
    _unwrap_risk_payload,
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_ws(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def build_paragraph_index(input_docx: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(input_docx, "r") as zin:
        doc_root = etree.fromstring(zin.read("word/document.xml"))
    paragraphs: list[dict[str, Any]] = []
    for i, p in enumerate(doc_root.xpath(".//w:p", namespaces=NS)):
        paragraphs.append(
            {
                "paragraph_index": i,
                "text": _paragraph_text_for_match(p),
            }
        )
    return paragraphs


def _resolve_related_clauses(
    risk: dict[str, Any],
    by_uid: dict[str, dict[str, Any]],
    by_id: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()

    for key in ("clause_uids", "related_clause_uids"):
        for uid in risk.get(key) or []:
            uid_s = str(uid or "").strip()
            if not uid_s:
                continue
            clause = by_uid.get(uid_s)
            if not clause:
                continue
            clause_uid = str(clause.get("clause_uid") or "").strip()
            if clause_uid and clause_uid not in seen:
                seen.add(clause_uid)
                resolved.append(clause)

    # Canonical clause_uids are already precise enough. Avoid broad ref-based
    # expansion afterwards, otherwise a top-level ref like "1" can accidentally
    # pull in unrelated sub-clauses whose source_clause_id is also "1" (e.g.
    # 5.1 / 8.1), which then poisons DOCX export locators.
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
            clause_uid = str(clause.get("clause_uid") or "").strip()
            if clause_uid and clause_uid not in seen:
                seen.add(clause_uid)
                resolved.append(clause)

    return resolved


def _pick_target_text(risk: dict[str, Any], paragraph_text: str, matched_text: str) -> str:
    evidence_text = str(risk.get("evidence_text") or "").strip()
    anchor_text = str(risk.get("anchor_text") or "").strip()
    para_norm = _normalize_ws(paragraph_text)
    evidence_norm = _normalize_ws(evidence_text)
    if evidence_norm and para_norm and evidence_norm in para_norm:
        return evidence_text
    if matched_text:
        return matched_text
    if anchor_text:
        return anchor_text
    return evidence_text


def _collect_locator_snippets(
    risk: dict[str, Any],
    related_clauses: list[dict[str, Any]],
    *,
    relaxed: bool = False,
) -> list[tuple[str, str]]:
    snippets_with_strategy: list[tuple[str, str]] = []
    for key, strategy in [
        (str(risk.get("evidence_text") or "").strip(), "evidence_text"),
        (str(risk.get("anchor_text") or "").strip(), "anchor_text"),
    ]:
        for sn in _candidate_snippets(key):
            snippets_with_strategy.append((sn, strategy))
    for clause in related_clauses:
        for raw, strategy in [
            (str(clause.get("clause_text") or "").strip(), "clause_text"),
            (str(clause.get("clause_title") or "").strip(), "clause_title"),
        ]:
            for sn in _candidate_snippets(raw):
                snippets_with_strategy.append((sn, strategy))
        if relaxed:
            for raw in [
                str(clause.get("display_clause_id") or "").strip(),
                str(clause.get("clause_id") or "").strip(),
                str(clause.get("source_clause_id") or "").strip(),
            ]:
                for sn in _candidate_snippets(raw):
                    snippets_with_strategy.append((sn, "clause_ref"))
    return snippets_with_strategy


def locate_risk(
    risk: dict[str, Any],
    clauses: list[dict[str, Any]],
    paragraphs: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    by_uid, by_id = _build_clause_indexes(clauses)
    related_clauses = _resolve_related_clauses(risk, by_uid, by_id)
    para_inputs = [
        ParagraphIndex(index=int(p.get("paragraph_index", 0)), text=str(p.get("text") or ""), element=None)
        for p in paragraphs
    ]

    snippets_with_strategy: list[tuple[str, str]] = []
    for key, strategy in [
        (str(risk.get("evidence_text") or "").strip(), "evidence_text"),
        (str(risk.get("anchor_text") or "").strip(), "anchor_text"),
    ]:
        for sn in _candidate_snippets(key):
            snippets_with_strategy.append((sn, strategy))
    for clause in related_clauses:
        for raw, strategy in [
            (str(clause.get("clause_text") or "").strip(), "clause_text"),
            (str(clause.get("clause_title") or "").strip(), "clause_title"),
        ]:
            for sn in _candidate_snippets(raw):
                snippets_with_strategy.append((sn, strategy))

    snippets = [x[0] for x in snippets_with_strategy]
    best_para, matched = _find_best_paragraph(para_inputs, snippets)
    if best_para is None:
        locator = {
            "paragraph_index": None,
            "matched_text": "",
            "match_strategy": "no_match",
            "confidence": 0.0,
        }
        return locator, _pick_target_text(risk, "", "")

    matched_text = str(matched or "")
    matched_strategy = "matched_text"
    for candidate, strategy in snippets_with_strategy:
        if _normalize_ws(candidate) == _normalize_ws(matched_text):
            matched_strategy = strategy
            break

    confidence_map = {
        "evidence_text": 0.95,
        "anchor_text": 0.85,
        "clause_text": 0.75,
        "clause_title": 0.65,
        "clause_ref": 0.55,
        "matched_text": 0.6,
    }
    confidence = confidence_map.get(matched_strategy, 0.6)
    locator = {
        "paragraph_index": best_para.index,
        "matched_text": matched_text,
        "match_strategy": matched_strategy,
        "confidence": confidence,
    }
    target_text = _pick_target_text(risk, best_para.text, matched_text)
    return locator, target_text


def get_or_create_reviewed_risks(run_id: str, run_root: Path) -> dict[str, Any]:
    run_dir = run_root / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run_id 不存在: {run_id}")
    reviewed_path = run_dir / "risk_result_reviewed.json"
    validated_path = run_dir / "risk_result_validated.json"
    if reviewed_path.exists():
        payload = _load_json(reviewed_path)
    else:
        if not validated_path.exists():
            raise FileNotFoundError(f"risk_result_validated.json 不存在: {validated_path}")
        shutil.copy2(validated_path, reviewed_path)
        payload = _load_json(reviewed_path)
    if not isinstance(payload, dict):
        raise ValueError("reviewed 风险 JSON 格式错误")
    return payload


def enrich_reviewed_risks_with_locators(run_id: str, run_root: Path = Path("data/runs")) -> dict[str, Any]:
    run_dir = run_root / run_id
    source_docx = run_dir / "source.docx"
    clauses_path = run_dir / "merged_clauses.json"
    paragraphs_path = run_dir / "document_paragraphs.json"
    reviewed_path = run_dir / "risk_result_reviewed.json"

    if not source_docx.exists():
        raise FileNotFoundError(f"source.docx 不存在: {source_docx}")
    if not clauses_path.exists():
        raise FileNotFoundError(f"merged_clauses.json 不存在: {clauses_path}")

    reviewed_payload = get_or_create_reviewed_risks(run_id, run_root=run_root)
    clauses = _unwrap_clauses(_load_json(clauses_path))
    paragraphs = build_paragraph_index(source_docx)
    _write_json(paragraphs_path, paragraphs)

    risks = _unwrap_risk_payload(reviewed_payload)
    success = 0
    failed = 0
    low_confidence = 0
    skipped = 0
    failures: list[dict[str, Any]] = []

    for risk in risks:
        if not isinstance(risk, dict):
            continue
        status = str(risk.get("status") or "pending").strip().lower()
        risk_source_type = str(risk.get("risk_source_type") or "anchored").strip().lower()
        if status == "rejected":
            skipped += 1
            continue

        locator, target_text = locate_risk(risk, clauses, paragraphs)
        risk["locator"] = locator
        # Keep the canonical patch target_text stable. Locator output is useful
        # for paragraph navigation and fallback matching, but should not rewrite
        # the risk's target span after review/AI patch generation.
        risk["locator_resolved_target_text"] = str(target_text or "")

        paragraph_index = locator.get("paragraph_index")
        confidence = float(locator.get("confidence") or 0.0)
        if paragraph_index is None:
            failed += 1
            if len(failures) < 5:
                failures.append(
                    {
                        "risk_id": risk.get("risk_id"),
                        "evidence_text": str(risk.get("evidence_text") or ""),
                    }
                )
            continue
        success += 1
        if confidence < 0.7:
            low_confidence += 1

    _write_json(reviewed_path, reviewed_payload)
    return {
        "run_id": run_id,
        "total_risks": len([r for r in risks if isinstance(r, dict)]),
        "located_success": success,
        "located_failed": failed,
        "low_confidence": low_confidence,
        "skipped_count": skipped,
        "failed_examples": failures,
        "reviewed_path": str(reviewed_path),
        "paragraphs_path": str(paragraphs_path),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build DOCX paragraph locator for reviewed risks")
    ap.add_argument("--run_id", required=True)
    ap.add_argument("--run_root", default="data/runs")
    args = ap.parse_args()

    report = enrich_reviewed_risks_with_locators(args.run_id, run_root=Path(args.run_root))
    print(f"run_id={report['run_id']}")
    print(
        "located_success={}/{} located_failed={} low_confidence={}".format(
            report["located_success"],
            report["total_risks"],
            report["located_failed"],
            report["low_confidence"],
        )
    )
    if report.get("skipped_count"):
        print(f"skipped_count={report['skipped_count']}")
    if report["failed_examples"]:
        print("failed_examples(top5):")
        for item in report["failed_examples"]:
            print(
                f"- risk_id={item.get('risk_id')} evidence_text={str(item.get('evidence_text') or '')[:120]}"
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
