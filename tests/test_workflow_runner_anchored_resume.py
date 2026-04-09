import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("requests", types.SimpleNamespace(post=None))
from src.workflow_runner import WorkflowRunner


class _FakeAnchoredClient:
    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []

    def run_workflow(self, *, inputs, user, response_mode="blocking"):
        self.calls.append({"inputs": inputs, "user": user, "response_mode": response_mode})
        if not self.plans:
            raise AssertionError("No fake plan left")
        current = self.plans.pop(0)
        if isinstance(current, Exception):
            raise current
        return current


def _batch_response(risk_items: list[dict]) -> dict:
    return {
        "data": {
            "outputs": {
                "text": json.dumps({"risk_items": risk_items}, ensure_ascii=False),
            }
        }
    }


class WorkflowRunnerAnchoredResumeTests(unittest.TestCase):
    def _build_settings(self):
        return SimpleNamespace(
            dify_base_url="http://fake.local/v1",
            dify_clause_workflow_api_key="x",
            request_timeout_seconds=5,
            review_side="supplier",
            contract_type_hint="service_agreement",
            anchored_risk_api_key=lambda: "y",
            missing_multi_risk_api_key=lambda: "z",
            dify_fast_screen_workflow_api_key="f",
            fast_screen_enabled=False,
            fast_screen_max_candidates="12",
        )

    def _build_clauses(self):
        return [
            {
                "clause_uid": "segment_1::1.1",
                "segment_id": "segment_1",
                "segment_title": "一",
                "clause_id": "1.1",
                "display_clause_id": "1.1",
                "clause_title": "条款1",
                "clause_text": "正文1",
                "clause_kind": "contract_clause",
                "source_excerpt": "正文1",
            },
            {
                "clause_uid": "segment_1::1.2",
                "segment_id": "segment_1",
                "segment_title": "一",
                "clause_id": "1.2",
                "display_clause_id": "1.2",
                "clause_title": "条款2",
                "clause_text": "正文2",
                "clause_kind": "contract_clause",
                "source_excerpt": "正文2",
            },
            {
                "clause_uid": "segment_2::2.1",
                "segment_id": "segment_2",
                "segment_title": "二",
                "clause_id": "2.1",
                "display_clause_id": "2.1",
                "clause_title": "条款3",
                "clause_text": "正文3",
                "clause_kind": "contract_clause",
                "source_excerpt": "正文3",
            },
        ]

    def test_segment_checkpoint_fail_and_resume_from_segment_start(self):
        clauses = self._build_clauses()
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            runner1 = WorkflowRunner(settings=self._build_settings(), run_dir=run_dir, user_id="u")
            runner1.anchored_risk_client = _FakeAnchoredClient(
                [
                    _batch_response(
                        [
                            {
                                "clause_uid": "segment_1::1.1",
                                "display_clause_id": "1.1",
                                "risk_source_type": "anchored",
                                "risk_label": "r11",
                                "issue": "问题1",
                                "evidence_text": "证据1",
                                "factual_basis": "事实1",
                                "reasoning_basis": "推理1",
                            },
                            {
                                "clause_uid": "segment_1::1.2",
                                "display_clause_id": "1.2",
                                "risk_source_type": "anchored",
                                "risk_label": "r12",
                                "issue": "问题2",
                                "evidence_text": "证据2",
                                "factual_basis": "事实2",
                                "reasoning_basis": "推理2",
                            },
                        ]
                    ),
                    RuntimeError("boom on segment_2"),
                ]
            )

            with self.assertRaises(RuntimeError):
                runner1.run_risk_reviewer_anchored(clauses, resume=True)

            ckpt_path = run_dir / "risk_checkpoints" / "anchored_state.json"
            state = json.loads(ckpt_path.read_text(encoding="utf-8"))
            self.assertEqual(state["next_clause_index"], 2)
            self.assertEqual(state["last_error"]["segment_id"], "segment_2")
            self.assertEqual(state["last_error"]["segment_start_clause_index"], 2)
            self.assertEqual(state["last_error"]["clause_uids"], ["segment_2::2.1"])

            runner2 = WorkflowRunner(settings=self._build_settings(), run_dir=run_dir, user_id="u")
            client2 = _FakeAnchoredClient(
                [
                    _batch_response(
                        [
                            {
                                "clause_uid": "segment_2::2.1",
                                "display_clause_id": "2.1",
                                "risk_source_type": "anchored",
                                "risk_label": "r21",
                                "issue": "问题3",
                                "evidence_text": "证据3",
                                "factual_basis": "事实3",
                                "reasoning_basis": "推理3",
                            }
                        ]
                    )
                ]
            )
            runner2.anchored_risk_client = client2
            debug, payload = runner2.run_risk_reviewer_anchored(clauses, resume=True)

            self.assertEqual(len(client2.calls), 1)
            self.assertEqual(client2.calls[0]["inputs"]["segment_id"], "segment_2")
            self.assertEqual(client2.calls[0]["inputs"]["clause_count"], "1")
            self.assertEqual(len(debug["by_clause"]), 3)
            self.assertEqual(len(payload["risk_items"]), 3)

    def test_batch_output_group_by_clause_uid(self):
        clauses = self._build_clauses()[:2]
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            runner = WorkflowRunner(settings=self._build_settings(), run_dir=run_dir, user_id="u")
            client = _FakeAnchoredClient(
                [
                    _batch_response(
                        [
                            {
                                "clause_uid": "segment_1::1.1",
                                "display_clause_id": "1.1",
                                "risk_source_type": "anchored",
                                "risk_label": "r11",
                                "issue": "问题1",
                                "evidence_text": "证据1",
                                "factual_basis": "事实1",
                                "reasoning_basis": "推理1",
                            },
                            {
                                "clause_uid": "segment_1::1.2",
                                "display_clause_id": "1.2",
                                "risk_source_type": "anchored",
                                "risk_label": "r12",
                                "issue": "问题2",
                                "evidence_text": "证据2",
                                "factual_basis": "事实2",
                                "reasoning_basis": "推理2",
                            },
                            {
                                "display_clause_id": "x",
                                "risk_source_type": "anchored",
                                "risk_label": "bad",
                                "issue": "缺少uid",
                                "evidence_text": "bad",
                                "factual_basis": "bad",
                                "reasoning_basis": "bad",
                            },
                        ]
                    )
                ]
            )
            runner.anchored_risk_client = client
            debug, payload = runner.run_risk_reviewer_anchored(clauses, resume=False)

            self.assertEqual(len(client.calls), 1)
            self.assertIn("clauses_json", client.calls[0]["inputs"])
            self.assertEqual(client.calls[0]["inputs"]["segment_id"], "segment_1")
            self.assertEqual(client.calls[0]["inputs"]["clause_count"], "2")
            self.assertEqual(len(debug["by_clause"]), 2)
            self.assertEqual(len(payload["risk_items"]), 2)
            for item in payload["risk_items"]:
                self.assertTrue(item.get("clause_uid"))
                self.assertTrue(item.get("display_clause_id"))
            dropped_total = sum(len(x.get("dropped_items") or []) for x in debug["by_clause"])
            self.assertGreaterEqual(dropped_total, 1)


if __name__ == "__main__":
    unittest.main()
