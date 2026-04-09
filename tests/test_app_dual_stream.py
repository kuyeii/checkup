import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import app


class AppDualStreamTests(unittest.TestCase):
    def test_main_merges_anchored_and_missing_multi_streams(self):
        with tempfile.TemporaryDirectory() as td:
            docx_path = Path(td) / "input.docx"
            docx_path.write_bytes(b"fake")

            args = Namespace(
                docx_path=str(docx_path),
                run_id="run_test",
                user_id="u",
                dry_run=False,
                resume=False,
            )

            fake_runner = MagicMock()
            fake_runner.run_clause_splitter.return_value = [
                {
                    "clause_uid": "segment_1::1.1",
                    "clause_id": "1.1",
                    "display_clause_id": "1.1",
                    "clause_text": "甲乙双方应遵守本协议。",
                    "segment_id": "segment_1",
                }
            ]
            fake_runner.run_anchored_for_segment.return_value = {
                "segment_id": "segment_1",
                "segment_start_idx": 0,
                "by_clause_records": [],
                "accepted_items": [{"risk_label": "A"}],
                "skipped": [],
                "error": None,
                "duration_seconds": 0.01,
            }
            fake_runner.run_risk_reviewer_missing_multi.return_value = (
                {"normalized_items": [{"risk_label": "M"}]},
                {"risk_items": [{"risk_label": "M"}]},
            )

            with patch.object(app, "build_arg_parser") as parser_mock, patch.object(
                app, "create_run_dir", return_value=Path(td)
            ), patch.object(app, "extract_docx_text", return_value="text"), patch.object(
                app, "clean_contract_text", return_value="clean"
            ), patch.object(
                app,
                "split_into_segments",
                return_value={"segment_count": 1, "heading_style": "cn", "segments": [{"segment_id": "segment_1", "segment_title": "一", "segment_text": "内容"}]},
            ), patch.object(
                app, "save_stage_outputs"
            ), patch.object(
                type(app.settings), "validate_for_live_call", return_value=None
            ), patch.object(
                app, "WorkflowRunner", return_value=fake_runner
            ), patch.object(
                app, "load_existing_clause_batch", return_value=None
            ), patch.object(
                app, "merge_clause_batches", return_value=fake_runner.run_clause_splitter.return_value
            ), patch.object(
                app, "normalize_clause_records", side_effect=lambda x: x
            ), patch.object(
                app, "normalize_clauses", side_effect=lambda x: x
            ), patch.object(
                app, "write_json"
            ), patch.object(
                app, "validate_risk_result", return_value=(True, "")
            ), patch.object(
                app, "merge_risk_results", return_value={"risk_items": [{"risk_label": "U"}]}
            ) as merge_mock:
                parser_mock.return_value.parse_args.return_value = args
                rc = app.main()

            self.assertEqual(rc, 0)
            merge_mock.assert_called_once()
            called_kwargs = merge_mock.call_args.kwargs
            self.assertEqual(called_kwargs["anchored_payload"], {"risk_items": [{"risk_label": "A"}]})
            self.assertEqual(called_kwargs["missing_multi_payload"], {"risk_items": [{"risk_label": "M"}]})


if __name__ == "__main__":
    unittest.main()
