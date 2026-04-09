from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("requests", types.SimpleNamespace(post=None))

from src.workflow_runner import WorkflowRunner


class FakeDifyClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active_calls = 0
        self.peak_active_calls = 0

    def run_workflow(self, *, inputs, user, response_mode="blocking"):
        del user, response_mode
        with self._lock:
            self.active_calls += 1
            if self.active_calls > self.peak_active_calls:
                self.peak_active_calls = self.active_calls
        try:
            time.sleep(0.05)
            clauses = json.loads(inputs.get("clauses_json", "[]"))
            risk_items = []
            for clause in clauses:
                clause_uid = str(clause.get("clause_uid") or "")
                display_clause_id = str(clause.get("display_clause_id") or clause.get("clause_id") or "")
                risk_items.append(
                    {
                        "clause_uid": clause_uid,
                        "display_clause_id": display_clause_id,
                        "risk_source_type": "anchored",
                        "risk_label": f"label_{display_clause_id}",
                        "issue": "issue",
                        "evidence_text": "evidence",
                        "factual_basis": "facts",
                        "reasoning_basis": "reason",
                    }
                )
            return {
                "data": {
                    "outputs": {
                        "text": json.dumps({"risk_items": risk_items}, ensure_ascii=False),
                    }
                }
            }
        finally:
            with self._lock:
                self.active_calls -= 1


def _build_settings(max_concurrency: int) -> SimpleNamespace:
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
        dify_max_concurrency=max_concurrency,
    )


def _build_clauses() -> list[dict]:
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
            "clause_uid": "segment_2::2.1",
            "segment_id": "segment_2",
            "segment_title": "二",
            "clause_id": "2.1",
            "display_clause_id": "2.1",
            "clause_title": "条款2",
            "clause_text": "正文2",
            "clause_kind": "contract_clause",
            "source_excerpt": "正文2",
        },
        {
            "clause_uid": "segment_3::3.1",
            "segment_id": "segment_3",
            "segment_title": "三",
            "clause_id": "3.1",
            "display_clause_id": "3.1",
            "clause_title": "条款3",
            "clause_text": "正文3",
            "clause_kind": "contract_clause",
            "source_excerpt": "正文3",
        },
    ]


def test_anchored_parallel_resume_false_enables_parallel_calls(tmp_path: Path, monkeypatch):
    clauses = _build_clauses()
    runner = WorkflowRunner(settings=_build_settings(max_concurrency=4), run_dir=tmp_path, user_id="u")
    fake = FakeDifyClient()
    monkeypatch.setattr(runner, "anchored_risk_client", fake)

    debug, payload = runner.run_risk_reviewer_anchored(clauses, resume=False)

    assert fake.peak_active_calls >= 2
    assert len(payload["risk_items"]) == len(clauses)
    assert len(debug["by_clause"]) == len(clauses)
    for record in debug["by_clause"]:
        assert set(record.keys()) == {
            "clause_uid",
            "input_payload",
            "outputs",
            "normalized_items",
            "dropped_items",
            "validation_errors",
        }
        assert isinstance(record["normalized_items"], list)


def test_anchored_parallel_resume_true_stays_serial(tmp_path: Path, monkeypatch):
    clauses = _build_clauses()
    runner = WorkflowRunner(settings=_build_settings(max_concurrency=4), run_dir=tmp_path, user_id="u")
    fake = FakeDifyClient()
    monkeypatch.setattr(runner, "anchored_risk_client", fake)

    debug, payload = runner.run_risk_reviewer_anchored(clauses, resume=True)

    assert fake.peak_active_calls == 1
    assert len(payload["risk_items"]) == len(clauses)
    assert len(debug["by_clause"]) == len(clauses)
