import unittest

from src.anchored_preprocess import prepare_anchored_clause_input


class AnchoredPreprocessTests(unittest.TestCase):
    def test_contract_clause_should_review_with_fallback_excerpt(self):
        clause = {
            "clause_uid": "segment_7::7.1",
            "clause_id": "7.1",
            "display_clause_id": "7.1",
            "clause_title": "知识产权",
            "clause_text": "由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有。",
            "clause_kind": "contract_clause",
            "source_excerpt": "",
            "segment_id": "segment_7",
            "segment_title": "七、知识产权",
        }
        prepared = prepare_anchored_clause_input(clause, review_side="supplier", contract_type_hint="service_agreement")
        self.assertTrue(prepared["should_review"])
        self.assertEqual(prepared["skip_reason"], "")
        payload = prepared["payload"]
        self.assertEqual(payload["source_excerpt"], payload["clause_text"])
        self.assertIn("clause_context", payload)
        for field in [
            "review_side",
            "contract_type_hint",
            "clause_uid",
            "clause_id",
            "display_clause_id",
            "clause_title",
            "clause_text",
            "clause_kind",
            "source_excerpt",
            "segment_id",
            "segment_title",
            "numbering_confidence",
            "title_confidence",
            "clause_context",
        ]:
            self.assertIn(field, payload)

    def test_placeholder_clause_should_skip(self):
        clause = {
            "clause_uid": "segment_3::3.4",
            "clause_id": "3.4",
            "display_clause_id": "3.4",
            "clause_title": "空白条款",
            "clause_text": "。",
            "clause_kind": "placeholder_clause",
            "segment_id": "segment_3",
        }
        prepared = prepare_anchored_clause_input(clause, review_side="supplier", contract_type_hint="service_agreement")
        self.assertFalse(prepared["should_review"])
        self.assertEqual(prepared["skip_reason"], "placeholder_clause")

    def test_note_clause_should_skip(self):
        clause = {
            "clause_uid": "segment_2::unlabeled_1",
            "clause_id": "unlabeled_1",
            "clause_title": "项目团队人员参保要求",
            "clause_text": "注：以上人员均要求在投标单位正常参保。",
            "clause_kind": "note_clause",
        }
        prepared = prepare_anchored_clause_input(clause, review_side="supplier", contract_type_hint="service_agreement")
        self.assertFalse(prepared["should_review"])
        self.assertEqual(prepared["skip_reason"], "note_clause")

    def test_confidence_should_clip(self):
        clause = {
            "clause_uid": "segment_7::7.1",
            "clause_id": "7.1",
            "display_clause_id": "7.1",
            "clause_title": "知识产权",
            "clause_text": "正文",
            "clause_kind": "contract_clause",
            "numbering_confidence": 1.5,
            "title_confidence": -0.2,
        }
        prepared = prepare_anchored_clause_input(clause, review_side="supplier", contract_type_hint="service_agreement")
        payload = prepared["payload"]
        self.assertEqual(payload["numbering_confidence"], "1")
        self.assertEqual(payload["title_confidence"], "0")
        self.assertIsInstance(payload["numbering_confidence"], str)
        self.assertIsInstance(payload["title_confidence"], str)

    def test_confidence_none_should_fallback_to_zero_string(self):
        clause = {
            "clause_uid": "segment_1::1.1",
            "clause_id": "1.1",
            "display_clause_id": "1.1",
            "clause_title": "总则",
            "clause_text": "正文",
            "clause_kind": "contract_clause",
            "numbering_confidence": None,
            "title_confidence": None,
        }
        prepared = prepare_anchored_clause_input(clause, review_side="supplier", contract_type_hint="service_agreement")
        payload = prepared["payload"]
        self.assertEqual(payload["numbering_confidence"], "0")
        self.assertEqual(payload["title_confidence"], "0")
        self.assertIsInstance(payload["numbering_confidence"], str)
        self.assertIsInstance(payload["title_confidence"], str)


if __name__ == "__main__":
    unittest.main()
