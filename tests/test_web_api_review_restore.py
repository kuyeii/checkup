import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_api


def _validated_payload() -> dict:
    return {
        "is_valid": True,
        "error_message": "",
        "risk_result": {
            "risk_items": [
                {
                    "risk_id": 101,
                    "risk_label": "测试风险",
                    "issue": "测试问题",
                    "status": "pending",
                    "suggestion": "建议补充明确约定。",
                    "evidence_text": "甲方应在验收后付款。",
                    "anchor_text": "甲方应在验收后付款。",
                    "risk_source_type": "anchored",
                    "clause_uids": ["segment_1::1.1"],
                }
            ]
        },
    }


class WebApiReviewRestoreTests(unittest.TestCase):
    def _setup_run(self):
        td = tempfile.TemporaryDirectory()
        base = Path(td.name)
        run_root = base / "runs"
        upload_root = base / "uploads"
        meta_root = base / "meta"
        run_dir = run_root / "restore_case_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        upload_root.mkdir(parents=True, exist_ok=True)
        meta_root.mkdir(parents=True, exist_ok=True)

        (run_dir / "merged_clauses.json").write_text(
            json.dumps(
                [
                    {
                        "clause_uid": "segment_1::1.1",
                        "source_excerpt": "甲方应在验收后付款。",
                        "clause_text": "甲方应在验收后付款。",
                    }
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (run_dir / "risk_result_validated.json").write_text(
            json.dumps(_validated_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "source.docx").write_bytes(b"fake-docx")
        (meta_root / "restore_case_001.json").write_text(
            json.dumps(
                {
                    "run_id": "restore_case_001",
                    "status": "running",
                    "file_name": "测试合同.docx",
                    "review_side": "supplier",
                    "contract_type_hint": "service_agreement",
                    "step": "正在识别风险点",
                    "progress": 65,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return td, run_root, upload_root, meta_root

    def test_get_review_status_reconciles_stale_running_meta(self):
        td, run_root, upload_root, meta_root = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                meta = web_api.get_review_status("restore_case_001")
                self.assertEqual(meta["status"], "completed")
                self.assertEqual(meta["progress"], 100)
                self.assertEqual(meta["step"], "历史结果")
                self.assertEqual(meta["file_name"], "测试合同.docx")
        finally:
            td.cleanup()

    def test_get_review_result_works_when_meta_is_stale(self):
        td, run_root, upload_root, meta_root = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                payload = web_api.get_review_result("restore_case_001")
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(payload["run_id"], "restore_case_001")
                self.assertEqual(len(payload["risk_result_validated"]["risk_result"]["risk_items"]), 1)
        finally:
            td.cleanup()

    def test_get_review_result_exposes_download_url_without_prebuilt_docx(self):
        td, run_root, upload_root, meta_root = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                payload = web_api.get_review_result("restore_case_001")
                self.assertTrue(payload["download_ready"])
                self.assertEqual(payload["download_url"], "/api/reviews/restore_case_001/download")
                self.assertFalse((run_root / "restore_case_001" / "reviewed_comments.docx").exists())
        finally:
            td.cleanup()

    def test_history_list_uses_reconciled_completed_status(self):
        td, run_root, upload_root, meta_root = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                body = web_api.get_review_history(limit=30)
                self.assertEqual(len(body["items"]), 1)
                self.assertEqual(body["items"][0]["run_id"], "restore_case_001")
                self.assertEqual(body["items"][0]["status"], "completed")
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
