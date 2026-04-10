import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree

from src.docx_apply_patches import _pick_candidates, export_ai_patches_to_docx


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _make_minimal_docx(path: Path, paragraph_runs_xml: str) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    package_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    document_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>
"""
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>{paragraph_runs_xml}</w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", package_rels)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/_rels/document.xml.rels", document_rels)


def _read_doc_root(docx_path: Path) -> etree._Element:
    with zipfile.ZipFile(docx_path, "r") as zf:
        document_xml = zf.read("word/document.xml")
    return etree.fromstring(document_xml)


class DocxApplyPatchesTests(unittest.TestCase):
    def test_pick_candidates_prefers_locator_then_evidence_then_ai_target(self):
        risk = {
            "target_text": "7",
            "evidence_text": "甲方应在7天内完成交接工作",
            "anchor_text": "7天内完成",
            "locator": {"matched_text": "双方应在7天内完成本项目交接工作"},
            "ai_rewrite": {"target_text": "7"},
            "ai_apply": {"target_text": "7"},
        }
        cands = _pick_candidates(risk)
        self.assertGreaterEqual(len(cands), 3)
        self.assertEqual(cands[0], "双方应在7天内完成本项目交接工作")
        self.assertEqual(cands[1], "甲方应在7天内完成交接工作")

    def test_export_applies_accepted_ai_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            input_docx = base / "in.docx"
            output_docx = base / "out.docx"
            risk_json = base / "risk_result_reviewed.json"

            _make_minimal_docx(input_docx, "<w:r><w:t>原文：乙方赔偿责任上限为合同总价20%。</w:t></w:r>")
            risk_json.write_text(
                json.dumps(
                    {
                        "risk_result": {
                            "risk_items": [
                                {
                                    "risk_id": 1,
                                    "status": "accepted",
                                    "ai_rewrite_decision": "accepted",
                                    "target_text": "乙方赔偿责任上限为合同总价20%。",
                                    "ai_rewrite": {
                                        "state": "succeeded",
                                        "target_text": "乙方赔偿责任上限为合同总价20%。",
                                        "revised_text": "乙方赔偿责任上限为合同总价100%。",
                                    },
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            report = export_ai_patches_to_docx(input_docx=input_docx, risk_path=risk_json, output_docx=output_docx)
            self.assertEqual(report["applied"], 1)
            self.assertEqual(report["failed"], 0)

            root = _read_doc_root(output_docx)
            ins_text = "".join(root.xpath(".//w:ins//w:t/text()", namespaces=NS))
            self.assertIn("乙方赔偿责任上限为合同总价100%。", ins_text)

    def test_export_prefers_underlined_occurrence_for_single_digit_target(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            input_docx = base / "in.docx"
            output_docx = base / "out.docx"
            risk_json = base / "risk_result_reviewed.json"

            paragraph_runs = (
                "<w:r><w:t>甲方应在</w:t></w:r>"
                "<w:r><w:rPr><w:u w:val=\"single\"/></w:rPr><w:t>7</w:t></w:r>"
                "<w:r><w:t>天内完成，若超期</w:t></w:r>"
                "<w:r><w:t>7</w:t></w:r>"
                "<w:r><w:t>日则违约。</w:t></w:r>"
            )
            _make_minimal_docx(input_docx, paragraph_runs)

            risk_json.write_text(
                json.dumps(
                    {
                        "risk_result": {
                            "risk_items": [
                                {
                                    "risk_id": 9,
                                    "status": "accepted",
                                    "ai_rewrite_decision": "accepted",
                                    "ai_rewrite": {
                                        "state": "succeeded",
                                        "target_text": "7",
                                        "revised_text": "9",
                                    },
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            report = export_ai_patches_to_docx(input_docx=input_docx, risk_path=risk_json, output_docx=output_docx)
            self.assertEqual(report["applied"], 1)
            self.assertEqual(report["failed"], 0)

            root = _read_doc_root(output_docx)
            ins_runs = root.xpath(".//w:ins//w:r", namespaces=NS)
            self.assertTrue(ins_runs)
            self.assertIn("9", "".join(root.xpath(".//w:ins//w:t/text()", namespaces=NS)))

            # Inserted digit keeps underline.
            self.assertTrue(root.xpath(".//w:ins//w:rPr/w:u", namespaces=NS))
            # There is still one non-replaced plain '7' in normal runs.
            plain_sevens = root.xpath(".//w:r[not(ancestor::w:ins) and not(ancestor::w:del)]/w:t[text()='7']", namespaces=NS)
            self.assertTrue(plain_sevens)

    def test_export_append_only_change_generates_insert_without_delete(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            input_docx = base / "in.docx"
            output_docx = base / "out.docx"
            risk_json = base / "risk_result_reviewed.json"

            original = "由乙方组织实施的本项目中的所有文件、资料、数据信息等，其所有权均属甲方所有。"
            appended = "，但乙方在项目实施前已拥有的知识产权除外。"
            revised = f"{original[:-1]}{appended}"
            _make_minimal_docx(input_docx, f"<w:r><w:t>{original}</w:t></w:r>")

            risk_json.write_text(
                json.dumps(
                    {
                        "risk_result": {
                            "risk_items": [
                                {
                                    "risk_id": 16,
                                    "status": "accepted",
                                    "ai_rewrite_decision": "accepted",
                                    "target_text": original,
                                    "ai_rewrite": {
                                        "state": "succeeded",
                                        "target_text": original,
                                        "revised_text": revised,
                                    },
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            report = export_ai_patches_to_docx(input_docx=input_docx, risk_path=risk_json, output_docx=output_docx)
            self.assertEqual(report["applied"], 1)
            self.assertEqual(report["failed"], 0)

            root = _read_doc_root(output_docx)
            del_nodes = root.xpath(".//w:del", namespaces=NS)
            ins_text = "".join(root.xpath(".//w:ins//w:t/text()", namespaces=NS))
            self.assertFalse(del_nodes)
            self.assertEqual(ins_text, appended)

    def test_export_dedupes_terminal_punctuation_at_replacement_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            input_docx = base / "in.docx"
            output_docx = base / "out.docx"
            risk_json = base / "risk_result_reviewed.json"

            paragraph = "项目验收合格后甲方向乙方支付合同总额的40%货款。"
            target = "项目验收合格后甲方向乙方支付合同总额的40%货款"
            revised = "乙方提交项目成果后，甲方在10日内完成验收审查，逾期未提出书面异议视为验收合格，甲方向乙方支付合同总额的40%货款。"
            _make_minimal_docx(input_docx, f"<w:r><w:t>{paragraph}</w:t></w:r>")

            risk_json.write_text(
                json.dumps(
                    {
                        "risk_result": {
                            "risk_items": [
                                {
                                    "risk_id": 77,
                                    "status": "accepted",
                                    "ai_rewrite_decision": "accepted",
                                    "target_text": target,
                                    "ai_rewrite": {
                                        "state": "succeeded",
                                        "target_text": target,
                                        "revised_text": revised,
                                    },
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            report = export_ai_patches_to_docx(input_docx=input_docx, risk_path=risk_json, output_docx=output_docx)
            self.assertEqual(report["applied"], 1)
            self.assertEqual(report["failed"], 0)

            root = _read_doc_root(output_docx)
            visible_text = "".join(
                root.xpath(".//w:r[not(ancestor::w:del)]//w:t/text()", namespaces=NS)
            )
            self.assertNotIn("。。", visible_text)
            self.assertIn("，甲方向乙方支付合同总额的40%货款。", visible_text)

    def test_export_supports_delete_rewrite_with_empty_revised_text(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            input_docx = base / "in.docx"
            output_docx = base / "out.docx"
            risk_json = base / "risk_result_reviewed.json"

            paragraph = "甲乙双方协商解决争议。没有争议的条款，双方应当继续履行。后续按法院判决处理。"
            target = "没有争议的条款，双方应当继续履行。"
            _make_minimal_docx(input_docx, f"<w:r><w:t>{paragraph}</w:t></w:r>")

            risk_json.write_text(
                json.dumps(
                    {
                        "risk_result": {
                            "risk_items": [
                                {
                                    "risk_id": 120,
                                    "status": "accepted",
                                    "ai_rewrite_decision": "accepted",
                                    "ai_rewrite": {
                                        "state": "succeeded",
                                        "target_text": target,
                                        "revised_text": "",
                                    },
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            report = export_ai_patches_to_docx(input_docx=input_docx, risk_path=risk_json, output_docx=output_docx)
            self.assertEqual(report["applied"], 1)
            self.assertEqual(report["failed"], 0)

            root = _read_doc_root(output_docx)
            visible_text = "".join(
                root.xpath(".//w:r[not(ancestor::w:del)]//w:t/text()", namespaces=NS)
            )
            deleted_text = "".join(root.xpath(".//w:del//w:delText/text()", namespaces=NS))
            self.assertEqual(visible_text, "甲乙双方协商解决争议。后续按法院判决处理。")
            self.assertEqual(deleted_text, target)

    def test_export_splits_replace_like_frontend_for_acceptance_phrase(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            input_docx = base / "in.docx"
            output_docx = base / "out.docx"
            risk_json = base / "risk_result_reviewed.json"

            paragraph = "合同签订后10日内向中标供应商支付合同总额的60%；项目验收合格后甲方向乙方支付合同总额的40%货款。"
            target = "项目验收合格后"
            revised = "乙方提交项目成果后，甲方在10日内完成验收审查，逾期未提出书面异议视为验收合格，"
            _make_minimal_docx(input_docx, f"<w:r><w:t>{paragraph}</w:t></w:r>")

            risk_json.write_text(
                json.dumps(
                    {
                        "risk_result": {
                            "risk_items": [
                                {
                                    "risk_id": 99,
                                    "status": "accepted",
                                    "ai_rewrite_decision": "accepted",
                                    "ai_rewrite": {
                                        "state": "succeeded",
                                        "target_text": target,
                                        "revised_text": revised,
                                    },
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            report = export_ai_patches_to_docx(input_docx=input_docx, risk_path=risk_json, output_docx=output_docx)
            self.assertEqual(report["applied"], 1)
            self.assertEqual(report["failed"], 0)

            root = _read_doc_root(output_docx)
            del_texts = ["".join(n.xpath(".//w:delText/text()", namespaces=NS)) for n in root.xpath(".//w:del", namespaces=NS)]
            ins_texts = ["".join(n.xpath(".//w:t/text()", namespaces=NS)) for n in root.xpath(".//w:ins", namespaces=NS)]
            self.assertIn("项目", del_texts)
            self.assertIn("后", del_texts)
            self.assertNotIn("项目验收合格后", del_texts)
            self.assertTrue(any("乙方提交项目成果后" in text for text in ins_texts))


if __name__ == "__main__":
    unittest.main()
