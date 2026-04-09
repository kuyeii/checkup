import unittest

from src.parse_outputs import parse_risk_payload, strip_markdown_json


class ParseRiskPayloadRobustnessTests(unittest.TestCase):
    def test_strip_markdown_json_nested_think(self):
        text = '<think>outer a <think>inner</think> outer b</think>{"risk_items":[]}'
        cleaned = strip_markdown_json(text)
        self.assertEqual(cleaned, '{"risk_items":[]}')

    def test_strip_markdown_json_multiple_think_blocks(self):
        text = "<think>a</think>xxx<think>b</think>yyy"
        cleaned = strip_markdown_json(text)
        self.assertEqual(cleaned, "xxxyyy")

    def test_strip_markdown_json_unclosed_think(self):
        text = 'aaa<think>bbb{"risk_items":[]}'
        cleaned = strip_markdown_json(text)
        self.assertEqual(cleaned, "aaa")

    def test_parse_text_with_think_block_then_real_json(self):
        text = '<think>解释里有示例 {"foo":1} 以及更多说明</think>{"risk_items":[]}'
        out = parse_risk_payload(text)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("risk_items"), [])

    def test_parse_text_with_think_tags(self):
        text = '<think>模型推理 {"risk_items": []} 解释</think>{"risk_items":[]}'
        out = parse_risk_payload(text)
        self.assertIsInstance(out, dict)
        self.assertIn("risk_items", out)
        self.assertEqual(out["risk_items"], [])

    def test_parse_text_with_natural_language_wrapped_json(self):
        text = '说明文字：请参考如下输出 {"risk_items": []} 尾巴说明'
        out = parse_risk_payload(text)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("risk_items"), [])

    def test_parse_double_escaped_json_text(self):
        text = '{\\n \\"risk_items\\": [] }'
        out = parse_risk_payload(text)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("risk_items"), [])

    def test_parse_markdown_json_block(self):
        text = "```json\n{ \"risk_items\": [] }\n```"
        out = parse_risk_payload(text)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("risk_items"), [])

    def test_list_payload_can_be_wrapped(self):
        text = '[{"risk_label":"付款条款风险","issue":"付款节点不明确"}]'
        out = parse_risk_payload(text)
        self.assertIsInstance(out, dict)
        self.assertEqual(len(out.get("risk_items", [])), 1)


if __name__ == "__main__":
    unittest.main()
