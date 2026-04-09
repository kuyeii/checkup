import unittest

from src.normalize_clauses import normalize_clause_record, normalize_clauses


class ClauseSchemaV2Tests(unittest.TestCase):
    def test_case_1_full_v2_clause_should_preserve_fields(self):
        raw = {
            "clause_id": "3.3",
            "clause_title": "验收",
            "clause_text": "甲方应在收到成果后5个工作日内完成验收。",
            "segment_id": "segment_3",
            "segment_title": "第三条 验收",
            "clause_kind": "contract_clause",
            "source_excerpt": "甲方应在收到成果后5个工作日内完成验收。",
            "numbering_confidence": 0.98,
            "title_confidence": 0.95,
        }
        record = normalize_clause_record(raw)
        self.assertEqual(record["clause_kind"], "contract_clause")
        self.assertEqual(record["source_excerpt"], raw["source_excerpt"])
        self.assertEqual(record["numbering_confidence"], 0.98)
        self.assertEqual(record["title_confidence"], 0.95)

        merged = normalize_clauses([raw])
        clause = merged[0]
        self.assertEqual(clause["clause_kind"], "contract_clause")
        self.assertEqual(clause["source_excerpt"], raw["source_excerpt"])
        self.assertEqual(clause["numbering_confidence"], 0.98)
        self.assertEqual(clause["title_confidence"], 0.95)

    def test_case_5_legacy_clause_should_load_and_fill_defaults(self):
        raw = [
            {
                "segment_id": "segment_3",
                "segment_title": "三、付款条款",
                "clause_id": "3.1",
                "clause_title": "付款条件",
                "clause_text": "甲方应在验收通过后 15 日内付款。",
            }
        ]
        normalized = normalize_clauses(raw)
        self.assertEqual(len(normalized), 1)
        clause = normalized[0]
        self.assertEqual(clause["clause_kind"], "contract_clause")
        self.assertEqual(clause["source_excerpt"], clause["clause_text"])
        self.assertIsNone(clause["numbering_confidence"])
        self.assertIsNone(clause["title_confidence"])

    def test_case_3_placeholder_clause_should_remain(self):
        raw = [
            {
                "segment_id": "segment_3",
                "segment_title": "三、违约责任",
                "clause_id": "3.4",
                "clause_title": "违约责任",
                "clause_text": "。",
                "clause_kind": "placeholder_clause",
            }
        ]
        normalized = normalize_clauses(raw)
        self.assertEqual(normalized[0]["clause_kind"], "placeholder_clause")

    def test_case_4_invalid_confidence_should_clip(self):
        raw = {
            "segment_id": "segment_3",
            "segment_title": "第三条 价款",
            "clause_id": "3.6",
            "clause_title": "价款",
            "clause_text": "合同总价款为人民币 100 万元。",
            "numbering_confidence": 1.5,
            "title_confidence": -0.2,
        }
        normalized = normalize_clauses([raw])[0]
        self.assertEqual(normalized["numbering_confidence"], 1.0)
        self.assertEqual(normalized["title_confidence"], 0.0)

    def test_case_5_source_excerpt_fallback_to_clause_text(self):
        raw = {
            "segment_id": "segment_3",
            "segment_title": "第三条 价款",
            "clause_id": "3.7",
            "clause_title": "价款",
            "clause_text": "甲方应在收到发票后付款。",
            "source_excerpt": "",
        }
        normalized = normalize_clauses([raw])[0]
        self.assertEqual(normalized["source_excerpt"], normalized["clause_text"])

    def test_normal_body_should_remain_contract_clause(self):
        raw = [
            {
                "segment_id": "segment_3",
                "segment_title": "三、违约责任",
                "clause_id": "3.3",
                "clause_title": "验收",
                "clause_text": "甲方应在收到成果后5个工作日内完成验收。",
            }
        ]
        normalized = normalize_clauses(raw)
        self.assertEqual(normalized[0]["clause_kind"], "contract_clause")


if __name__ == "__main__":
    unittest.main()
