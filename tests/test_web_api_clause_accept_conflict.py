import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_api


def _make_validated_payload(risk_items: list[dict]) -> dict:
    return {
        "is_valid": True,
        "error_message": "",
        "risk_result": {
            "risk_items": risk_items,
        },
    }


def _make_clause_payload() -> list[dict]:
    return [
        {"clause_uid": "segment_1::1.1", "clause_id": "1.1", "display_clause_id": "第1.1条"},
        {"clause_uid": "segment_2::2.1", "clause_id": "2.1", "display_clause_id": "第2.1条"},
    ]


class WebApiClauseAcceptConflictTests(unittest.TestCase):
    def _setup_run(self, risk_items: list[dict]):
        td = tempfile.TemporaryDirectory()
        base = Path(td.name)
        run_root = base / "runs"
        upload_root = base / "uploads"
        meta_root = base / "meta"
        run_dir = run_root / "smoke_test_006"
        run_dir.mkdir(parents=True, exist_ok=True)
        upload_root.mkdir(parents=True, exist_ok=True)
        meta_root.mkdir(parents=True, exist_ok=True)
        (run_dir / "risk_result_validated.json").write_text(
            json.dumps(_make_validated_payload(risk_items), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "merged_clauses.json").write_text(
            json.dumps(_make_clause_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return td, run_root, upload_root, meta_root

    def test_patch_accept_is_allowed_when_same_clause_already_accepted(self):
        td, run_root, upload_root, meta_root = self._setup_run(
            [
                {"risk_id": 1, "status": "accepted", "clause_uids": ["segment_1::1.1"]},
                {"risk_id": 2, "status": "pending", "clause_uids": ["segment_1::1.1"]},
            ]
        )
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                body = web_api.patch_risk_status("smoke_test_006", "2", web_api.RiskPatchBody(status="accepted"))
                self.assertTrue(body.get("ok"))
                self.assertEqual(body["item"]["status"], "ai_applied")
        finally:
            td.cleanup()

    def test_ai_accept_is_allowed_when_same_clause_already_accepted(self):
        td, run_root, upload_root, meta_root = self._setup_run(
            [
                {"risk_id": 1, "status": "accepted", "clause_uids": ["segment_1::1.1"]},
                {
                    "risk_id": 2,
                    "status": "pending",
                    "clause_uids": ["segment_1::1.1"],
                    "ai_rewrite": {
                        "state": "succeeded",
                        "target_text": "原文",
                        "revised_text": "改写",
                        "comment_text": "说明",
                        "created_at": "2026-04-07T00:00:00Z",
                    },
                },
            ]
        )
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                body = web_api.ai_accept_risk("smoke_test_006", "2", web_api.AiAcceptBody())
                self.assertTrue(body.get("ok"))
                self.assertEqual(body["item"]["status"], "ai_applied")
                self.assertEqual(body["item"]["ai_rewrite_decision"], "accepted")
        finally:
            td.cleanup()

    def test_patch_accept_is_allowed_for_different_clause(self):
        td, run_root, upload_root, meta_root = self._setup_run(
            [
                {"risk_id": 1, "status": "accepted", "clause_uids": ["segment_1::1.1"]},
                {"risk_id": 2, "status": "pending", "clause_uids": ["segment_2::2.1"]},
            ]
        )
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                body = web_api.patch_risk_status("smoke_test_006", "2", web_api.RiskPatchBody(status="accepted"))
                self.assertTrue(body.get("ok"))
                self.assertEqual(body["item"]["status"], "accepted")
        finally:
            td.cleanup()

    def test_accept_all_accepts_multiple_pending_risks_in_same_clause(self):
        td, run_root, upload_root, meta_root = self._setup_run(
            [
                {"risk_id": 1, "status": "pending", "clause_uids": ["segment_1::1.1"]},
                {"risk_id": 2, "status": "pending", "clause_uids": ["segment_1::1.1"]},
            ]
        )
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                body = web_api.accept_all_risks("smoke_test_006")
                self.assertTrue(body.get("ok"))
                self.assertEqual(body["summary"], {"accepted": 2, "skipped": 0})
                statuses = {str(item["risk_id"]): item["status"] for item in body["risk_items"]}
                self.assertEqual(statuses, {"1": "accepted", "2": "accepted"})
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
