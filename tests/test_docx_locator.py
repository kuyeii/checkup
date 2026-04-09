import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.docx_locator import enrich_reviewed_risks_with_locators, locate_risk


class DocxLocatorTests(unittest.TestCase):
    def test_locate_risk_prefers_evidence_text_when_hit(self):
        risk = {
            "risk_id": 1,
            "evidence_text": "乙方赔偿责任上限为合同总价20%。",
            "anchor_text": "赔偿责任上限",
            "clause_uids": ["segment_5::5.2"],
        }
        clauses = [
            {
                "clause_uid": "segment_5::5.2",
                "clause_id": "5.2",
                "display_clause_id": "5.2",
                "clause_title": "赔偿",
                "clause_text": "乙方赔偿责任上限为合同总价20%。",
            }
        ]
        paragraphs = [
            {"paragraph_index": 0, "text": "本合同总则"},
            {"paragraph_index": 1, "text": "乙方赔偿责任上限为合同总价20%。"},
        ]
        locator, target_text = locate_risk(risk, clauses, paragraphs)
        self.assertEqual(locator["paragraph_index"], 1)
        self.assertEqual(locator["match_strategy"], "evidence_text")
        self.assertEqual(target_text, "乙方赔偿责任上限为合同总价20%。")

    def test_enrich_reviewed_risks_with_locators_creates_reviewed_and_paragraph_dump(self):
        with tempfile.TemporaryDirectory() as td:
            run_root = Path(td) / "runs"
            run_id = "smoke_test_006"
            run_dir = run_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)

            (run_dir / "source.docx").write_bytes(b"not-used-because-build_paragraph_index-is-mocked")
            (run_dir / "merged_clauses.json").write_text(
                json.dumps(
                    [
                        {
                            "clause_uid": "segment_5::5.2",
                            "clause_id": "5.2",
                            "display_clause_id": "5.2",
                            "clause_title": "赔偿",
                            "clause_text": "乙方赔偿责任上限为合同总价20%。",
                        }
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            validated = {
                "is_valid": True,
                "error_message": "",
                "risk_result": {
                    "risk_items": [
                        {
                            "risk_id": 101,
                            "status": "pending",
                            "risk_source_type": "anchored",
                            "evidence_text": "乙方赔偿责任上限为合同总价20%。",
                            "anchor_text": "赔偿责任上限",
                            "clause_uids": ["segment_5::5.2"],
                        },
                        {
                            "risk_id": 102,
                            "status": "rejected",
                            "risk_source_type": "anchored",
                            "evidence_text": "应跳过",
                        },
                        {
                            "risk_id": 103,
                            "status": "pending",
                            "risk_source_type": "missing_clause",
                            "evidence_text": "应跳过",
                        },
                    ]
                },
            }
            (run_dir / "risk_result_validated.json").write_text(
                json.dumps(validated, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            with patch("src.docx_locator.build_paragraph_index", return_value=[{"paragraph_index": 7, "text": "乙方赔偿责任上限为合同总价20%。"}]):
                report = enrich_reviewed_risks_with_locators(run_id, run_root=run_root)

            self.assertEqual(report["located_success"], 1)
            self.assertTrue((run_dir / "risk_result_reviewed.json").exists())
            self.assertTrue((run_dir / "document_paragraphs.json").exists())

            reviewed = json.loads((run_dir / "risk_result_reviewed.json").read_text(encoding="utf-8"))
            items = reviewed["risk_result"]["risk_items"]
            item_101 = next(x for x in items if x["risk_id"] == 101)
            self.assertEqual(item_101["locator"]["paragraph_index"], 7)
            self.assertEqual(item_101["target_text"], "乙方赔偿责任上限为合同总价20%。")
            item_102 = next(x for x in items if x["risk_id"] == 102)
            item_103 = next(x for x in items if x["risk_id"] == 103)
            self.assertNotIn("locator", item_102)
            self.assertNotIn("locator", item_103)


if __name__ == "__main__":
    unittest.main()
