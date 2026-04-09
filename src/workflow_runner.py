from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .anchored_postprocess import postprocess_anchored_risk_items
from .anchored_preprocess import prepare_anchored_clause_input
from .dify_client import DifyWorkflowClient, extract_blocking_outputs
from .file_utils import write_json
from .normalize_clauses import normalize_clause_records
from .parse_outputs import (
    _load_json_with_repair,
    parse_clause_payload,
    parse_risk_payload,
    strip_markdown_json,
)

if TYPE_CHECKING:
    from config import Settings


@dataclass(slots=True)
class _SegmentJob:
    segment_id: str
    segment_title: str
    segment_start_idx: int
    segment_end_idx: int
    segment_clause_uids: list[str]
    segment_payload_buffer: list[dict[str, Any]]
    payload_by_uid: dict[str, dict[str, Any]]


@dataclass(slots=True)
class _SegmentResult:
    segment_id: str
    segment_start_idx: int
    segment_end_idx: int
    outputs: dict[str, Any]
    by_clause_records: list[dict[str, Any]]
    accepted_items: list[dict[str, Any]]
    error: dict[str, Any] | None
    duration_seconds: float


class WorkflowRunner:
    _FAST_SCREEN_EXCERPT_MAX_LEN = 700

    def __init__(self, settings: "Settings", run_dir: Path, user_id: str) -> None:
        self.settings = settings
        self.run_dir = run_dir
        self.user_id = user_id
        self.clause_client = DifyWorkflowClient(
            base_url=settings.dify_base_url,
            api_key=settings.dify_clause_workflow_api_key,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.anchored_risk_client = DifyWorkflowClient(
            base_url=settings.dify_base_url,
            api_key=settings.anchored_risk_api_key(),
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.missing_multi_risk_client = DifyWorkflowClient(
            base_url=settings.dify_base_url,
            api_key=settings.missing_multi_risk_api_key(),
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.fast_screen_client = DifyWorkflowClient(
            base_url=settings.dify_base_url,
            api_key=getattr(settings, "dify_fast_screen_workflow_api_key", ""),
            timeout_seconds=settings.request_timeout_seconds,
        )
        # Backward-compat alias for old tests/callers that referenced one risk client.
        self.risk_client = self.anchored_risk_client
        self.anchored_checkpoint_path = self.run_dir / "risk_checkpoints" / "anchored_state.json"
        self.fast_screen_checkpoint_path = self.run_dir / "risk_checkpoints" / "fast_screen.json"
        self.fast_screen_enabled = bool(getattr(settings, "fast_screen_enabled", False))
        self.fast_screen_max_candidates = str(getattr(settings, "fast_screen_max_candidates", "12"))

    def _anchored_clauses_fingerprint(self, clauses: list[dict[str, Any]]) -> str:
        joined = "||".join(str(c.get("clause_uid", "") or "") for c in clauses)
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()

    def _write_anchored_checkpoint(
        self,
        *,
        clauses_fingerprint: str,
        next_clause_index: int,
        by_clause: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        risk_items: list[dict[str, Any]],
        last_error: dict[str, Any] | None,
    ) -> None:
        state = {
            "version": 1,
            "clauses_fingerprint": clauses_fingerprint,
            "next_clause_index": max(0, int(next_clause_index)),
            "by_clause": by_clause,
            "skipped": skipped,
            "risk_items": risk_items,
            "last_error": last_error or {},
        }
        write_json(self.anchored_checkpoint_path, state)

    def _load_anchored_checkpoint(self) -> dict[str, Any] | None:
        if not self.anchored_checkpoint_path.exists():
            return None
        try:
            return json.loads(self.anchored_checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_fast_screen_checkpoint(self) -> dict[str, Any] | None:
        if not self.fast_screen_checkpoint_path.exists():
            return None
        try:
            return json.loads(self.fast_screen_checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def run_clause_splitter(self, segment: dict[str, str]) -> list[dict[str, Any]]:
        response = self.clause_client.run_workflow(
            inputs={
                "segment_id": segment["segment_id"],
                "segment_title": segment["segment_title"],
                "segment_text": segment["segment_text"],
            },
            user=self.user_id,
            response_mode="blocking",
        )
        outputs = extract_blocking_outputs(response)
        raw = outputs.get("clauses") if "clauses" in outputs else outputs.get("text", outputs)
        clauses = parse_clause_payload(raw)
        for item in clauses:
            item.setdefault("segment_id", segment.get("segment_id", "segment_unknown"))
            item.setdefault("segment_title", segment.get("segment_title", ""))
        clauses = normalize_clause_records(clauses)
        write_json(self.run_dir / "clauses" / f"{segment['segment_id']}.json", clauses)
        return clauses

    def _build_risk_inputs(self, clauses: list[dict[str, Any]]) -> dict[str, Any]:
        minimal_clauses = [
            {
                "clause_uid": c.get("clause_uid"),
                "segment_id": c.get("segment_id"),
                "segment_title": c.get("segment_title"),
                "clause_id": c.get("clause_id"),
                "display_clause_id": c.get("display_clause_id"),
                "clause_title": c.get("clause_title"),
                "clause_text": c.get("clause_text"),
                "clause_kind": c.get("clause_kind"),
                "source_excerpt": c.get("source_excerpt"),
                "numbering_confidence": c.get("numbering_confidence"),
                "title_confidence": c.get("title_confidence"),
                "is_boilerplate_instruction": c.get("is_boilerplate_instruction"),
            }
            for c in clauses
        ]
        return {
            "clauses_json": json.dumps(minimal_clauses, ensure_ascii=False),
            "review_side": self.settings.review_side,
            "contract_type_hint": self.settings.contract_type_hint,
        }

    def _build_fast_screen_clause_item(self, clause: dict[str, Any]) -> dict[str, Any]:
        excerpt = str(clause.get("source_excerpt") or clause.get("clause_text") or "")
        excerpt = excerpt[: self._FAST_SCREEN_EXCERPT_MAX_LEN]
        return {
            "clause_uid": str(clause.get("clause_uid") or ""),
            "display_clause_id": str(clause.get("display_clause_id") or ""),
            "clause_id": str(clause.get("clause_id") or ""),
            "clause_title": str(clause.get("clause_title") or ""),
            "clause_kind": str(clause.get("clause_kind") or ""),
            "clause_text_excerpt": excerpt,
        }

    def _parse_fast_screen_candidates(self, outputs: dict[str, Any]) -> set[str]:
        raw: Any = outputs.get("text")
        if raw is None:
            raw = outputs.get("output")
        if raw is None and isinstance(outputs.get("candidate_clause_uids"), list):
            raw = {"candidate_clause_uids": outputs.get("candidate_clause_uids")}
        if raw is None:
            raise ValueError(f"Fast screen outputs missing text/output: keys={list(outputs.keys())}")

        payload: Any = raw
        if isinstance(raw, str):
            cleaned = strip_markdown_json(raw)
            payload = _load_json_with_repair(cleaned)

        if isinstance(payload, dict):
            candidate_uids = payload.get("candidate_clause_uids")
            if not isinstance(candidate_uids, list):
                raise ValueError("Fast screen payload missing candidate_clause_uids list")
            return {str(uid).strip() for uid in candidate_uids if str(uid).strip()}

        if isinstance(payload, list):
            return {str(uid).strip() for uid in payload if str(uid).strip()}

        raise ValueError(f"Unsupported fast screen payload type: {type(payload).__name__}")

    def run_fast_screen_by_segment(self, clauses: list[dict[str, Any]], resume: bool) -> set[str]:
        clauses_fingerprint = self._anchored_clauses_fingerprint(clauses)
        if resume:
            saved = self._load_fast_screen_checkpoint()
            if isinstance(saved, dict) and str(saved.get("clauses_fingerprint", "")) == clauses_fingerprint:
                cached = saved.get("all_candidates") or []
                if isinstance(cached, list):
                    print(f"Fast screen cache hit; reuse {len(cached)} candidate clauses.")
                    return {str(uid).strip() for uid in cached if str(uid).strip()}

        segment_groups: dict[str, list[dict[str, Any]]] = {}
        for clause in clauses:
            segment_id = str(clause.get("segment_id") or "segment_unknown")
            segment_groups.setdefault(segment_id, []).append(clause)

        per_segment: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        all_candidates: set[str] = set()

        for segment_id, segment_clauses in segment_groups.items():
            reviewable_clauses: list[dict[str, Any]] = []
            reviewable_clause_uids: list[str] = []
            for clause in segment_clauses:
                prepared = prepare_anchored_clause_input(
                    clause,
                    review_side=self.settings.review_side,
                    contract_type_hint=self.settings.contract_type_hint,
                )
                if prepared.get("should_review"):
                    reviewable_clauses.append(clause)
                    uid = str(clause.get("clause_uid") or "")
                    if uid:
                        reviewable_clause_uids.append(uid)

            if not reviewable_clauses:
                per_segment[segment_id] = {
                    "reviewable_clause_uids": [],
                    "candidate_clause_uids": [],
                    "skipped_reason": "no_reviewable_clause",
                }
                continue

            clauses_json = json.dumps(
                [self._build_fast_screen_clause_item(c) for c in reviewable_clauses],
                ensure_ascii=False,
            )
            inputs = {
                "review_side": self.settings.review_side,
                "contract_type_hint": self.settings.contract_type_hint,
                "segment_id": segment_id,
                "segment_title": str(reviewable_clauses[0].get("segment_title") or ""),
                "clauses_json": clauses_json,
                "max_candidates": str(self.fast_screen_max_candidates),
            }

            try:
                response = self.fast_screen_client.run_workflow(
                    inputs=inputs,
                    user=self.user_id,
                    response_mode="blocking",
                )
                outputs = extract_blocking_outputs(response)
                candidate_set = self._parse_fast_screen_candidates(outputs)
                # Keep only current segment's reviewable clauses for safety.
                segment_reviewable_set = set(reviewable_clause_uids)
                candidate_set = {uid for uid in candidate_set if uid in segment_reviewable_set}
                all_candidates.update(candidate_set)
                per_segment[segment_id] = {
                    "reviewable_clause_uids": reviewable_clause_uids,
                    "candidate_clause_uids": sorted(candidate_set),
                }
            except Exception as exc:
                all_candidates.update(reviewable_clause_uids)
                error_record = {
                    "segment_id": segment_id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "fallback": "all_reviewable_clauses_as_candidates",
                }
                errors.append(error_record)
                per_segment[segment_id] = {
                    "reviewable_clause_uids": reviewable_clause_uids,
                    "candidate_clause_uids": reviewable_clause_uids,
                    "fallback_reason": "fast_screen_error",
                }

        write_json(
            self.fast_screen_checkpoint_path,
            {
                "version": 1,
                "clauses_fingerprint": clauses_fingerprint,
                "max_candidates": str(self.fast_screen_max_candidates),
                "per_segment": per_segment,
                "all_candidates": sorted(all_candidates),
                "errors": errors,
            },
        )
        if errors:
            print(f"Fast screen finished with {len(errors)} segment fallback(s).")
        else:
            print(f"Fast screen finished; {len(all_candidates)} candidate clauses kept.")
        return all_candidates

    def _build_contract_outline(self, clauses: list[dict[str, Any]], max_lines: int = 12) -> str:
        lines: list[str] = []
        for clause in clauses:
            clause_id = str(clause.get("display_clause_id") or clause.get("clause_id") or "").strip()
            title = str(clause.get("clause_title") or "").strip()
            text = str(clause.get("source_excerpt") or clause.get("clause_text") or "").strip()
            text = (text[:80] + "…") if len(text) > 80 else text
            parts = [p for p in [clause_id, title, text] if p]
            if parts:
                lines.append(" | ".join(parts))
            if len(lines) >= max_lines:
                break
        return "\n".join(lines)

    def build_missing_multi_review_payload(self, clauses: list[dict[str, Any]]) -> dict[str, Any]:
        base = self._build_risk_inputs(clauses)
        base["contract_outline"] = self._build_contract_outline(clauses)
        base["clause_count"] = str(len(clauses))
        return base

    def _parse_risk_outputs(self, outputs: dict[str, Any]) -> dict[str, Any]:
        raw = outputs.get("risk_items")
        if raw is not None and isinstance(raw, list):
            return {"risk_items": raw}
        if "contract_risk_report" in outputs:
            return outputs
        # Compatibility fallback for payloads like {"text": "..."} / {"output": "..."} / {"foo": "..."}.
        if len(outputs) == 1:
            only_value = next(iter(outputs.values()))
            if isinstance(only_value, str):
                return parse_risk_payload(only_value)
        return parse_risk_payload(outputs.get("text", outputs))

    def _run_risk_workflow(
        self,
        *,
        clauses: list[dict[str, Any]],
        stream: Literal["anchored", "missing_multi"],
        client: DifyWorkflowClient,
        inputs: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        inputs = dict(inputs or self._build_risk_inputs(clauses))
        # Current phase may still use one Dify workflow key; split stream boundary in Python first.
        inputs["risk_stream"] = stream
        response = client.run_workflow(
            inputs=inputs,
            user=self.user_id,
            response_mode="blocking",
        )
        outputs = extract_blocking_outputs(response)
        payload = self._parse_risk_outputs(outputs)
        return inputs, outputs, payload

    def _run_anchored_segment_job(self, job: _SegmentJob) -> _SegmentResult:
        started = time.perf_counter()
        inputs = {
            "review_side": self.settings.review_side,
            "contract_type_hint": self.settings.contract_type_hint,
            "segment_id": job.segment_id,
            "segment_title": job.segment_title,
            "clauses_json": json.dumps(job.segment_payload_buffer, ensure_ascii=False),
            "clause_count": str(len(job.segment_payload_buffer)),
            "risk_stream": "anchored",
        }
        outputs: dict[str, Any] = {}
        print(f"[anchored-parallel] start {job.segment_id}")
        try:
            response = self.anchored_risk_client.run_workflow(
                inputs=inputs,
                user=self.user_id,
                response_mode="blocking",
            )
            outputs = extract_blocking_outputs(response)
            parsed = self._parse_risk_outputs(outputs)
            parsed_items = parsed.get("risk_items") if isinstance(parsed, dict) else []
            raw_items = [it for it in (parsed_items or []) if isinstance(it, dict)]
            items_by_uid: dict[str, list[dict[str, Any]]] = {uid: [] for uid in job.segment_clause_uids}
            unmatched_dropped: list[dict[str, Any]] = []

            for item in raw_items:
                item_uid = str(item.get("clause_uid") or "").strip()
                if not item_uid:
                    unmatched_dropped.append({"item": item, "reason": "missing_clause_uid"})
                    continue
                if item_uid not in job.payload_by_uid:
                    unmatched_dropped.append({"item": item, "reason": f"unknown_clause_uid={item_uid}"})
                    continue
                items_by_uid.setdefault(item_uid, []).append(item)

            by_clause_records: list[dict[str, Any]] = []
            accepted_items: list[dict[str, Any]] = []
            for uid in job.segment_clause_uids:
                input_payload = job.payload_by_uid[uid]
                post = postprocess_anchored_risk_items(
                    raw_items=items_by_uid.get(uid, []),
                    input_payload=input_payload,
                )
                clause_accepted_items = post.get("accepted_items") or []
                accepted_items.extend(clause_accepted_items)
                by_clause_records.append(
                    {
                        "clause_uid": uid,
                        "input_payload": input_payload,
                        "outputs": outputs,
                        "normalized_items": clause_accepted_items,
                        "dropped_items": (post.get("dropped_items") or []) + unmatched_dropped,
                        "validation_errors": post.get("validation_errors") or [],
                    }
                )

            return _SegmentResult(
                segment_id=job.segment_id,
                segment_start_idx=job.segment_start_idx,
                segment_end_idx=job.segment_end_idx,
                outputs=outputs,
                by_clause_records=by_clause_records,
                accepted_items=accepted_items,
                error=None,
                duration_seconds=time.perf_counter() - started,
            )
        except Exception as e:
            raw_preview = str(outputs)[:300] if isinstance(outputs, dict) else ""
            return _SegmentResult(
                segment_id=job.segment_id,
                segment_start_idx=job.segment_start_idx,
                segment_end_idx=job.segment_end_idx,
                outputs=outputs if isinstance(outputs, dict) else {},
                by_clause_records=[],
                accepted_items=[],
                error={
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "segment_start_idx": job.segment_start_idx,
                    "inputs": inputs,
                    "raw_preview": raw_preview,
                },
                duration_seconds=time.perf_counter() - started,
            )
        finally:
            print(f"[anchored-parallel] end {job.segment_id}")

    def run_anchored_for_segment(
        self,
        *,
        segment_id: str,
        segment_title: str,
        clauses: list[dict[str, Any]],
        segment_start_idx: int,
    ) -> dict[str, Any]:
        skipped: list[dict[str, str]] = []
        segment_payload_buffer: list[dict[str, Any]] = []
        payload_by_uid: dict[str, dict[str, Any]] = {}
        segment_clause_uids: list[str] = []

        for clause in clauses:
            prepared = prepare_anchored_clause_input(
                clause,
                review_side=self.settings.review_side,
                contract_type_hint=self.settings.contract_type_hint,
            )
            payload = prepared.get("payload") or {}
            clause_uid = str(payload.get("clause_uid") or clause.get("clause_uid") or "")
            if not prepared.get("should_review"):
                skipped.append({"clause_uid": clause_uid, "skip_reason": str(prepared.get("skip_reason") or "")})
                continue

            clause_uid = str(payload.get("clause_uid") or clause.get("clause_uid") or "").strip()
            if not clause_uid:
                skipped.append({"clause_uid": "", "skip_reason": "missing_clause_uid"})
                continue
            payload_by_uid[clause_uid] = dict(payload)
            segment_payload_buffer.append(dict(payload))
            segment_clause_uids.append(clause_uid)

        if not segment_payload_buffer:
            return {
                "segment_id": segment_id,
                "segment_start_idx": segment_start_idx,
                "segment_end_idx": segment_start_idx,
                "outputs": {},
                "by_clause_records": [],
                "accepted_items": [],
                "error": None,
                "duration_seconds": 0.0,
                "skipped": skipped,
            }

        job = _SegmentJob(
            segment_id=str(segment_id or "segment_unknown"),
            segment_title=str(segment_title or ""),
            segment_start_idx=int(segment_start_idx),
            segment_end_idx=int(segment_start_idx),
            segment_clause_uids=segment_clause_uids,
            segment_payload_buffer=segment_payload_buffer,
            payload_by_uid=payload_by_uid,
        )
        result = self._run_anchored_segment_job(job)
        return {
            "segment_id": result.segment_id,
            "segment_start_idx": result.segment_start_idx,
            "segment_end_idx": result.segment_end_idx,
            "outputs": result.outputs,
            "by_clause_records": result.by_clause_records,
            "accepted_items": result.accepted_items,
            "error": result.error,
            "duration_seconds": result.duration_seconds,
            "skipped": skipped,
        }

    def run_risk_reviewer_anchored(
        self,
        clauses: list[dict[str, Any]],
        resume: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        outputs_by_clause: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        all_items: list[dict[str, Any]] = []
        parallel_errors: list[dict[str, Any]] = []
        clauses_fingerprint = self._anchored_clauses_fingerprint(clauses)
        next_clause_index = 0

        if resume:
            saved = self._load_anchored_checkpoint()
            if isinstance(saved, dict):
                saved_fingerprint = str(saved.get("clauses_fingerprint", "") or "")
                if saved_fingerprint == clauses_fingerprint:
                    next_clause_index = int(saved.get("next_clause_index", 0) or 0)
                    outputs_by_clause = list(saved.get("by_clause") or [])
                    skipped = list(saved.get("skipped") or [])
                    all_items = list(saved.get("risk_items") or [])
                else:
                    print(
                        "Anchored checkpoint fingerprint mismatch; "
                        "ignore old checkpoint and restart anchored stream from clause 0."
                    )

        if resume:
            current_segment_id: str | None = None
            current_segment_title = ""
            segment_start_idx = -1
            segment_end_idx = -1
            segment_clause_uids: list[str] = []
            segment_payload_buffer: list[dict[str, Any]] = []
            payload_by_uid: dict[str, dict[str, Any]] = {}

            def _flush_segment() -> None:
                nonlocal current_segment_id
                nonlocal current_segment_title
                nonlocal segment_start_idx
                nonlocal segment_end_idx
                nonlocal segment_clause_uids
                nonlocal segment_payload_buffer
                nonlocal payload_by_uid
                if not segment_payload_buffer:
                    current_segment_id = None
                    current_segment_title = ""
                    segment_start_idx = -1
                    segment_end_idx = -1
                    segment_clause_uids = []
                    payload_by_uid = {}
                    return

                inputs = {
                    "review_side": self.settings.review_side,
                    "contract_type_hint": self.settings.contract_type_hint,
                    "segment_id": str(current_segment_id or "segment_unknown"),
                    "segment_title": current_segment_title,
                    "clauses_json": json.dumps(segment_payload_buffer, ensure_ascii=False),
                    "clause_count": str(len(segment_payload_buffer)),
                    "risk_stream": "anchored",
                }
                outputs: dict[str, Any] | None = None
                try:
                    response = self.anchored_risk_client.run_workflow(
                        inputs=inputs,
                        user=self.user_id,
                        response_mode="blocking",
                    )
                    outputs = extract_blocking_outputs(response)
                    parsed = self._parse_risk_outputs(outputs)
                    parsed_items = parsed.get("risk_items") if isinstance(parsed, dict) else []
                    raw_items = [it for it in (parsed_items or []) if isinstance(it, dict)]
                    items_by_uid: dict[str, list[dict[str, Any]]] = {uid: [] for uid in segment_clause_uids}
                    unmatched_dropped: list[dict[str, Any]] = []

                    for item in raw_items:
                        item_uid = str(item.get("clause_uid") or "").strip()
                        if not item_uid:
                            unmatched_dropped.append({"item": item, "reason": "missing_clause_uid"})
                            continue
                        if item_uid not in payload_by_uid:
                            unmatched_dropped.append({"item": item, "reason": f"unknown_clause_uid={item_uid}"})
                            continue
                        items_by_uid.setdefault(item_uid, []).append(item)

                    for uid in segment_clause_uids:
                        input_payload = payload_by_uid[uid]
                        post = postprocess_anchored_risk_items(
                            raw_items=items_by_uid.get(uid, []),
                            input_payload=input_payload,
                        )
                        accepted_items = post.get("accepted_items") or []
                        all_items.extend(accepted_items)
                        outputs_by_clause.append(
                            {
                                "clause_uid": uid,
                                "input_payload": input_payload,
                                "outputs": outputs,
                                "normalized_items": accepted_items,
                                "dropped_items": (post.get("dropped_items") or []) + unmatched_dropped,
                                "validation_errors": post.get("validation_errors") or [],
                            }
                        )

                    self._write_anchored_checkpoint(
                        clauses_fingerprint=clauses_fingerprint,
                        next_clause_index=segment_end_idx + 1,
                        by_clause=outputs_by_clause,
                        skipped=skipped,
                        risk_items=all_items,
                        last_error={},
                    )
                except Exception as e:
                    raw_preview = ""
                    if isinstance(outputs, dict):
                        raw_preview = str(outputs)[:300]
                    last_error = {
                        "segment_id": str(current_segment_id or ""),
                        "clause_uids": list(segment_clause_uids),
                        "segment_start_clause_index": segment_start_idx,
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                        "input_payload": inputs,
                        "outputs": outputs if isinstance(outputs, dict) else {},
                        "raw_preview": raw_preview,
                    }
                    self._write_anchored_checkpoint(
                        clauses_fingerprint=clauses_fingerprint,
                        next_clause_index=segment_start_idx,
                        by_clause=outputs_by_clause,
                        skipped=skipped,
                        risk_items=all_items,
                        last_error=last_error,
                    )
                    raise
                finally:
                    current_segment_id = None
                    current_segment_title = ""
                    segment_start_idx = -1
                    segment_end_idx = -1
                    segment_clause_uids = []
                    segment_payload_buffer = []
                    payload_by_uid = {}

            for idx, clause in enumerate(clauses):
                if idx < next_clause_index:
                    continue
                clause_segment_id = str(clause.get("segment_id") or "segment_unknown")
                clause_segment_title = str(clause.get("segment_title") or "")
                if segment_payload_buffer and current_segment_id and clause_segment_id != current_segment_id:
                    _flush_segment()

                prepared = prepare_anchored_clause_input(
                    clause,
                    review_side=self.settings.review_side,
                    contract_type_hint=self.settings.contract_type_hint,
                )
                payload = prepared.get("payload") or {}
                clause_uid = str(payload.get("clause_uid") or clause.get("clause_uid") or "")
                if not prepared.get("should_review"):
                    skipped.append({"clause_uid": clause_uid, "skip_reason": str(prepared.get("skip_reason") or "")})
                    if segment_payload_buffer and current_segment_id == clause_segment_id:
                        segment_end_idx = idx
                    else:
                        self._write_anchored_checkpoint(
                            clauses_fingerprint=clauses_fingerprint,
                            next_clause_index=idx + 1,
                            by_clause=outputs_by_clause,
                            skipped=skipped,
                            risk_items=all_items,
                            last_error={},
                        )
                    continue

                if not current_segment_id:
                    current_segment_id = str(payload.get("segment_id") or clause_segment_id or "segment_unknown")
                    current_segment_title = str(payload.get("segment_title") or clause_segment_title or "")
                    segment_start_idx = idx
                    segment_end_idx = idx
                segment_end_idx = idx
                clause_uid = str(payload.get("clause_uid") or clause.get("clause_uid") or "").strip()
                if not clause_uid:
                    skipped.append({"clause_uid": "", "skip_reason": "missing_clause_uid"})
                    continue
                payload_by_uid[clause_uid] = dict(payload)
                segment_payload_buffer.append(dict(payload))
                segment_clause_uids.append(clause_uid)

            if segment_payload_buffer:
                _flush_segment()
        else:
            segment_jobs: list[_SegmentJob] = []
            current_segment_id: str | None = None
            current_segment_title = ""
            segment_start_idx = -1
            segment_end_idx = -1
            segment_clause_uids: list[str] = []
            segment_payload_buffer: list[dict[str, Any]] = []
            payload_by_uid: dict[str, dict[str, Any]] = {}

            def _close_segment_job() -> None:
                nonlocal current_segment_id
                nonlocal current_segment_title
                nonlocal segment_start_idx
                nonlocal segment_end_idx
                nonlocal segment_clause_uids
                nonlocal segment_payload_buffer
                nonlocal payload_by_uid
                if segment_payload_buffer:
                    segment_jobs.append(
                        _SegmentJob(
                            segment_id=str(current_segment_id or "segment_unknown"),
                            segment_title=current_segment_title,
                            segment_start_idx=segment_start_idx,
                            segment_end_idx=segment_end_idx,
                            segment_clause_uids=list(segment_clause_uids),
                            segment_payload_buffer=list(segment_payload_buffer),
                            payload_by_uid=dict(payload_by_uid),
                        )
                    )
                current_segment_id = None
                current_segment_title = ""
                segment_start_idx = -1
                segment_end_idx = -1
                segment_clause_uids = []
                segment_payload_buffer = []
                payload_by_uid = {}

            for idx, clause in enumerate(clauses):
                clause_segment_id = str(clause.get("segment_id") or "segment_unknown")
                clause_segment_title = str(clause.get("segment_title") or "")
                if segment_payload_buffer and current_segment_id and clause_segment_id != current_segment_id:
                    _close_segment_job()

                prepared = prepare_anchored_clause_input(
                    clause,
                    review_side=self.settings.review_side,
                    contract_type_hint=self.settings.contract_type_hint,
                )
                payload = prepared.get("payload") or {}
                clause_uid = str(payload.get("clause_uid") or clause.get("clause_uid") or "")
                if not prepared.get("should_review"):
                    skipped.append({"clause_uid": clause_uid, "skip_reason": str(prepared.get("skip_reason") or "")})
                    if segment_payload_buffer and current_segment_id == clause_segment_id:
                        segment_end_idx = idx
                    continue

                if not current_segment_id:
                    current_segment_id = str(payload.get("segment_id") or clause_segment_id or "segment_unknown")
                    current_segment_title = str(payload.get("segment_title") or clause_segment_title or "")
                    segment_start_idx = idx
                    segment_end_idx = idx
                segment_end_idx = idx
                clause_uid = str(payload.get("clause_uid") or clause.get("clause_uid") or "").strip()
                if not clause_uid:
                    skipped.append({"clause_uid": "", "skip_reason": "missing_clause_uid"})
                    continue
                payload_by_uid[clause_uid] = dict(payload)
                segment_payload_buffer.append(dict(payload))
                segment_clause_uids.append(clause_uid)

            if segment_payload_buffer:
                _close_segment_job()

            max_concurrency = max(1, int(getattr(self.settings, "dify_max_concurrency", 6) or 6))
            unordered_results: list[_SegmentResult] = []
            with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
                futures = [executor.submit(self._run_anchored_segment_job, job) for job in segment_jobs]
                for future in as_completed(futures):
                    unordered_results.append(future.result())

            errors: list[dict[str, Any]] = []
            segment_results_summary: list[dict[str, Any]] = []
            for result in sorted(unordered_results, key=lambda item: item.segment_start_idx):
                outputs_by_clause.extend(result.by_clause_records)
                all_items.extend(result.accepted_items)
                summary_item = {
                    "segment_id": result.segment_id,
                    "segment_start_idx": result.segment_start_idx,
                    "segment_end_idx": result.segment_end_idx,
                    "status": "ok" if not result.error else "error",
                    "duration_seconds": round(result.duration_seconds, 6),
                    "risk_item_count": len(result.accepted_items),
                    "by_clause_count": len(result.by_clause_records),
                }
                segment_results_summary.append(summary_item)
                if result.error:
                    errors.append(
                        {
                            "segment_id": result.segment_id,
                            "segment_start_idx": result.segment_start_idx,
                            **result.error,
                        }
                    )

            write_json(
                self.run_dir / "risk_checkpoints" / "anchored_parallel_state.json",
                {
                    "version": 1,
                    "clauses_fingerprint": clauses_fingerprint,
                    "max_concurrency": max_concurrency,
                    "segment_results_summary": segment_results_summary,
                    "errors": errors,
                },
            )
            if errors:
                print(f"{len(errors)} segments failed")
            parallel_errors = errors

        debug: dict[str, Any] = {"by_clause": outputs_by_clause, "skipped": skipped}
        if parallel_errors:
            debug["errors"] = parallel_errors
        return (debug, {"risk_items": all_items})

    def run_risk_reviewer_missing_multi(self, clauses: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        inputs = self.build_missing_multi_review_payload(clauses)
        sent_inputs, outputs, payload = self._run_risk_workflow(
            clauses=clauses,
            stream="missing_multi",
            client=self.missing_multi_risk_client,
            inputs=inputs,
        )
        parsed_items = payload.get("risk_items") if isinstance(payload, dict) else []
        normalized_items = [it for it in (parsed_items or []) if isinstance(it, dict)]
        debug = {
            "input_payload": sent_inputs,
            "outputs": outputs,
            "normalized_items": normalized_items,
            "dropped_items": [],
            "validation_errors": [],
        }
        return debug, {"risk_items": normalized_items}

    def run_risk_reviewers(self, clauses: list[dict[str, Any]], resume: bool = False) -> dict[str, Any]:
        def run_anchored():
            return self.run_risk_reviewer_anchored(clauses, resume=resume)

        def run_missing_multi():
            return self.run_risk_reviewer_missing_multi(clauses)

        anchored_outputs = None
        anchored_payload = None
        missing_multi_outputs = None
        missing_multi_payload = None

        with ThreadPoolExecutor(max_workers=2) as executor:
            anchored_future = executor.submit(run_anchored)
            missing_multi_future = executor.submit(run_missing_multi)

            anchored_outputs, anchored_payload = anchored_future.result()
            missing_multi_outputs, missing_multi_payload = missing_multi_future.result()

        bundle_outputs = {
            "anchored": anchored_outputs,
            "missing_multi": missing_multi_outputs,
        }
        write_json(self.run_dir / "risk_result_outputs.json", bundle_outputs)
        return {
            "anchored": anchored_payload,
            "missing_multi": missing_multi_payload,
        }

    def run_risk_reviewer(self, clauses: list[dict[str, Any]]) -> dict[str, Any]:
        """Backward-compatible single payload for older call sites."""
        streams = self.run_risk_reviewers(clauses, resume=False)
        return {
            "risk_items": (streams.get("anchored", {}).get("risk_items") or [])
            + (streams.get("missing_multi", {}).get("risk_items") or []),
        }
