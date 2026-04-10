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
                    "risk_id": 1,
                    "risk_label": "A",
                    "issue": "i1",
                    "status": "pending",
                    "suggestion": "s1",
                    "evidence_text": "原文A",
                    "anchor_text": "原文A",
                    "risk_source_type": "anchored",
                    "clause_uids": ["segment_1::1.1"],
                },
                {
                    "risk_id": 2,
                    "risk_label": "B",
                    "issue": "i2",
                    "status": "pending",
                    "suggestion": "s2",
                    "evidence_text": "原文B",
                    "anchor_text": "原文B",
                    "risk_source_type": "anchored",
                    "clause_uids": ["segment_2::2.1"],
                },
            ]
        },
    }


class _FakeClient:
    def __init__(self, **kwargs):
        del kwargs

    def run_workflow(self, **kwargs):
        target_text = str((kwargs.get("inputs") or {}).get("target_text") or "").strip()
        return {
            "data": {
                "status": "succeeded",
                "outputs": {
                    "structured_output": {
                        "revised_text": f"{target_text}（改）",
                        "rationale": "ok",
                        "edit_type": "replace_sentence",
                    }
                },
            }
        }


class WebApiAiFlowTests(unittest.TestCase):
    def _setup_run(self):
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
            json.dumps(_validated_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "merged_clauses.json").write_text(
            json.dumps(
                [
                    {"clause_uid": "segment_1::1.1", "source_excerpt": "原文A", "clause_text": "原文A"},
                    {"clause_uid": "segment_2::2.1", "source_excerpt": "原文B", "clause_text": "原文B"},
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (meta_root / "smoke_test_006.json").write_text(
            json.dumps(
                {
                    "run_id": "smoke_test_006",
                    "status": "completed",
                    "review_side": "supplier",
                    "contract_type_hint": "service_agreement",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return td, run_root, upload_root, meta_root, run_dir

    def test_ai_apply_all_generates_rewrite_for_all_items(self):
        td, run_root, upload_root, meta_root, run_dir = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"), patch.object(
                web_api, "DifyWorkflowClient", _FakeClient
            ):
                body = web_api.ai_apply_all_risks("smoke_test_006")
                self.assertTrue(body.get("ok"))
                self.assertEqual(body["summary"]["created"], 2)
                reviewed = json.loads((run_dir / "risk_result_reviewed.json").read_text(encoding="utf-8"))
                items = reviewed["risk_result"]["risk_items"]
                self.assertEqual(items[0]["ai_rewrite"]["state"], "succeeded")
                self.assertEqual(items[1]["ai_rewrite"]["state"], "succeeded")
                self.assertEqual(items[0]["ai_rewrite_decision"], "proposed")
        finally:
            td.cleanup()

    def test_ai_accept_requires_rewrite_and_sets_ai_applied(self):
        td, run_root, upload_root, meta_root, run_dir = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"), patch.object(
                web_api, "DifyWorkflowClient", _FakeClient
            ):
                with self.assertRaises(web_api.HTTPException):
                    web_api.ai_accept_risk("smoke_test_006", "1", web_api.AiAcceptBody())
                web_api.ai_apply_risk("smoke_test_006", "1")
                body = web_api.ai_accept_risk("smoke_test_006", "1", web_api.AiAcceptBody())
                self.assertEqual(body["item"]["status"], "ai_applied")
                self.assertEqual(body["item"]["ai_rewrite_decision"], "accepted")
                reviewed = json.loads((run_dir / "risk_result_reviewed.json").read_text(encoding="utf-8"))
                self.assertEqual(reviewed["risk_result"]["risk_items"][0]["status"], "ai_applied")
        finally:
            td.cleanup()

    def test_ai_edit_updates_revised_text_and_comment(self):
        td, run_root, upload_root, meta_root, run_dir = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"), patch.object(
                web_api, "DifyWorkflowClient", _FakeClient
            ):
                web_api.ai_apply_risk("smoke_test_006", "1")
                body = web_api.ai_edit_risk("smoke_test_006", "1", web_api.AiEditBody(revised_text="用户手改文本"))
                self.assertEqual(body["item"]["ai_rewrite"]["revised_text"], "用户手改文本")
                self.assertIn("修改为", body["item"]["ai_rewrite"]["comment_text"])
        finally:
            td.cleanup()

    def test_ai_edit_allows_empty_revised_text_for_delete(self):
        td, run_root, upload_root, meta_root, run_dir = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"), patch.object(
                web_api, "DifyWorkflowClient", _FakeClient
            ):
                web_api.ai_apply_risk("smoke_test_006", "1")
                body = web_api.ai_edit_risk("smoke_test_006", "1", web_api.AiEditBody(revised_text=""))
                self.assertEqual(body["item"]["ai_rewrite"]["revised_text"], "")
                self.assertEqual(body["item"]["ai_rewrite"]["comment_text"], "删除“原文A”。")
        finally:
            td.cleanup()

    def test_ai_reject_clears_ai_rewrite(self):
        td, run_root, upload_root, meta_root, run_dir = self._setup_run()
        try:
            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"), patch.object(
                web_api, "DifyWorkflowClient", _FakeClient
            ):
                web_api.ai_apply_risk("smoke_test_006", "1")
                body = web_api.ai_reject_risk("smoke_test_006", "1")
                self.assertNotIn("ai_rewrite", body["item"])
                self.assertEqual(body["item"]["ai_rewrite_decision"], "rejected")
        finally:
            td.cleanup()

    def test_get_or_create_reviewed_risks_sanitizes_segment_prefixed_ai_target(self):
        td, run_root, upload_root, meta_root, run_dir = self._setup_run()
        try:
            reviewed = _validated_payload()
            reviewed_item = reviewed["risk_result"]["risk_items"][0]
            reviewed_item["risk_source_type"] = "multi_clause"
            reviewed_item["ai_rewrite"] = {
                "state": "succeeded",
                "target_text": "segment_7::7.1约定「由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有」，但未区分乙方原有知识产权与项目衍生成果",
                "revised_text": "由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有，但乙方在项目实施前已拥有的知识产权仍归乙方所有。",
                "comment_text": "将“segment_7::7.1约定...”修改为“...”。",
                "created_at": "2026-03-20T05:00:00Z",
            }
            (run_dir / "risk_result_reviewed.json").write_text(
                json.dumps(reviewed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                payload = web_api.get_or_create_reviewed_risks("smoke_test_006")
                ai = payload["risk_result"]["risk_items"][0]["ai_rewrite"]
                self.assertEqual(ai["target_text"], "由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有")
                self.assertNotIn("segment_", ai["comment_text"])
                on_disk = json.loads((run_dir / "risk_result_reviewed.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    on_disk["risk_result"]["risk_items"][0]["ai_rewrite"]["target_text"],
                    "由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有",
                )
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
