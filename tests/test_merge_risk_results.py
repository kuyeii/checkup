import unittest

from src.merge_risk_results import merge_risk_results
from src.validate_risks import validate_risk_result


CLAUSES = [
    {
        "clause_uid": "segment_5::5.2",
        "segment_id": "segment_5",
        "segment_title": "五、违约责任",
        "clause_id": "5.2",
        "display_clause_id": "5.2",
        "local_clause_id": "2",
        "source_clause_id": "5.2",
        "clause_title": "赔偿",
        "clause_text": "乙方赔偿责任上限为合同总价20%。",
        "clause_kind": "contract_clause",
        "source_excerpt": "乙方赔偿责任上限为合同总价20%。",
        "numbering_confidence": 0.9,
        "title_confidence": 0.9,
        "is_boilerplate_instruction": False,
    },
    {
        "clause_uid": "segment_5::5.5",
        "segment_id": "segment_5",
        "segment_title": "五、违约责任",
        "clause_id": "5.5",
        "display_clause_id": "5.5",
        "local_clause_id": "5",
        "source_clause_id": "5.5",
        "clause_title": "违约金",
        "clause_text": "逾期付款需支付违约金。",
        "clause_kind": "contract_clause",
        "source_excerpt": "逾期付款需支付违约金。",
        "numbering_confidence": 0.9,
        "title_confidence": 0.9,
        "is_boilerplate_instruction": False,
    },
]


class MergeRiskResultsTests(unittest.TestCase):
    def test_anchored_and_missing_and_multi_validate(self):
        anchored = {
            "risk_items": [
                {
                    "risk_id": "A1",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "赔偿责任上限过低",
                    "risk_level": "medium",
                    "issue": "赔偿上限偏低",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总价20%。",
                    "suggestion": "提高赔偿上限",
                    "clause_id": "5.2",
                    "clause_uid": "segment_5::5.2",
                    "clause_uids": ["segment_5::5.2"],
                    "anchor_text": "赔偿责任上限",
                    "risk_source_type": "anchored",
                }
            ]
        }
        missing_multi = {
            "risk_items": [
                {
                    "risk_id": "M1",
                    "dimension": "权责分配与责任限制",
                    "risk_label": "无乙方赔偿责任上限",
                    "risk_level": "low",
                    "issue": "合同未约定乙方累计赔偿责任上限",
                    "basis": "",
                    "basis_summary": "合同缺失责任上限条款",
                    "evidence_text": "合同中未找到明确的责任限制条款",
                    "suggestion": "补充责任上限",
                    "clause_id": "",
                    "anchor_text": "",
                    "risk_source_type": "missing_clause",
                },
                {
                    "risk_id": "M2",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "跨条款责任不一致",
                    "risk_level": "medium",
                    "issue": "5.2与5.5条款联动后责任口径冲突",
                    "basis": "",
                    "basis_summary": "两个条款的责任表达存在联动冲突",
                    "evidence_text": "需联合审阅",
                    "suggestion": "统一责任口径",
                    "clause_id": "5.2、5.5",
                    "anchor_text": "",
                    "risk_source_type": "multi_clause",
                    "related_clause_ids": ["5.2", "5.5"],
                    "related_clause_uids": ["segment_5::5.2", "segment_5::5.5"],
                },
            ]
        }
        merged = merge_risk_results(anchored_payload=anchored, missing_multi_payload=missing_multi, clauses=CLAUSES)
        ok, msg = validate_risk_result(merged)
        self.assertTrue(ok, msg)

    def test_invalid_multi_clause_should_fail(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": 1,
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "跨条款风险",
                    "risk_level": "medium",
                    "issue": "",
                    "basis": "",
                    "basis_summary": "",
                    "evidence_text": "",
                    "suggestion": "",
                    "clause_id": "",
                    "display_clause_id": "",
                    "anchor_text": "",
                    "needs_human_review": True,
                    "status": "pending",
                    "clause_uid": "",
                    "clause_uids": [],
                    "display_clause_ids": [],
                    "clause_ids": [],
                    "is_multi_clause_risk": True,
                    "basis_rule_id": "RULE_GENERAL_001",
                    "review_required_reason": ["POC阶段默认全量人工复核"],
                    "auto_apply_allowed": False,
                    "is_boilerplate_related": False,
                    "mapping_conflict": False,
                    "risk_source_type": "multi_clause",
                    "suggestion_minimal": "",
                    "suggestion_optimized": "",
                    "evidence_confidence": None,
                    "quality_flags": [],
                    "related_clause_ids": [],
                    "related_clause_uids": [],
                }
            ]
        }
        ok, msg = validate_risk_result(payload)
        self.assertFalse(ok)
        self.assertIn("related_clause_ids 或 related_clause_uids", msg)

    def test_dedupe_anchored(self):
        anchored = {
            "risk_items": [
                {
                    "risk_id": "A1",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "赔偿责任上限过低",
                    "risk_level": "medium",
                    "issue": "赔偿上限偏低",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总价20%。",
                    "suggestion": "提高赔偿上限",
                    "clause_id": "5.2",
                    "clause_uid": "segment_5::5.2",
                    "clause_uids": ["segment_5::5.2"],
                    "anchor_text": "赔偿责任上限",
                    "risk_source_type": "anchored",
                },
                {
                    "risk_id": "A2",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "赔偿责任上限过低",
                    "risk_level": "low",
                    "issue": "赔偿上限偏低",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总价20%。",
                    "suggestion": "提高赔偿上限",
                    "clause_id": "5.2",
                    "clause_uid": "segment_5::5.2",
                    "clause_uids": ["segment_5::5.2"],
                    "anchor_text": "赔偿责任上限",
                    "risk_source_type": "anchored",
                },
            ]
        }
        merged = merge_risk_results(anchored_payload=anchored, missing_multi_payload={"risk_items": []}, clauses=CLAUSES)
        self.assertEqual(len(merged["risk_items"]), 1)

    def test_dedupe_multi_clause(self):
        missing_multi = {
            "risk_items": [
                {
                    "risk_id": "M1",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "跨条款责任不一致",
                    "risk_level": "medium",
                    "issue": "联动风险",
                    "basis": "",
                    "basis_summary": "联动冲突",
                    "evidence_text": "需联合审阅",
                    "suggestion": "统一责任口径",
                    "risk_source_type": "multi_clause",
                    "related_clause_uids": ["segment_5::5.5", "segment_5::5.2"],
                },
                {
                    "risk_id": "M2",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "跨条款责任不一致",
                    "risk_level": "medium",
                    "issue": "联动风险",
                    "basis": "",
                    "basis_summary": "联动冲突",
                    "evidence_text": "需联合审阅",
                    "suggestion": "统一责任口径",
                    "risk_source_type": "multi_clause",
                    "related_clause_uids": ["segment_5::5.2", "segment_5::5.5"],
                },
            ]
        }
        merged = merge_risk_results(anchored_payload={"risk_items": []}, missing_multi_payload=missing_multi, clauses=CLAUSES)
        self.assertEqual(len(merged["risk_items"]), 1)

    def test_rule_callback_liability_cap_should_escalate(self):
        missing_multi = {
            "risk_items": [
                {
                    "risk_id": "R1",
                    "dimension": "权责分配与责任限制",
                    "risk_label": "无赔偿责任上限",
                    "risk_level": "low",
                    "issue": "合同未约定乙方累计赔偿责任上限",
                    "basis": "",
                    "basis_summary": "合同缺失责任上限条款",
                    "evidence_text": "未找到责任上限约定",
                    "suggestion": "补充上限",
                    "risk_source_type": "missing_clause",
                }
            ]
        }
        merged = merge_risk_results(anchored_payload={"risk_items": []}, missing_multi_payload=missing_multi, clauses=CLAUSES)
        self.assertEqual(merged["risk_items"][0]["risk_level"], "high")

    def test_risk_level_aliases_should_be_normalized_and_removed(self):
        anchored = {
            "risk_items": [
                {
                    "risk_id": "A1",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "赔偿责任上限过低",
                    "risk_level_level": "medium",
                    "issue": "赔偿上限偏低",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总价20%。",
                    "suggestion": "提高赔偿上限",
                    "clause_id": "5.2",
                    "clause_uid": "segment_5::5.2",
                    "clause_uids": ["segment_5::5.2"],
                    "anchor_text": "赔偿责任上限",
                    "risk_source_type": "anchored",
                },
                {
                    "risk_id": "A2",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "赔偿责任上限过高",
                    "risk_level_candidate": "VERY_HIGH",
                    "issue": "赔偿上限偏高",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总价200%。",
                    "suggestion": "降低赔偿上限",
                    "clause_id": "5.5",
                    "clause_uid": "segment_5::5.5",
                    "clause_uids": ["segment_5::5.5"],
                    "anchor_text": "赔偿责任上限",
                    "risk_source_type": "anchored",
                },
            ]
        }
        merged = merge_risk_results(anchored_payload=anchored, missing_multi_payload={"risk_items": []}, clauses=CLAUSES)
        self.assertEqual(len(merged["risk_items"]), 2)
        self.assertEqual(merged["risk_items"][0]["risk_level"], "medium")
        self.assertEqual(merged["risk_items"][1]["risk_level"], "medium")
        self.assertNotIn("risk_level_level", merged["risk_items"][0])
        self.assertNotIn("risk_level_candidate", merged["risk_items"][1])
        ok, msg = validate_risk_result(merged)
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()
