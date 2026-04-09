from __future__ import annotations

import json
import tempfile
import threading
import time
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import app


class FakeRunner:
    def __init__(self) -> None:
        self._split_lock = threading.Lock()
        self._anchored_lock = threading.Lock()
        self._split_active = 0
        self._anchored_active = 0
        self.split_peak_active_calls = 0
        self.anchored_peak_active_calls = 0

    def run_clause_splitter(self, segment: dict) -> list[dict]:
        with self._split_lock:
            self._split_active += 1
            self.split_peak_active_calls = max(self.split_peak_active_calls, self._split_active)
        try:
            time.sleep(0.05)
            segment_id = str(segment.get("segment_id") or "")
            return [
                {
                    "clause_uid": f"{segment_id}::1.1",
                    "segment_id": segment_id,
                    "segment_title": str(segment.get("segment_title") or ""),
                    "clause_id": "1.1",
                    "display_clause_id": "1.1",
                    "clause_title": "条款",
                    "clause_text": "正文",
                    "clause_kind": "contract_clause",
                    "source_excerpt": "正文",
                }
            ]
        finally:
            with self._split_lock:
                self._split_active -= 1

    def run_anchored_for_segment(self, *, segment_id: str, segment_title: str, clauses: list[dict], segment_start_idx: int):
        del segment_title
        with self._anchored_lock:
            self._anchored_active += 1
            self.anchored_peak_active_calls = max(self.anchored_peak_active_calls, self._anchored_active)
        try:
            time.sleep(0.05)
            by_clause_records = []
            accepted_items = []
            for clause in clauses:
                clause_uid = str(clause.get("clause_uid") or "")
                by_clause_records.append(
                    {
                        "clause_uid": clause_uid,
                        "input_payload": {"clause_uid": clause_uid},
                        "outputs": {"risk_items": []},
                        "normalized_items": [],
                        "dropped_items": [],
                        "validation_errors": [],
                    }
                )
                accepted_items.append({"risk_label": f"anchored_{clause_uid}"})
            return {
                "segment_id": segment_id,
                "segment_start_idx": segment_start_idx,
                "segment_end_idx": segment_start_idx,
                "outputs": {"risk_items": []},
                "by_clause_records": by_clause_records,
                "accepted_items": accepted_items,
                "error": None,
                "duration_seconds": 0.05,
                "skipped": [],
            }
        finally:
            with self._anchored_lock:
                self._anchored_active -= 1

    def run_risk_reviewer_missing_multi(self, clauses: list[dict]):
        return (
            {"normalized_items": [{"risk_label": f"missing_{len(clauses)}"}]},
            {"risk_items": [{"risk_label": f"missing_{len(clauses)}"}]},
        )


def test_clause_split_pipeline_parallel_and_output_shape():
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        (run_dir / "clauses").mkdir(parents=True, exist_ok=True)
        docx_path = run_dir / "input.docx"
        docx_path.write_bytes(b"fake")

        args = Namespace(
            docx_path=str(docx_path),
            run_id="run_pipeline",
            user_id="u",
            dry_run=False,
            resume=False,
        )

        fake_runner = FakeRunner()
        segments = [
            {"segment_id": "segment_1", "segment_title": "一", "segment_text": "A"},
            {"segment_id": "segment_2", "segment_title": "二", "segment_text": "B"},
            {"segment_id": "segment_3", "segment_title": "三", "segment_text": "C"},
        ]

        with patch.object(app, "build_arg_parser") as parser_mock, patch.object(
            app, "create_run_dir", return_value=run_dir
        ), patch.object(app, "extract_docx_text", return_value="text"), patch.object(
            app, "clean_contract_text", return_value="clean"
        ), patch.object(
            app,
            "split_into_segments",
            return_value={"segment_count": len(segments), "heading_style": "cn", "segments": segments},
        ), patch.object(
            app, "save_stage_outputs"
        ), patch.object(
            type(app.settings), "validate_for_live_call", return_value=None
        ), patch.object(
            app, "WorkflowRunner", return_value=fake_runner
        ), patch.object(
            app, "merge_risk_results", return_value={"risk_items": [{"risk_label": "merged"}]}
        ), patch.object(
            app, "validate_risk_result", return_value=(True, "")
        ):
            parser_mock.return_value.parse_args.return_value = args
            setattr(app.settings, "clause_split_max_concurrency", 3)
            setattr(app.settings, "dify_max_concurrency", 6)
            rc = app.main()

        assert rc == 0
        assert fake_runner.split_peak_active_calls >= 2
        assert fake_runner.anchored_peak_active_calls >= 2

        validated = json.loads((run_dir / "risk_result_validated.json").read_text(encoding="utf-8"))
        assert set(validated.keys()) == {"is_valid", "error_message", "risk_result"}
        assert isinstance(validated["risk_result"], dict)
        assert "risk_items" in validated["risk_result"]
