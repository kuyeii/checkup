import unittest

from src.normalize_risks import normalize_and_dedupe_risks


CLAUSES = [
    {
        "clause_uid": "segment_7::7.1",
        "clause_id": "7.1",
        "display_clause_id": "7.1",
        "clause_text": "由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有。",
        "segment_id": "segment_7",
        "is_boilerplate_instruction": False,
    }
]


def _normalize_single(item: dict) -> dict:
    out = normalize_and_dedupe_risks({"risk_items": [item]}, CLAUSES)
    return out["risk_items"][0]


class StructuredBasisSummaryTests(unittest.TestCase):
    def test_prefers_factual_and_reasoning_basis(self):
        item = _normalize_single(
            {
                "risk_label": "知识产权归属不清",
                "dimension": "知识产权归属与使用权",
                "issue": "乙方背景权利边界不清",
                "evidence_text": "所有权均属甲方所有",
                "clause_id": "7.1",
                "factual_basis": "条款将项目文件和数据概括归甲方所有。",
                "reasoning_basis": "未区分乙方既有知识产权与项目成果，可能导致乙方背景权利边界不清。",
            }
        )
        self.assertIn("概括归甲方所有", item["basis_summary"])
        self.assertIn("边界不清", item["basis_summary"])
        self.assertIn("概括归甲方所有", item["basis"])
        self.assertIn("边界不清", item["basis"])
        self.assertNotIn("需要进一步人工审核", item["basis_summary"])

    def test_prefers_normative_basis(self):
        item = _normalize_single(
            {
                "risk_label": "验收标准不明确",
                "dimension": "服务期限、里程碑与验收标准",
                "issue": "未明确可执行的验收标准",
                "evidence_text": "甲方验收不合格可要求整改",
                "clause_id": "7.1",
                "factual_basis": "条款约定了整改责任，但未列出明确验收指标。",
                "reasoning_basis": "缺少可核验口径会导致验收争议。",
                "normative_basis": {
                    "basis_type": "contract_logic_rule",
                    "basis_title": "验收标准应客观可执行",
                    "basis_detail": "验收标准应具备可核验指标，否则容易导致单方判断。",
                    "citation_text": "",
                },
            }
        )
        self.assertIn("验收标准应客观可执行", item["basis"])
        self.assertIn("可核验指标", item["basis"])
        self.assertTrue(
            ("验收标准应客观可执行" in item["basis_summary"])
            or ("可核验指标" in item["basis_summary"])
        )

    def test_basis_includes_citation_text_but_summary_stays_short(self):
        item = _normalize_single(
            {
                "risk_label": "管辖条款不利",
                "dimension": "争议解决、适用法律与管辖",
                "issue": "争议解决固定甲方所在地法院",
                "evidence_text": "向甲方所在地人民法院起诉",
                "clause_id": "7.1",
                "factual_basis": "条款固定为甲方所在地法院。",
                "reasoning_basis": "会增加乙方异地维权成本。",
                "normative_basis": {
                    "basis_title": "争议解决条款应兼顾可诉性",
                    "basis_detail": "单方固定管辖可能造成维权成本失衡。",
                    "citation_text": "《民事诉讼法》关于协议管辖的规定允许约定，但通常应审慎评估实际可诉性。",
                },
            }
        )
        self.assertIn("《民事诉讼法》", item["basis"])
        self.assertNotIn("《民事诉讼法》", item["basis_summary"])

    def test_old_item_falls_back_to_legacy_basis_logic(self):
        item = _normalize_single(
            {
                "risk_label": "付款条款不明确",
                "dimension": "付款结算、发票与税费",
                "issue": "付款节点未明确",
                "evidence_text": "付款安排以双方后续确认为准",
                "clause_id": "7.1",
            }
        )
        self.assertIn("当前文本不足以支撑稳定履约与争议处理", item["basis_summary"])

    def test_empty_structured_fields_fall_back_without_error(self):
        item = _normalize_single(
            {
                "risk_label": "验收条款风险",
                "dimension": "服务期限、里程碑与验收标准",
                "issue": "验收标准不明确",
                "evidence_text": "验收标准由甲方解释",
                "clause_id": "7.1",
                "factual_basis": "",
                "reasoning_basis": "",
                "normative_basis": {},
            }
        )
        self.assertIn("根据原文", item["basis_summary"])

    def test_no_template_sentences_when_structured_basis_exists(self):
        item = _normalize_single(
            {
                "risk_label": "责任限制缺失",
                "dimension": "权责分配与责任限制",
                "issue": "无乙方赔偿责任上限",
                "evidence_text": "赔偿责任按实际损失承担",
                "clause_id": "7.1",
                "factual_basis": "条款未设置乙方累计赔偿责任上限。",
                "reasoning_basis": "在高额索赔情境下可能形成无限责任敞口。",
            }
        )
        self.assertNotIn("需要进一步人工审核", item["basis_summary"])
        self.assertNotIn("当前文本不足以支撑稳定履约与争议处理", item["basis_summary"])
        self.assertNotIn("需要进一步人工审核", item["basis"])


if __name__ == "__main__":
    unittest.main()
