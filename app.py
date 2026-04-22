from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from config import settings
from src.checkpoint import load_existing_clause_batch
from src.clean_text import clean_contract_text
from src.extract_docx import extract_docx_text
from src.file_utils import ensure_dir, write_json, write_text
from src.merge_clauses import merge_clause_batches
from src.merge_risk_results import merge_risk_results
from src.normalize_clauses import normalize_clause_records, normalize_clauses
from src.split_segments import split_into_segments
from src.validate_risks import validate_risk_result
from src.workflow_runner import WorkflowRunner
from src.analysis_scope import apply_analysis_scope, normalize_analysis_scope


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Contract review POC controller")
    parser.add_argument("docx_path", help="Path to DOCX contract")
    parser.add_argument("--run-id", default="", help="Optional run id; defaults to timestamp")
    parser.add_argument("--user-id", default="contract-review-poc", help="Dify user identifier")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Dify; only extract, clean and split")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from saved per-segment clause outputs when available",
    )
    return parser


def create_run_dir(run_id: str) -> Path:
    if not run_id:
        run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    run_dir = ensure_dir(settings.run_root / run_id)
    ensure_dir(run_dir / "clauses")
    return run_dir


def save_stage_outputs(run_dir: Path, extracted_text: str, cleaned_text: str, segment_bundle: dict[str, Any]) -> None:
    write_text(run_dir / "extracted_text.txt", extracted_text)
    write_text(run_dir / "cleaned_text.txt", cleaned_text)
    write_json(run_dir / "segments.json", segment_bundle)


def main() -> int:
    args = build_arg_parser().parse_args()
    docx_path = Path(args.docx_path)
    if not docx_path.exists():
        print(f"DOCX not found: {docx_path}", file=sys.stderr)
        return 2

    run_dir = create_run_dir(args.run_id)

    print("[1/6] Extracting DOCX text...")
    extracted_text = extract_docx_text(docx_path)

    print("[2/6] Cleaning text...")
    cleaned_text = clean_contract_text(extracted_text)

    print("[3/6] Splitting top-level segments...")
    segment_bundle = split_into_segments(cleaned_text)
    save_stage_outputs(run_dir, extracted_text, cleaned_text, segment_bundle)
    print(f"Segments: {segment_bundle['segment_count']} | heading_style={segment_bundle['heading_style']}")

    if args.dry_run:
        print(f"Dry run complete. Outputs saved under: {run_dir}")
        return 0

    settings.validate_for_live_call()
    runner = WorkflowRunner(settings=settings, run_dir=run_dir, user_id=args.user_id)

    merged_clauses_path = run_dir / "merged_clauses.json"
    merged_clauses: list[dict[str, Any]]
    anchored_outputs_prefetched: dict[str, Any] | None = None
    anchored_payload_prefetched: dict[str, Any] | None = None
    if args.resume and merged_clauses_path.exists():
        print("[4/6] Resume mode: loading existing merged clauses...")
        merged_clauses = json.loads(merged_clauses_path.read_text(encoding="utf-8"))
    elif args.resume:
        print("[4/6] Running clause splitter workflow for each segment...")
        clause_batches: list[list[dict[str, Any]]] = []
        for segment in segment_bundle["segments"]:
            print(f"  - {segment['segment_id']} {segment['segment_title']}")
            existing_path = run_dir / "clauses" / f"{segment['segment_id']}.json"
            clauses = load_existing_clause_batch(existing_path)
            if clauses is not None:
                print(f"    resumed from {existing_path.name} ({len(clauses)} clauses)")
            if clauses is None:
                clauses = runner.run_clause_splitter(segment)
            clause_batches.append(clauses)

        raw_merged_clauses = merge_clause_batches(clause_batches)
        raw_merged_clauses = normalize_clause_records(raw_merged_clauses)
        write_json(run_dir / "merged_clauses_raw.json", raw_merged_clauses)
        merged_clauses = normalize_clauses(raw_merged_clauses)
        write_json(run_dir / "merged_clauses.json", merged_clauses)
    else:
        print("[4/6] Running clause splitter in parallel with anchored pipeline...")
        segments = list(segment_bundle["segments"])
        segment_order_index = {str(seg.get("segment_id") or ""): idx for idx, seg in enumerate(segments)}
        clause_batches_map: dict[str, list[dict[str, Any]]] = {}
        anchored_segment_results: list[dict[str, Any]] = []

        def run_clause_splitter_for_segment(segment: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
            clauses = runner.run_clause_splitter(segment)
            return str(segment.get("segment_id") or ""), str(segment.get("segment_title") or ""), clauses

        with ThreadPoolExecutor(max_workers=max(1, int(settings.clause_split_max_concurrency))) as clause_executor, ThreadPoolExecutor(
            max_workers=max(1, int(settings.dify_max_concurrency))
        ) as anchored_executor:
            clause_future_map = {
                clause_executor.submit(run_clause_splitter_for_segment, segment): segment for segment in segments
            }
            anchored_future_map: dict[Any, str] = {}

            for clause_future in as_completed(clause_future_map):
                segment_id, segment_title, clauses = clause_future.result()
                print(f"  - clause done {segment_id} ({len(clauses)} clauses), submit anchored")
                clause_batches_map[segment_id] = clauses

                anchored_clauses = normalize_clauses(normalize_clause_records(list(clauses)))
                anchored_future = anchored_executor.submit(
                    runner.run_anchored_for_segment,
                    segment_id=segment_id,
                    segment_title=segment_title,
                    clauses=anchored_clauses,
                    segment_start_idx=segment_order_index.get(segment_id, 0),
                )
                anchored_future_map[anchored_future] = segment_id

            for anchored_future in as_completed(anchored_future_map):
                anchored_segment_results.append(anchored_future.result())

        clause_batches: list[list[dict[str, Any]]] = []
        for segment in segments:
            sid = str(segment.get("segment_id") or "")
            if sid not in clause_batches_map:
                raise RuntimeError(f"Missing clause batch for segment: {sid}")
            clause_batches.append(clause_batches_map[sid])
        raw_merged_clauses = merge_clause_batches(clause_batches)
        raw_merged_clauses = normalize_clause_records(raw_merged_clauses)
        write_json(run_dir / "merged_clauses_raw.json", raw_merged_clauses)
        merged_clauses = normalize_clauses(raw_merged_clauses)
        write_json(run_dir / "merged_clauses.json", merged_clauses)

        anchored_by_clause: list[dict[str, Any]] = []
        anchored_skipped: list[dict[str, Any]] = []
        anchored_risk_items: list[dict[str, Any]] = []
        anchored_errors: list[dict[str, Any]] = []
        segment_results_summary: list[dict[str, Any]] = []
        for result in sorted(anchored_segment_results, key=lambda item: int(item.get("segment_start_idx", 0))):
            anchored_by_clause.extend(list(result.get("by_clause_records") or []))
            anchored_skipped.extend(list(result.get("skipped") or []))
            anchored_risk_items.extend(list(result.get("accepted_items") or []))
            summary_item = {
                "segment_id": str(result.get("segment_id") or ""),
                "segment_start_idx": int(result.get("segment_start_idx") or 0),
                "status": "ok" if not result.get("error") else "error",
                "duration_seconds": float(result.get("duration_seconds") or 0.0),
                "risk_item_count": len(result.get("accepted_items") or []),
                "by_clause_count": len(result.get("by_clause_records") or []),
            }
            segment_results_summary.append(summary_item)
            if result.get("error"):
                anchored_errors.append(
                    {
                        "segment_id": str(result.get("segment_id") or ""),
                        "segment_start_idx": int(result.get("segment_start_idx") or 0),
                        **dict(result.get("error") or {}),
                    }
                )

        write_json(
            run_dir / "risk_checkpoints" / "anchored_pipeline_state.json",
            {
                "version": 1,
                "clause_split_max_concurrency": int(settings.clause_split_max_concurrency),
                "dify_max_concurrency": int(settings.dify_max_concurrency),
                "segment_results_summary": segment_results_summary,
                "errors": anchored_errors,
            },
        )
        if anchored_errors:
            print(f"{len(anchored_errors)} anchored segments failed in pipeline")
        anchored_outputs_prefetched = {"by_clause": anchored_by_clause, "skipped": anchored_skipped}
        if anchored_errors:
            anchored_outputs_prefetched["errors"] = anchored_errors
        anchored_payload_prefetched = {"risk_items": anchored_risk_items}
    print(f"Merged clauses: {len(merged_clauses)}")

    print("[5/6] Running risk reviewer workflow...")
    if args.resume or anchored_outputs_prefetched is None or anchored_payload_prefetched is None:
        risk_stream_payloads = runner.run_risk_reviewers(merged_clauses, resume=args.resume)
    else:
        missing_multi_outputs, missing_multi_payload = runner.run_risk_reviewer_missing_multi(merged_clauses)
        write_json(
            run_dir / "risk_result_outputs.json",
            {
                "anchored": anchored_outputs_prefetched,
                "missing_multi": missing_multi_outputs,
            },
        )
        risk_stream_payloads = {
            "anchored": anchored_payload_prefetched,
            "missing_multi": missing_multi_payload,
        }
    normalized_risk_payload = merge_risk_results(
        anchored_payload=risk_stream_payloads.get("anchored", {}),
        missing_multi_payload=risk_stream_payloads.get("missing_multi", {}),
        clauses=merged_clauses,
    )
    analysis_scope = normalize_analysis_scope(getattr(settings, "analysis_scope", "full_detail"))
    scoped_risk_payload = apply_analysis_scope(normalized_risk_payload, analysis_scope)
    write_json(
        run_dir / "risk_result_raw.json",
        {
            "anchored": risk_stream_payloads.get("anchored", {}),
            "missing_multi": risk_stream_payloads.get("missing_multi", {}),
            "unified": normalized_risk_payload,
            "scoped": scoped_risk_payload,
            "analysis_scope": analysis_scope,
        },
    )
    write_json(run_dir / "risk_result_normalized.full.json", normalized_risk_payload)
    write_json(run_dir / "risk_result_normalized.json", scoped_risk_payload)

    print("[6/6] Validating risk result...")
    is_valid, error_message = validate_risk_result(scoped_risk_payload)
    validated = {
        "is_valid": is_valid,
        "error_message": error_message,
        "risk_result": scoped_risk_payload,
        "analysis_scope": analysis_scope,
    }
    write_json(run_dir / "risk_result_validated.json", validated)

    if is_valid:
        print(f"Run complete. Outputs saved under: {run_dir}")
        return 0

    print(f"Risk result validation failed: {error_message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
