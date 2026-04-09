import unittest

from src.anchored_postprocess import postprocess_anchored_risk_items


INPUT_PAYLOAD = {
    "clause_uid": "segment_7::7.1",
    "clause_id": "7.1",
    "display_clause_id": "7.1",
    "source_excerpt": "由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有。",
    "clause_text": "由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有。",
}


class AnchoredPostprocessTests(unittest.TestCase):
    def test_anchored_item_should_be_accepted(self):
        raw_items = [
            {
                "risk_source_type": "anchored",
                "risk_label": "知识产权归属不利",
                "issue": "成果所有权全部归甲方，乙方使用权不明确",
                "evidence_text": "所有权均属甲方所有",
                "factual_basis": "条款明确所有权均归甲方",
                "reasoning_basis": "归属失衡会影响乙方复用能力",
            }
        ]
        out = postprocess_anchored_risk_items(raw_items=raw_items, input_payload=INPUT_PAYLOAD)
        self.assertEqual(len(out["accepted_items"]), 1)
        item = out["accepted_items"][0]
        self.assertEqual(item["clause_uid"], INPUT_PAYLOAD["clause_uid"])
        self.assertEqual(item["clause_uids"], [INPUT_PAYLOAD["clause_uid"]])
        self.assertEqual(item["risk_source_type"], "anchored")

    def test_non_anchored_should_be_dropped(self):
        raw_items = [
            {
                "risk_source_type": "missing_clause",
                "risk_label": "缺失赔偿上限",
                "issue": "未约定上限",
                "evidence_text": "未找到条款",
                "factual_basis": "缺失事实",
                "reasoning_basis": "需补充",
            }
        ]
        out = postprocess_anchored_risk_items(raw_items=raw_items, input_payload=INPUT_PAYLOAD)
        self.assertEqual(len(out["accepted_items"]), 0)
        self.assertEqual(len(out["dropped_items"]), 1)
        self.assertTrue(out["validation_errors"])

    def test_empty_evidence_should_be_dropped(self):
        raw_items = [
            {
                "risk_source_type": "anchored",
                "risk_label": "风险",
                "issue": "问题",
                "evidence_text": "",
                "factual_basis": "事实",
                "reasoning_basis": "推理",
            }
        ]
        out = postprocess_anchored_risk_items(raw_items=raw_items, input_payload=INPUT_PAYLOAD)
        self.assertEqual(len(out["accepted_items"]), 0)
        self.assertEqual(len(out["dropped_items"]), 1)
        self.assertIn("missing_evidence_text", out["validation_errors"][0])

    def test_weak_reasoning_should_flag_quality(self):
        raw_items = [
            {
                "risk_source_type": "anchored",
                "risk_label": "知识产权归属不利",
                "issue": "成果所有权全部归甲方",
                "evidence_text": "所有权均属甲方所有",
                "factual_basis": "根据原文",
                "reasoning_basis": "需要进一步人工审核",
            }
        ]
        out = postprocess_anchored_risk_items(raw_items=raw_items, input_payload=INPUT_PAYLOAD)
        self.assertEqual(len(out["accepted_items"]), 1)
        flags = out["accepted_items"][0]["quality_flags"]
        self.assertIn("weak_reasoning_basis", flags)
        self.assertIn("weak_factual_basis", flags)


if __name__ == "__main__":
    unittest.main()
