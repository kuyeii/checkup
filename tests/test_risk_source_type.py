import unittest

from src.normalize_risks import normalize_and_dedupe_risks
from src.validate_risks import validate_risk_result


CLAUSES = [
    {
        "clause_uid": "segment_10::10.1",
        "segment_id": "segment_10",
        "segment_title": "十、违约责任",
        "clause_id": "10.1",
        "display_clause_id": "10.1",
        "local_clause_id": "1",
        "source_clause_id": "10.1",
        "clause_title": "赔偿责任上限",
        "clause_text": "乙方赔偿责任上限为合同总额的20%。",
        "clause_kind": "contract_clause",
        "is_boilerplate_instruction": False,
    },
    {
        "clause_uid": "segment_10::10.2",
        "segment_id": "segment_10",
        "segment_title": "十、违约责任",
        "clause_id": "10.2",
        "display_clause_id": "10.2",
        "local_clause_id": "2",
        "source_clause_id": "10.2",
        "clause_title": "违约金条款",
        "clause_text": "逾期付款按日万分之五支付违约金。",
        "clause_kind": "contract_clause",
        "is_boilerplate_instruction": False,
    },
]


def _normalize(payload):
    return normalize_and_dedupe_risks(payload, CLAUSES)


class RiskSourceTypeTests(unittest.TestCase):
    def test_case_1_legacy_anchored_compat_should_pass(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": "R1",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "赔偿责任上限过低",
                    "risk_level": "medium",
                    "issue": "赔偿责任上限约定可能不足",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总额的20%。",
                    "suggestion": "提高上限",
                    "clause_id": "10.1",
                    "anchor_text": "乙方赔偿责任上限",
                    "status": "pending",
                }
            ]
        }
        normalized = _normalize(payload)
        item = normalized["risk_items"][0]
        self.assertEqual(item["risk_source_type"], "anchored")
        self.assertEqual(item["suggestion_minimal"], item["suggestion"])
        self.assertEqual(item["suggestion_optimized"], "")
        self.assertEqual(item["quality_flags"], [])
        self.assertEqual(item["related_clause_ids"], [item["clause_id"]])
        self.assertEqual(item["related_clause_uids"], item["clause_uids"])
        self.assertTrue(str(item["clause_uid"]).strip())
        self.assertTrue(item["clause_uids"])
        ok, msg = validate_risk_result(normalized)
        self.assertTrue(ok, msg)

    def test_suggestion_prefers_minimal(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": "S1",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "测试建议字段",
                    "risk_level": "medium",
                    "issue": "测试问题",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总额的20%。",
                    "suggestion": "",
                    "suggestion_minimal": "MIN",
                    "suggestion_optimized": "OPT",
                    "clause_id": "10.1",
                    "anchor_text": "乙方赔偿责任上限",
                    "status": "pending",
                }
            ]
        }
        normalized = _normalize(payload)
        item = normalized["risk_items"][0]
        self.assertEqual(item["suggestion"], "MIN")

    def test_suggestion_falls_back_to_optimized(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": "S2",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "测试建议字段",
                    "risk_level": "medium",
                    "issue": "测试问题",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总额的20%。",
                    "suggestion": "",
                    "suggestion_minimal": "",
                    "suggestion_optimized": "OPT",
                    "clause_id": "10.1",
                    "anchor_text": "乙方赔偿责任上限",
                    "status": "pending",
                }
            ]
        }
        normalized = _normalize(payload)
        item = normalized["risk_items"][0]
        self.assertEqual(item["suggestion"], "OPT")

    def test_suggestion_empty_when_minimal_and_optimized_empty(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": "S3",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "测试建议字段",
                    "risk_level": "medium",
                    "issue": "测试问题",
                    "basis": "",
                    "evidence_text": "乙方赔偿责任上限为合同总额的20%。",
                    "suggestion": "",
                    "suggestion_minimal": "",
                    "suggestion_optimized": "",
                    "clause_id": "10.1",
                    "anchor_text": "乙方赔偿责任上限",
                    "status": "pending",
                }
            ]
        }
        normalized = _normalize(payload)
        item = normalized["risk_items"][0]
        self.assertIn("suggestion", item)
        self.assertEqual(item["suggestion"], "")
        self.assertIsInstance(item["suggestion"], str)

    def test_case_1b_unmapped_text_should_not_be_auto_missing_clause(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": "R1B",
                    "dimension": "付款结算、发票与税费",
                    "risk_label": "付款条款表述不清",
                    "risk_level": "medium",
                    "issue": "",
                    "basis": "",
                    "evidence_text": "付款条款存在不明确之处",
                    "suggestion": "补充付款条件和触发标准",
                    "clause_id": "",
                    "anchor_text": "",
                    "status": "pending",
                }
            ]
        }
        normalized = _normalize(payload)
        item = normalized["risk_items"][0]
        self.assertEqual(item["risk_source_type"], "anchored")

    def test_case_2_missing_clause_should_pass(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": "R2",
                    "dimension": "权责分配与责任限制",
                    "risk_label": "无乙方赔偿责任上限",
                    "risk_level": "high",
                    "issue": "合同未约定乙方赔偿上限",
                    "basis": "",
                    "evidence_text": "合同中未找到明确的责任限制条款",
                    "suggestion": "补充责任上限条款",
                    "clause_id": "",
                    "anchor_text": "",
                    "clause_uid": "",
                    "clause_uids": [],
                    "risk_source_type": "missing_clause",
                    "status": "pending",
                }
            ]
        }
        normalized = _normalize(payload)
        item = normalized["risk_items"][0]
        self.assertEqual(item["risk_source_type"], "missing_clause")
        self.assertEqual(item["clause_uid"], "")
        self.assertEqual(item["clause_uids"], [])
        ok, msg = validate_risk_result(normalized)
        self.assertTrue(ok, msg)

    def test_missing_clause_anchor_text_should_fallback(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": "R2B",
                    "dimension": "权责分配与责任限制",
                    "risk_label": "无乙方赔偿责任上限",
                    "risk_level": "high",
                    "issue": "合同未约定乙方赔偿上限",
                    "basis": "",
                    "evidence_text": "合同中未找到明确的责任限制条款",
                    "suggestion": "补充责任上限条款",
                    "clause_id": "",
                    "anchor_text": "",
                    "clause_uid": "",
                    "clause_uids": [],
                    "related_clause_uids": ["segment_10::10.1"],
                    "risk_source_type": "missing_clause",
                    "status": "pending",
                }
            ]
        }
        normalized = _normalize(payload)
        item = normalized["risk_items"][0]
        self.assertIn("anchor_text", item)
        self.assertIsInstance(item["anchor_text"], str)
        self.assertTrue(item["anchor_text"].strip())
        self.assertIn(
            item["anchor_text"],
            {
                "合同中未找到明确的责任限制条款",
                "乙方赔偿责任上限为合同总额的20%。",
            },
        )

    def test_case_3_multi_clause_should_pass(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": "R3",
                    "dimension": "违约责任与赔偿机制",
                    "risk_label": "违约责任与罚则联动风险",
                    "risk_level": "medium",
                    "issue": "责任条款与罚则条款组合后可能导致违约后果失衡",
                    "basis": "",
                    "evidence_text": "需联动审阅 10.1 与 10.2 条款",
                    "suggestion": "统一责任口径并调整罚则",
                    "clause_id": "10.1、10.2",
                    "anchor_text": "10.1 与 10.2",
                    "risk_source_type": "multi_clause",
                    "related_clause_ids": ["10.1", "10.2"],
                    "related_clause_uids": ["segment_10::10.1", "segment_10::10.2"],
                    "status": "pending",
                }
            ]
        }
        normalized = _normalize(payload)
        item = normalized["risk_items"][0]
        self.assertEqual(item["risk_source_type"], "multi_clause")
        self.assertTrue(item["related_clause_ids"])
        self.assertTrue(item["related_clause_uids"])
        ok, msg = validate_risk_result(normalized)
        self.assertTrue(ok, msg)

    def test_case_4_invalid_empty_missing_clause_should_fail(self):
        payload = {
            "risk_items": [
                {
                    "risk_id": 1,
                    "dimension": "权责分配与责任限制",
                    "risk_label": "",
                    "risk_level": "medium",
                    "issue": "",
                    "basis": "",
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
                    "is_multi_clause_risk": False,
                    "basis_rule_id": "RULE_GENERAL_001",
                    "basis_summary": "",
                    "review_required_reason": ["POC阶段默认全量人工复核"],
                    "auto_apply_allowed": False,
                    "is_boilerplate_related": False,
                    "mapping_conflict": False,
                    "risk_source_type": "missing_clause",
                }
            ]
        }
        ok, msg = validate_risk_result(payload)
        self.assertFalse(ok)
        self.assertIn("缺失型风险必须包含", msg)


if __name__ == "__main__":
    unittest.main()
