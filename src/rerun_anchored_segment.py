from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from config import settings
from src.file_utils import write_json
from src.merge_risk_results import merge_risk_results
from src.validate_risks import validate_risk_result
from src.workflow_runner import WorkflowRunner


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _clause_sort_key(clause_uid: str, clause_order: dict[str, int]) -> tuple[int, str]:
    return (clause_order.get(str(clause_uid or "").strip(), 10**9), str(clause_uid or ""))


def _sort_clause_records(records: list[dict[str, Any]], clause_order: dict[str, int]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda item: _clause_sort_key(str(item.get("clause_uid") or ""), clause_order))


def _sort_risk_items(items: list[dict[str, Any]], clause_order: dict[str, int]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            clause_order.get(str(item.get("clause_uid") or "").strip(), 10**9),
            str(item.get("risk_label") or ""),
            str(item.get("issue") or ""),
        ),
    )


def _backup_and_remove(path: Path, backup_path: Path) -> bool:
    if not path.exists():
        return False
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    path.unlink()
    return True


def rerun_anchored_segment(*, run_id: str, segment_id: str, user_id: str = "contract-review-poc") -> dict[str, Any]:
    settings.validate_for_live_call()

    run_dir = Path(settings.run_root) / run_id
    if not run_dir.exists():
        raise ValueError(f"run_id 不存在: {run_id}")

    merged_clauses_path = run_dir / "merged_clauses.json"
    merged_clauses = _load_json(merged_clauses_path)
    if not isinstance(merged_clauses, list):
        raise ValueError(f"缺少或无法解析: {merged_clauses_path}")

    segment_clauses = [clause for clause in merged_clauses if str(clause.get("segment_id") or "") == segment_id]
    if not segment_clauses:
        raise ValueError(f"未找到 segment_id={segment_id} 对应条款")

    clause_order = {
        str(clause.get("clause_uid") or "").strip(): idx
        for idx, clause in enumerate(merged_clauses)
        if str(clause.get("clause_uid") or "").strip()
    }
    segment_clause_uids = {
        str(clause.get("clause_uid") or "").strip()
        for clause in segment_clauses
        if str(clause.get("clause_uid") or "").strip()
    }
    segment_title = str(segment_clauses[0].get("segment_title") or "")
    segment_start_idx = min(clause_order.get(uid, 10**9) for uid in segment_clause_uids) if segment_clause_uids else 0

    runner = WorkflowRunner(settings=settings, run_dir=run_dir, user_id=user_id)
    rerun_result = runner.run_anchored_for_segment(
        segment_id=segment_id,
        segment_title=segment_title,
        clauses=segment_clauses,
        segment_start_idx=segment_start_idx,
    )

    rerun_debug_path = run_dir / "risk_checkpoints" / "anchored_segment_reruns" / f"{segment_id}.json"
    write_json(rerun_debug_path, rerun_result)

    if rerun_result.get("error"):
        error = dict(rerun_result.get("error") or {})
        raise RuntimeError(
            f"anchored segment 重跑失败: segment_id={segment_id}, "
            f"{error.get('error_type') or 'UnknownError'}: {error.get('error_message') or '未知错误'}"
        )

    outputs_bundle = _load_json(run_dir / "risk_result_outputs.json")
    if not isinstance(outputs_bundle, dict):
        outputs_bundle = {}
    anchored_outputs = dict(outputs_bundle.get("anchored") or {})
    missing_multi_outputs = dict(outputs_bundle.get("missing_multi") or {})

    next_by_clause = [
        item for item in (anchored_outputs.get("by_clause") or []) if str(item.get("clause_uid") or "").strip() not in segment_clause_uids
    ]
    next_by_clause.extend(list(rerun_result.get("by_clause_records") or []))
    next_by_clause = _sort_clause_records(next_by_clause, clause_order)

    next_skipped = [
        item for item in (anchored_outputs.get("skipped") or []) if str(item.get("clause_uid") or "").strip() not in segment_clause_uids
    ]
    next_skipped.extend(list(rerun_result.get("skipped") or []))
    next_skipped = _sort_clause_records(next_skipped, clause_order)

    next_errors = [
        item for item in (anchored_outputs.get("errors") or []) if str(item.get("segment_id") or "").strip() != segment_id
    ]

    next_outputs_bundle: dict[str, Any] = {
        "anchored": {
            "by_clause": next_by_clause,
            "skipped": next_skipped,
        },
        "missing_multi": missing_multi_outputs,
    }
    if next_errors:
        next_outputs_bundle["anchored"]["errors"] = next_errors

    raw_bundle = _load_json(run_dir / "risk_result_raw.json")
    if not isinstance(raw_bundle, dict):
        raw_bundle = {}
    anchored_payload = dict(raw_bundle.get("anchored") or {})
    missing_multi_payload = dict(raw_bundle.get("missing_multi") or {})

    next_anchored_items = [
        item for item in (anchored_payload.get("risk_items") or []) if str(item.get("clause_uid") or "").strip() not in segment_clause_uids
    ]
    next_anchored_items.extend(list(rerun_result.get("accepted_items") or []))
    next_anchored_items = _sort_risk_items(next_anchored_items, clause_order)
    next_anchored_payload = {"risk_items": next_anchored_items}

    unified_payload = merge_risk_results(
        anchored_payload=next_anchored_payload,
        missing_multi_payload=missing_multi_payload,
        clauses=merged_clauses,
    )
    is_valid, error_message = validate_risk_result(unified_payload)
    if not is_valid:
        raise RuntimeError(f"重建风险结果失败: {error_message}")

    next_raw_bundle = {
        "anchored": next_anchored_payload,
        "missing_multi": missing_multi_payload,
        "unified": unified_payload,
    }
    next_validated_payload = {
        "is_valid": is_valid,
        "error_message": error_message,
        "risk_result": unified_payload,
    }

    write_json(run_dir / "risk_result_outputs.json", next_outputs_bundle)
    write_json(run_dir / "risk_result_raw.json", next_raw_bundle)
    write_json(run_dir / "risk_result_normalized.json", unified_payload)
    write_json(run_dir / "risk_result_validated.json", next_validated_payload)

    reviewed_backup = run_dir / f"risk_result_reviewed.before_rerun_{segment_id}.json"
    reviewed_reset = _backup_and_remove(run_dir / "risk_result_reviewed.json", reviewed_backup)
    docx_backup = run_dir / f"reviewed_comments.before_rerun_{segment_id}.docx"
    docx_reset = _backup_and_remove(run_dir / "reviewed_comments.docx", docx_backup)

    return {
        "run_id": run_id,
        "segment_id": segment_id,
        "segment_title": segment_title,
        "segment_clause_count": len(segment_clauses),
        "accepted_risk_count": len(rerun_result.get("accepted_items") or []),
        "skipped_clause_count": len(rerun_result.get("skipped") or []),
        "reviewed_snapshot_reset": reviewed_reset,
        "docx_reset": docx_reset,
        "debug_path": str(rerun_debug_path),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rerun anchored risk detection for one segment inside an existing run")
    parser.add_argument("run_id", help="Existing run id under RUN_ROOT")
    parser.add_argument("segment_id", help="Segment id to rerun, e.g. segment_7")
    parser.add_argument("--user-id", default="contract-review-poc", help="Dify user identifier")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        summary = rerun_anchored_segment(run_id=args.run_id, segment_id=args.segment_id, user_id=args.user_id)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
