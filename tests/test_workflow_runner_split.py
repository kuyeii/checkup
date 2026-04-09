import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys
import types

sys.modules.setdefault("requests", types.SimpleNamespace(post=None))
from src.workflow_runner import WorkflowRunner


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def run_workflow(self, *, inputs, user, response_mode="blocking"):
        self.calls.append({"inputs": inputs, "user": user, "response_mode": response_mode})
        if not self._responses:
            raise AssertionError("No fake responses left")
        return self._responses.pop(0)


class WorkflowRunnerSplitTests(unittest.TestCase):
    def test_runner_uses_stream_specific_api_keys(self):
        settings = SimpleNamespace(
            dify_base_url="http://fake.local/v1",
            dify_clause_workflow_api_key="app-clause",
            request_timeout_seconds=5,
            review_side="supplier",
            contract_type_hint="service_agreement",
            anchored_risk_api_key=lambda: "app-anchored",
            missing_multi_risk_api_key=lambda: "app-missing-multi",
        )
        with tempfile.TemporaryDirectory() as td:
            runner = WorkflowRunner(settings=settings, run_dir=Path(td), user_id="u")
            self.assertEqual(runner.anchored_risk_client.api_key, "app-anchored")
            self.assertEqual(runner.missing_multi_risk_client.api_key, "app-missing-multi")

    def test_missing_multi_payload_builder_contains_outline_and_count(self):
        settings = SimpleNamespace(
            dify_base_url="http://fake.local/v1",
            dify_clause_workflow_api_key="x",
            dify_risk_workflow_api_key="y",
            dify_anchored_risk_workflow_api_key="",
            dify_missing_multi_risk_workflow_api_key="",
            request_timeout_seconds=5,
            review_side="supplier",
            contract_type_hint="service_agreement",
            anchored_risk_api_key=lambda: "y",
            missing_multi_risk_api_key=lambda: "y",
        )
        with tempfile.TemporaryDirectory() as td:
            runner = WorkflowRunner(settings=settings, run_dir=Path(td), user_id="u")
            payload = runner.build_missing_multi_review_payload(
                [
                    {
                        "clause_uid": "segment_1::1.1",
                        "segment_id": "segment_1",
                        "segment_title": "一、总则",
                        "clause_id": "1.1",
                        "display_clause_id": "1.1",
                        "clause_title": "总则",
                        "clause_text": "甲乙双方应遵守本协议。",
                        "clause_kind": "contract_clause",
                        "source_excerpt": "甲乙双方应遵守本协议。",
                    }
                ]
            )
            self.assertIn("clauses_json", payload)
            self.assertIn("contract_outline", payload)
            self.assertEqual(payload["clause_count"], "1")
            self.assertIsInstance(payload["clause_count"], str)

    def test_missing_multi_clause_count_is_string_for_three_clauses(self):
        settings = SimpleNamespace(
            dify_base_url="http://fake.local/v1",
            dify_clause_workflow_api_key="x",
            dify_risk_workflow_api_key="y",
            dify_anchored_risk_workflow_api_key="",
            dify_missing_multi_risk_workflow_api_key="",
            request_timeout_seconds=5,
            review_side="supplier",
            contract_type_hint="service_agreement",
            anchored_risk_api_key=lambda: "y",
            missing_multi_risk_api_key=lambda: "y",
        )
        with tempfile.TemporaryDirectory() as td:
            runner = WorkflowRunner(settings=settings, run_dir=Path(td), user_id="u")
            clauses = [
                {
                    "clause_uid": "segment_1::1.1",
                    "segment_id": "segment_1",
                    "segment_title": "一、总则",
                    "clause_id": "1.1",
                    "display_clause_id": "1.1",
                    "clause_title": "总则",
                    "clause_text": "甲乙双方应遵守本协议。",
                    "clause_kind": "contract_clause",
                    "source_excerpt": "甲乙双方应遵守本协议。",
                },
                {
                    "clause_uid": "segment_1::1.2",
                    "segment_id": "segment_1",
                    "segment_title": "一、总则",
                    "clause_id": "1.2",
                    "display_clause_id": "1.2",
                    "clause_title": "定义",
                    "clause_text": "定义内容。",
                    "clause_kind": "contract_clause",
                    "source_excerpt": "定义内容。",
                },
                {
                    "clause_uid": "segment_1::1.3",
                    "segment_id": "segment_1",
                    "segment_title": "一、总则",
                    "clause_id": "1.3",
                    "display_clause_id": "1.3",
                    "clause_title": "适用范围",
                    "clause_text": "适用范围内容。",
                    "clause_kind": "contract_clause",
                    "source_excerpt": "适用范围内容。",
                },
            ]
            payload = runner.build_missing_multi_review_payload(clauses)
            self.assertEqual(payload["clause_count"], "3")
            self.assertIsInstance(payload["clause_count"], str)

    def test_should_skip_anchored_call_when_clause_not_reviewable(self):
        settings = SimpleNamespace(
            dify_base_url="http://fake.local/v1",
            dify_clause_workflow_api_key="x",
            dify_risk_workflow_api_key="y",
            request_timeout_seconds=5,
            review_side="supplier",
            contract_type_hint="service_agreement",
            anchored_risk_api_key=lambda: "y",
            missing_multi_risk_api_key=lambda: "y",
        )
        with tempfile.TemporaryDirectory() as td:
            runner = WorkflowRunner(settings=settings, run_dir=Path(td), user_id="u")
            fake_missing = _FakeClient(
                [
                    {"data": {"outputs": {"risk_items": [{"risk_id": "M1", "risk_source_type": "missing_clause"}]}}},
                ]
            )
            runner.missing_multi_risk_client = fake_missing
            runner.anchored_risk_client = _FakeClient([])
            payloads = runner.run_risk_reviewers(
                [
                    {
                        "clause_uid": "segment_3::3.4",
                        "segment_id": "segment_3",
                        "segment_title": "三、空白条款",
                        "clause_id": "3.4",
                        "display_clause_id": "3.4",
                        "clause_title": "空白条款",
                        "clause_text": "。",
                        "clause_kind": "placeholder_clause",
                    }
                ]
            )
            self.assertIn("anchored", payloads)
            self.assertIn("missing_multi", payloads)
            self.assertEqual(len(fake_missing.calls), 1)
            self.assertEqual(fake_missing.calls[0]["inputs"]["risk_stream"], "missing_multi")
            self.assertEqual(payloads["anchored"]["risk_items"], [])

    def test_should_call_anchored_when_reviewable(self):
        settings = SimpleNamespace(
            dify_base_url="http://fake.local/v1",
            dify_clause_workflow_api_key="x",
            dify_risk_workflow_api_key="y",
            request_timeout_seconds=5,
            review_side="supplier",
            contract_type_hint="service_agreement",
            anchored_risk_api_key=lambda: "y",
            missing_multi_risk_api_key=lambda: "y",
        )
        with tempfile.TemporaryDirectory() as td:
            runner = WorkflowRunner(settings=settings, run_dir=Path(td), user_id="u")
            fake_anchored = _FakeClient(
                [
                    {
                        "data": {
                            "outputs": {
                                "text": '{"risk_items":[{"clause_uid":"segment_5::5.2","display_clause_id":"5.2","risk_id":"A1","risk_source_type":"anchored","risk_label":"测试锚定风险","issue":"该条款约束较重","evidence_text":"乙方赔偿责任上限为合同总价20%。","factual_basis":"条款明确约定赔偿上限","reasoning_basis":"该约定在特定交易中可能导致风险暴露"}]}'
                            }
                        }
                    },
                ]
            )
            fake_missing = _FakeClient(
                [{"data": {"outputs": {"risk_items": [{"risk_id": "M1", "risk_source_type": "missing_clause"}]}}}]
            )
            runner.anchored_risk_client = fake_anchored
            runner.missing_multi_risk_client = fake_missing

            payloads = runner.run_risk_reviewers(
                [
                    {
                        "clause_uid": "segment_5::5.2",
                        "segment_id": "segment_5",
                        "segment_title": "五、违约责任",
                        "clause_id": "5.2",
                        "clause_title": "赔偿",
                        "clause_text": "乙方赔偿责任上限为合同总价20%。",
                        "clause_kind": "contract_clause",
                        "source_excerpt": "乙方赔偿责任上限为合同总价20%。",
                        "numbering_confidence": 0.9,
                        "title_confidence": 0.9,
                        "is_boilerplate_instruction": False,
                    }
                ]
            )

            self.assertIn("anchored", payloads)
            self.assertIn("missing_multi", payloads)
            self.assertEqual(len(fake_anchored.calls), 1)
            self.assertEqual(len(fake_missing.calls), 1)
            self.assertEqual(fake_anchored.calls[0]["inputs"]["risk_stream"], "anchored")
            self.assertEqual(fake_missing.calls[0]["inputs"]["risk_stream"], "missing_multi")
            self.assertIn("segment_id", fake_anchored.calls[0]["inputs"])
            self.assertIn("clauses_json", fake_anchored.calls[0]["inputs"])
            self.assertIn("clause_count", fake_anchored.calls[0]["inputs"])
            self.assertIn("contract_outline", fake_missing.calls[0]["inputs"])
            self.assertIn("clause_count", fake_missing.calls[0]["inputs"])
            self.assertEqual(len(payloads["anchored"]["risk_items"]), 1)

            runner.anchored_risk_client = _FakeClient(
                [
                    {
                        "data": {
                            "outputs": {
                                "text": '{"risk_items":[{"clause_uid":"segment_5::5.2","display_clause_id":"5.2","risk_id":"A2","risk_source_type":"anchored","risk_label":"测试锚定风险2","issue":"该条款约束较重","evidence_text":"乙方赔偿责任上限为合同总价20%。","factual_basis":"条款明确约定赔偿上限","reasoning_basis":"可能导致风险暴露"}]}'
                            }
                        }
                    }
                ]
            )
            anchored_debug = runner.run_risk_reviewer_anchored(
                [
                    {
                        "clause_uid": "segment_5::5.2",
                        "segment_id": "segment_5",
                        "segment_title": "五、违约责任",
                        "clause_id": "5.2",
                        "clause_title": "赔偿",
                        "clause_text": "乙方赔偿责任上限为合同总价20%。",
                        "clause_kind": "contract_clause",
                        "source_excerpt": "乙方赔偿责任上限为合同总价20%。",
                        "numbering_confidence": 0.9,
                        "title_confidence": 0.9,
                        "is_boilerplate_instruction": False,
                    }
                ]
            )[0]["by_clause"][0]
            self.assertIn("input_payload", anchored_debug)
            self.assertIn("outputs", anchored_debug)
            self.assertIn("normalized_items", anchored_debug)
            self.assertIn("dropped_items", anchored_debug)
            self.assertIn("validation_errors", anchored_debug)

    def test_missing_multi_debug_structure(self):
        settings = SimpleNamespace(
            dify_base_url="http://fake.local/v1",
            dify_clause_workflow_api_key="x",
            dify_risk_workflow_api_key="y",
            dify_anchored_risk_workflow_api_key="",
            dify_missing_multi_risk_workflow_api_key="",
            request_timeout_seconds=5,
            review_side="supplier",
            contract_type_hint="service_agreement",
            anchored_risk_api_key=lambda: "y",
            missing_multi_risk_api_key=lambda: "y",
        )
        with tempfile.TemporaryDirectory() as td:
            runner = WorkflowRunner(settings=settings, run_dir=Path(td), user_id="u")
            fake_missing = _FakeClient(
                [{"data": {"outputs": {"risk_items": [{"risk_source_type": "missing_clause", "issue": "未约定"}]}}}]
            )
            runner.missing_multi_risk_client = fake_missing
            debug, payload = runner.run_risk_reviewer_missing_multi(
                [
                    {
                        "clause_uid": "segment_1::1.1",
                        "segment_id": "segment_1",
                        "segment_title": "一、总则",
                        "clause_id": "1.1",
                        "display_clause_id": "1.1",
                        "clause_title": "总则",
                        "clause_text": "甲乙双方应遵守本协议。",
                        "clause_kind": "contract_clause",
                        "source_excerpt": "甲乙双方应遵守本协议。",
                    }
                ]
            )
            self.assertIn("input_payload", debug)
            self.assertIn("outputs", debug)
            self.assertIn("normalized_items", debug)
            self.assertIn("dropped_items", debug)
            self.assertIn("validation_errors", debug)
            self.assertEqual(len(payload["risk_items"]), 1)


if __name__ == "__main__":
    unittest.main()
