from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", types.SimpleNamespace(post=None))
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))

import src.rerun_anchored_segment as rerun_mod


def _risk_item(
    *,
    clause_uid: str,
    display_clause_id: str,
    risk_label: str,
    issue: str,
    evidence_text: str,
    suggestion: str,
    risk_source_type: str = "anchored",
) -> dict:
    clause_ids = [display_clause_id] if display_clause_id else []
    clause_uids = [clause_uid] if clause_uid else []
    is_multi = risk_source_type == "multi_clause"
    return {
        "dimension": "权责分配与责任限制",
        "risk_label": risk_label,
        "risk_level": "medium",
        "issue": issue,
        "basis": f"{risk_label}依据",
        "evidence_text": evidence_text,
        "suggestion": suggestion,
        "clause_id": display_clause_id,
        "display_clause_id": display_clause_id,
        "anchor_text": evidence_text,
        "needs_human_review": True,
        "status": "pending",
        "clause_uid": clause_uid,
        "clause_uids": clause_uids,
        "display_clause_ids": clause_ids,
        "clause_ids": clause_ids,
        "is_multi_clause_risk": is_multi,
        "basis_rule_id": "RULE_TEST_001",
        "basis_summary": f"{risk_label}摘要",
        "review_required_reason": ["需要人工复核"],
        "auto_apply_allowed": False,
        "is_boilerplate_related": False,
        "mapping_conflict": False,
        "risk_source_type": risk_source_type,
        "suggestion_minimal": suggestion,
        "suggestion_optimized": f"{suggestion}（优化）",
        "evidence_confidence": 0.9,
        "quality_flags": [],
        "related_clause_ids": [],
        "related_clause_uids": [],
        "factual_basis": f"{risk_label}事实",
        "reasoning_basis": f"{risk_label}推理",
    }


class _FakeWorkflowRunner:
    def __init__(self, settings, run_dir: Path, user_id: str) -> None:
        self.settings = settings
        self.run_dir = run_dir
        self.user_id = user_id

    def run_anchored_for_segment(self, *, segment_id: str, segment_title: str, clauses: list[dict], segment_start_idx: int) -> dict:
        assert segment_id == "segment_2"
        assert segment_title == "二"
        assert len(clauses) == 1
        assert segment_start_idx == 1
        return {
            "segment_id": segment_id,
            "segment_start_idx": segment_start_idx,
            "segment_end_idx": segment_start_idx,
            "outputs": {"text": '{"risk_items": []}'},
            "by_clause_records": [
                {
                    "clause_uid": "segment_2::2.1",
                    "input_payload": {"clause_uid": "segment_2::2.1"},
                    "outputs": {"text": '{"risk_items": []}'},
                    "normalized_items": [
                        _risk_item(
                            clause_uid="segment_2::2.1",
                            display_clause_id="2.1",
                            risk_label="新风险",
                            issue="新问题",
                            evidence_text="新证据",
                            suggestion="新建议",
                        )
                    ],
                    "dropped_items": [],
                    "validation_errors": [],
                }
            ],
            "accepted_items": [
                _risk_item(
                    clause_uid="segment_2::2.1",
                    display_clause_id="2.1",
                    risk_label="新风险",
                    issue="新问题",
                    evidence_text="新证据",
                    suggestion="新建议",
                )
            ],
            "error": None,
            "duration_seconds": 0.01,
            "skipped": [],
        }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class RerunAnchoredSegmentTests(unittest.TestCase):
    def test_rerun_anchored_segment_rewrites_segment_outputs(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        tmp_path = Path(temp_dir.name)

        run_root = tmp_path / "runs"
        run_dir = run_root / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)

        (run_dir / "merged_clauses.json").write_text(
            json.dumps(
                [
                    {
                        "clause_uid": "segment_1::1.1",
                        "segment_id": "segment_1",
                        "segment_title": "一",
                        "display_clause_id": "1.1",
                        "clause_id": "1.1",
                        "clause_text": "条款一",
                        "source_excerpt": "条款一",
                        "clause_kind": "contract_clause",
                    },
                    {
                        "clause_uid": "segment_2::2.1",
                        "segment_id": "segment_2",
                        "segment_title": "二",
                        "display_clause_id": "2.1",
                        "clause_id": "2.1",
                        "clause_text": "条款二",
                        "source_excerpt": "条款二",
                        "clause_kind": "contract_clause",
                    },
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        _write_json(
            run_dir / "risk_result_outputs.json",
            {
                "anchored": {
                    "by_clause": [
                        {"clause_uid": "segment_1::1.1", "normalized_items": [{"risk_label": "旧风险1"}]},
                        {"clause_uid": "segment_2::2.1", "normalized_items": [{"risk_label": "旧风险2"}]},
                    ],
                    "skipped": [],
                    "errors": [{"segment_id": "segment_2", "error_message": "old"}],
                },
                "missing_multi": {
                    "normalized_items": [
                        {"risk_source_type": "missing_clause", "risk_label": "缺失风险", "issue": "缺少付款条款"}
                    ]
                },
            },
        )
        _write_json(
            run_dir / "risk_result_raw.json",
            {
                "anchored": {
                    "risk_items": [
                        _risk_item(
                            clause_uid="segment_1::1.1",
                            display_clause_id="1.1",
                            risk_label="旧风险1",
                            issue="旧问题1",
                            evidence_text="旧证据1",
                            suggestion="旧建议1",
                        ),
                        _risk_item(
                            clause_uid="segment_2::2.1",
                            display_clause_id="2.1",
                            risk_label="旧风险2",
                            issue="旧问题2",
                            evidence_text="旧证据2",
                            suggestion="旧建议2",
                        ),
                    ]
                },
                "missing_multi": {
                    "risk_items": [
                        _risk_item(
                            clause_uid="",
                            display_clause_id="",
                            risk_label="缺失风险",
                            issue="缺少付款条款",
                            evidence_text="未约定付款节点",
                            suggestion="补充付款节点",
                            risk_source_type="missing_clause",
                        )
                    ]
                },
            },
        )
        (run_dir / "risk_result_reviewed.json").write_text("{}", encoding="utf-8")
        (run_dir / "reviewed_comments.docx").write_bytes(b"old-docx")

        fake_settings = SimpleNamespace(
            run_root=run_root,
            validate_for_live_call=lambda: None,
        )

        with patch.object(rerun_mod, "settings", fake_settings), patch.object(rerun_mod, "WorkflowRunner", _FakeWorkflowRunner):
            summary = rerun_mod.rerun_anchored_segment(run_id="run_001", segment_id="segment_2", user_id="u")

        self.assertEqual(summary["accepted_risk_count"], 1)
        self.assertTrue(summary["reviewed_snapshot_reset"])
        self.assertTrue(summary["docx_reset"])

        outputs = json.loads((run_dir / "risk_result_outputs.json").read_text(encoding="utf-8"))
        anchored_by_clause = outputs["anchored"]["by_clause"]
        self.assertEqual([item["clause_uid"] for item in anchored_by_clause], ["segment_1::1.1", "segment_2::2.1"])
        self.assertEqual(anchored_by_clause[1]["normalized_items"][0]["risk_label"], "新风险")
        self.assertNotIn("errors", outputs["anchored"])

        raw = json.loads((run_dir / "risk_result_raw.json").read_text(encoding="utf-8"))
        anchored_items = raw["anchored"]["risk_items"]
        self.assertEqual(len(anchored_items), 2)
        self.assertEqual(anchored_items[0]["risk_label"], "旧风险1")
        self.assertEqual(anchored_items[1]["risk_label"], "新风险")

        validated = json.loads((run_dir / "risk_result_validated.json").read_text(encoding="utf-8"))
        self.assertTrue(validated["is_valid"])
        risk_items = validated["risk_result"]["risk_items"]
        self.assertTrue(any(item["risk_label"] == "新风险" for item in risk_items))
        self.assertTrue(any(item["risk_label"] == "缺失风险" for item in risk_items))

        self.assertFalse((run_dir / "risk_result_reviewed.json").exists())
        self.assertTrue((run_dir / "risk_result_reviewed.before_rerun_segment_2.json").exists())
        self.assertFalse((run_dir / "reviewed_comments.docx").exists())
        self.assertTrue((run_dir / "reviewed_comments.before_rerun_segment_2.docx").exists())
        self.assertTrue((run_dir / "risk_checkpoints" / "anchored_segment_reruns" / "segment_2.json").exists())


if __name__ == "__main__":
    unittest.main()
