import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_api


def _validated_payload(status: str = "pending") -> dict:
    return {
        "is_valid": True,
        "error_message": "",
        "risk_result": {
            "risk_items": [
                {
                    "risk_id": 201,
                    "risk_label": "测试风险",
                    "issue": "测试问题",
                    "status": status,
                    "suggestion": "把条款改清晰",
                    "evidence_text": "乙方赔偿责任上限为合同总价20%。",
                    "anchor_text": "赔偿责任上限",
                    "clause_uids": ["segment_5::5.2"],
                }
            ]
        },
    }


class WebApiAiApplyTests(unittest.TestCase):
    def test_ai_apply_updates_reviewed_item(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
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
                        {
                            "clause_uid": "segment_5::5.2",
                            "source_excerpt": "乙方赔偿责任上限为合同总价20%。",
                            "clause_text": "乙方赔偿责任上限为合同总价20%。",
                        }
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

            fake_client = type(
                "FakeClient",
                (),
                {
                    "__init__": lambda self, **kwargs: None,
                    "run_workflow": lambda self, **kwargs: {
                        "data": {
                            "status": "succeeded",
                            "outputs": {
                                "structured_output": {
                                    "revised_text": "乙方累计赔偿责任上限不超过合同总价100%。",
                                    "rationale": "补充责任上限可降低争议风险",
                                    "edit_type": "replace_sentence",
                                }
                            },
                        }
                    },
                },
            )

            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"), patch.object(
                web_api, "DifyWorkflowClient", fake_client
            ):
                body = web_api.ai_apply_risk("smoke_test_006", "201")
                self.assertTrue(body.get("ok"))
                item = body["item"]
                self.assertEqual(item["status"], "pending")
                self.assertIn("ai_rewrite", item)
                self.assertEqual(item["ai_rewrite"]["state"], "succeeded")
                self.assertTrue(item["ai_rewrite"]["revised_text"])
                self.assertTrue(item["ai_rewrite"]["comment_text"])
                self.assertEqual(item["ai_rewrite_decision"], "proposed")

                reviewed = json.loads((run_dir / "risk_result_reviewed.json").read_text(encoding="utf-8"))
                updated = reviewed["risk_result"]["risk_items"][0]
                self.assertEqual(updated["status"], "pending")
                self.assertEqual(updated["ai_rewrite"]["state"], "succeeded")

    def test_ai_apply_rejected_risk_should_fail(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            run_root = base / "runs"
            upload_root = base / "uploads"
            meta_root = base / "meta"
            run_dir = run_root / "smoke_test_006"
            run_dir.mkdir(parents=True, exist_ok=True)
            upload_root.mkdir(parents=True, exist_ok=True)
            meta_root.mkdir(parents=True, exist_ok=True)

            (run_dir / "risk_result_validated.json").write_text(
                json.dumps(_validated_payload(status="rejected"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (run_dir / "merged_clauses.json").write_text("[]", encoding="utf-8")
            (meta_root / "smoke_test_006.json").write_text("{}", encoding="utf-8")

            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"):
                with self.assertRaises(web_api.HTTPException) as ctx:
                    web_api.ai_apply_risk("smoke_test_006", "201")
                self.assertEqual(ctx.exception.status_code, 409)

    def test_ai_apply_flat_outputs_still_supported(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
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
                        {
                            "clause_uid": "segment_5::5.2",
                            "source_excerpt": "乙方赔偿责任上限为合同总价20%。",
                            "clause_text": "乙方赔偿责任上限为合同总价20%。",
                        }
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

            fake_client = type(
                "FakeClientFlat",
                (),
                {
                    "__init__": lambda self, **kwargs: None,
                    "run_workflow": lambda self, **kwargs: {
                        "data": {
                            "status": "succeeded",
                            "outputs": {
                                "revised_text": "扁平输出改写文本",
                                "rationale": "扁平输出原因",
                                "edit_type": "replace_sentence",
                            },
                        }
                    },
                },
            )

            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"), patch.object(
                web_api, "DifyWorkflowClient", fake_client
            ):
                body = web_api.ai_apply_risk("smoke_test_006", "201")
                self.assertTrue(body.get("ok"))
                self.assertEqual(body["item"]["status"], "pending")
                self.assertEqual(body["item"]["ai_rewrite"]["revised_text"], "扁平输出改写文本")

    def test_ai_apply_allows_empty_revised_text_for_delete(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
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
                        {
                            "clause_uid": "segment_5::5.2",
                            "source_excerpt": "乙方赔偿责任上限为合同总价20%。",
                            "clause_text": "乙方赔偿责任上限为合同总价20%。",
                        }
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

            fake_client = type(
                "FakeClientDelete",
                (),
                {
                    "__init__": lambda self, **kwargs: None,
                    "run_workflow": lambda self, **kwargs: {
                        "data": {
                            "status": "succeeded",
                            "outputs": {
                                "structured_output": {
                                    "revised_text": "",
                                    "rationale": "删除该条款",
                                    "edit_type": "replace",
                                }
                            },
                        }
                    },
                },
            )

            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ), patch.object(web_api.settings, "dify_rewrite_workflow_api_key", "app-rewrite"), patch.object(
                web_api, "DifyWorkflowClient", fake_client
            ):
                body = web_api.ai_apply_risk("smoke_test_006", "201")
                self.assertTrue(body.get("ok"))
                self.assertEqual(body["item"]["ai_rewrite"]["state"], "succeeded")
                self.assertEqual(body["item"]["ai_rewrite"]["revised_text"], "")


if __name__ == "__main__":
    unittest.main()
