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
                }
            ]
        },
    }


class WebApiReviewedPatchTests(unittest.TestCase):
    def test_patch_creates_reviewed_and_sets_rejected(self):
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

            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                body = web_api.patch_risk_status("smoke_test_006", "101", web_api.RiskPatchBody(status="rejected"))
                self.assertTrue(body.get("ok"))
                self.assertEqual(body["item"]["status"], "rejected")

                reviewed_path = run_dir / "risk_result_reviewed.json"
                self.assertTrue(reviewed_path.exists())
                reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
                item = reviewed["risk_result"]["risk_items"][0]
                self.assertEqual(str(item["risk_id"]), "101")
                self.assertEqual(item["status"], "rejected")

    def test_patch_risk_not_found(self):
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

            with patch.object(web_api, "RUN_ROOT", run_root), patch.object(web_api, "UPLOAD_ROOT", upload_root), patch.object(
                web_api, "WEB_META_ROOT", meta_root
            ):
                with self.assertRaises(web_api.HTTPException) as ctx:
                    web_api.patch_risk_status("smoke_test_006", "999", web_api.RiskPatchBody(status="rejected"))
                self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
