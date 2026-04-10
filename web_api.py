from __future__ import annotations

import json
from difflib import SequenceMatcher
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from config import settings
from src.dify_client import DifyWorkflowClient, DifyWorkflowError, extract_blocking_outputs
from src.parse_outputs import _load_json_with_repair, strip_markdown_json

BASE_DIR = Path(__file__).resolve().parent
RUN_ROOT = BASE_DIR / "data" / "runs"
UPLOAD_ROOT = BASE_DIR / "data" / "uploads"
WEB_META_ROOT = BASE_DIR / "data" / "web_meta"

for path in (RUN_ROOT, UPLOAD_ROOT, WEB_META_ROOT):
    path.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Contract Review Web API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_CLAUSE_UID_PATTERN = r"segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）\-]+"
_CLAUSE_UID_RE = re.compile(_CLAUSE_UID_PATTERN)
_CLAUSE_REF_SPLIT_RE = re.compile(r"\s*[、，,；;/]\s*")
_TARGET_PREFIX_RE = re.compile(rf"^\s*(?:{_CLAUSE_UID_PATTERN})\s*")
_CLAUSE_REF_TOKEN_PATTERN = r"[0-9一二三四五六七八九十百千万零〇]+(?:\.[A-Za-z0-9]+)*"
_LEADING_CLAUSE_LABEL_RE_LIST = [
    re.compile(rf"^\s*(?:条款|条文|clause)\s*{_CLAUSE_REF_TOKEN_PATTERN}\s*[:：，,]\s*", re.IGNORECASE),
    re.compile(rf"^\s*第?\s*{_CLAUSE_REF_TOKEN_PATTERN}\s*(?:条|款)\s*[:：，,]?\s*"),
    re.compile(rf"^\s*{_CLAUSE_REF_TOKEN_PATTERN}\s*[:：，,]\s*"),
    re.compile(r"^\s*[A-Za-z]+[0-9][A-Za-z0-9]*\s*[:：，,]\s*"),
]
_TARGET_INTRO_RE = re.compile(
    r"^\s*(?:(?:条款|条文|clause)\s*)?(?:约定|规定|载明|提到|显示)?\s*[:：，,]?\s*",
    re.IGNORECASE,
)
_QUOTED_TEXT_RE_LIST = [
    re.compile(r"「([^」]{4,})」"),
    re.compile(r"“([^”]{4,})”"),
    re.compile(r'"([^"\n]{4,})"'),
]
_ACCEPTED_RISK_STATUSES = {"accepted", "ai_applied"}



_TEXT_BOUNDARY_CHARS = "，,；;。!！？?：\n"
def _expand_text_span_to_boundaries(text: str, start: int, end: int) -> tuple[int, int]:
    source = str(text or "")
    if not source:
        return 0, 0
    start = max(0, min(int(start), len(source)))
    end = max(start, min(int(end), len(source)))

    if not (start > 0 and source[start - 1] in _TEXT_BOUNDARY_CHARS):
        prev_positions = [source.rfind(ch, 0, start) for ch in _TEXT_BOUNDARY_CHARS]
        prev_boundary = max(prev_positions) if prev_positions else -1
        if prev_boundary >= 0:
            start = prev_boundary + 1
        else:
            start = 0

    if not (end > 0 and source[end - 1] in _TEXT_BOUNDARY_CHARS):
        next_positions = [source.find(ch, end) for ch in _TEXT_BOUNDARY_CHARS]
        next_positions = [pos for pos in next_positions if pos >= 0]
        if next_positions:
            end = min(next_positions) + 1
        else:
            end = len(source)

    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    return start, end


def _shrink_aggregate_target_text(original_target_text: str | None, revised_text: str | None) -> str:
    original = str(original_target_text or "").strip()
    revised = str(revised_text or "").strip()
    if not original or not revised:
        return original
    if len(revised) >= len(original):
        return original

    matcher = SequenceMatcher(None, original, revised, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
    if not blocks:
        return original

    significant_blocks = [block for block in blocks if block.size >= 2]
    use_blocks = significant_blocks or blocks
    if len(use_blocks) < 2:
        return original

    first = use_blocks[0]
    last = use_blocks[-1]
    start = first.a
    end = last.a + last.size
    if start >= end:
        return original

    start, end = _expand_text_span_to_boundaries(original, start, end)
    candidate = original[start:end].strip()
    if not candidate:
        return original
    if len(candidate) >= len(original):
        return original

    candidate_matcher = SequenceMatcher(None, candidate, revised, autojunk=False)
    matched_chars = sum(block.size for block in candidate_matcher.get_matching_blocks() if block.size > 0)
    min_match_chars = max(6, min(len(revised), len(candidate)) // 3)
    if matched_chars < min_match_chars:
        return original

    if len(candidate) > int(len(original) * 0.95):
        return original

    return candidate


def _resolve_aggregate_patch_target(item: dict[str, Any], ai_payload: dict[str, Any], revised_text: str | None = None) -> str:
    if not isinstance(item, dict) or not isinstance(ai_payload, dict):
        return str(ai_payload.get("target_text") or "").strip()

    source_target = str(item.get("target_text") or item.get("clause_text") or "").strip()
    current_target = str(ai_payload.get("target_text") or "").strip()
    next_revised = str(revised_text or ai_payload.get("revised_text") or "").strip()

    workflow_kind = str(ai_payload.get("workflow_kind") or "").strip().lower()
    is_aggregate = workflow_kind == "aggregate" or bool(str(item.get("aggregate_id") or "").strip())
    if not is_aggregate:
        return current_target or source_target

    baseline = source_target or current_target
    if not baseline:
        return current_target

    shrunk = _shrink_aggregate_target_text(baseline, next_revised)
    return str(shrunk or current_target or baseline).strip()


def _minimize_patch_pair(target_text: str | None, revised_text: str | None) -> tuple[str, str]:
    before = str(target_text or "")
    after = str(revised_text or "")
    if not before:
        return before, after
    if not after:
        return before, after

    prefix = 0
    max_prefix = min(len(before), len(after))
    while prefix < max_prefix and before[prefix] == after[prefix]:
        prefix += 1

    suffix = 0
    max_suffix = min(len(before) - prefix, len(after) - prefix)
    while suffix < max_suffix and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]:
        suffix += 1

    minimized_before = before[prefix : len(before) - suffix if suffix > 0 else len(before)]
    minimized_after = after[prefix : len(after) - suffix if suffix > 0 else len(after)]

    if not minimized_before:
        return before, after

    if minimized_before == before and minimized_after == after:
        return before, after

    return minimized_before, minimized_after


def _finalize_aggregate_patch_pair(item: dict[str, Any], ai_payload: dict[str, Any], revised_text: str | None = None) -> tuple[str, str]:
    resolved_target = _resolve_aggregate_patch_target(item, ai_payload, revised_text)
    next_revised = str(revised_text or ai_payload.get("revised_text") or "").strip()
    # Keep the resolved span as target and never re-minimize to a tiny token
    # (e.g. "参照"), otherwise replace may miss and append at tail.
    # Also keep revised_text exactly as returned by Dify.
    return resolved_target, next_revised


def _meta_path(run_id: str) -> Path:
    return WEB_META_ROOT / f"{run_id}.json"


def _write_meta(run_id: str, payload: dict[str, Any]) -> None:
    current = {}
    path = _meta_path(run_id)
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(payload)
    current.setdefault("run_id", run_id)
    current["updated_at"] = datetime.utcnow().isoformat() + "Z"
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_iso_datetime(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _latest_mtime_iso(target: Path) -> str:
    latest = target.stat().st_mtime
    if target.is_dir():
        for p in target.rglob("*"):
            if p.is_file():
                latest = max(latest, p.stat().st_mtime)
    return datetime.utcfromtimestamp(latest).isoformat() + "Z"


def _infer_meta_from_run(run_id: str) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="run_id 不存在")

    merged_exists = (run_dir / "merged_clauses.json").exists()
    validated_path = run_dir / "risk_result_validated.json"
    status = "running"
    step = "历史运行记录"
    progress = 35
    error: str | None = None

    if merged_exists and validated_path.exists():
        validated = _safe_json(validated_path) or {}
        if bool(validated.get("is_valid")):
            status = "completed"
            step = "历史结果"
            progress = 100
        else:
            status = "failed"
            step = "历史结果校验失败"
            progress = 100
            error = validated.get("error_message") or "risk_result_validated.json 校验未通过"
    elif merged_exists:
        step = "历史运行记录（风险识别阶段）"
        progress = 65

    source_doc = run_dir / "source.docx"
    upload_doc = UPLOAD_ROOT / f"{run_id}.docx"
    reviewed_doc = run_dir / "reviewed_comments.docx"
    if source_doc.exists():
        file_name = source_doc.name
    elif upload_doc.exists():
        file_name = upload_doc.name
    elif reviewed_doc.exists():
        file_name = reviewed_doc.name
    else:
        file_name = f"{run_id}.docx"

    return {
        "run_id": run_id,
        "status": status,
        "file_name": file_name,
        "step": step,
        "progress": progress,
        "error": error,
        "updated_at": _latest_mtime_iso(run_dir),
    }


def _read_meta(run_id: str) -> dict[str, Any]:
    path = _meta_path(run_id)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("run_id", run_id)
        payload.setdefault("updated_at", datetime.utcnow().isoformat() + "Z")
        if payload.get("progress") is None:
            status = str(payload.get("status") or "")
            step = str(payload.get("step") or "")
            if status == "queued":
                payload["progress"] = 10
            elif status == "completed" or status == "failed":
                payload["progress"] = 100
            elif "风险" in step:
                payload["progress"] = 65
            elif "结果" in step or "导出" in step:
                payload["progress"] = 85
            else:
                payload["progress"] = 35
        if not str(payload.get("file_name") or "").strip():
            try:
                inferred = _infer_meta_from_run(run_id)
                payload["file_name"] = str(inferred.get("file_name") or "").strip()
            except Exception:
                payload["file_name"] = f"{run_id}.docx"

        current_status = str(payload.get("status") or "").strip().lower()
        if current_status in {"queued", "running"}:
            try:
                inferred = _infer_meta_from_run(run_id)
            except Exception:
                inferred = None
            payload_updated_at = _parse_iso_datetime(str(payload.get("updated_at") or ""))
            is_stale_running = payload_updated_at and (time.time() - payload_updated_at >= 300)
            if isinstance(inferred, dict) and str(inferred.get("status") or "") in {"completed", "failed"} and is_stale_running:
                payload["status"] = inferred.get("status")
                payload["step"] = inferred.get("step")
                payload["progress"] = inferred.get("progress")
                payload["error"] = inferred.get("error")
                payload["updated_at"] = inferred.get("updated_at")
        return payload
    return _infer_meta_from_run(run_id)


def _safe_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _short_text(value: str | None, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _extract_quoted_contract_text(text: str) -> str:
    candidates: list[str] = []
    for pattern in _QUOTED_TEXT_RE_LIST:
        for match in pattern.finditer(text):
            part = str(match.group(1) or "").strip()
            if not part:
                continue
            if _CLAUSE_UID_RE.fullmatch(part):
                continue
            candidates.append(part)
    if not candidates:
        return ""
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def _strip_leading_clause_label(text: str | None) -> str:
    cleaned = str(text or "").strip()
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        for pattern in _LEADING_CLAUSE_LABEL_RE_LIST:
            next_cleaned = pattern.sub("", cleaned, count=1).strip()
            if next_cleaned != cleaned:
                cleaned = next_cleaned
                break
    return cleaned


def _strip_outer_wrapping_quotes(text: str | None) -> str:
    cleaned = str(text or "").strip()
    quote_pairs = {
        '“': '”',
        '「': '」',
        '"': '"',
        "'": "'",
    }
    while len(cleaned) >= 2:
        opening = cleaned[0]
        closing = quote_pairs.get(opening)
        if not closing or cleaned[-1] != closing:
            break
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _sanitize_ai_target_text(text: str | None) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"\s+", " ", raw)

    raw_has_segment_prefix = bool(_TARGET_PREFIX_RE.match(raw))
    # Keep target_text cleaning conservative by default. In particular, do not
    # auto-extract the longest quoted fragment from contract text such as
    # 参照“甲方提供的验收指标”, otherwise the actionable phrase is reduced to the
    # quoted noun phrase and the replacement span becomes too small. The only
    # exception is model wrapper text with an explicit segment_xxx:: prefix,
    # where extracting the quoted clause body remains useful.
    cleaned = _TARGET_PREFIX_RE.sub("", raw, count=1)
    cleaned = _strip_leading_clause_label(cleaned)
    cleaned = _TARGET_INTRO_RE.sub("", cleaned, count=1)
    cleaned = _strip_outer_wrapping_quotes(cleaned)
    cleaned = _strip_leading_clause_label(cleaned)

    if not cleaned:
        return ""
    if _CLAUSE_UID_RE.fullmatch(cleaned):
        return ""
    if raw_has_segment_prefix:
        quoted = _extract_quoted_contract_text(cleaned) or _extract_quoted_contract_text(raw)
        if quoted:
            return quoted
    return cleaned


def _use_full_clause_target(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("target_text_source") or "").strip() == "host_clause_text"


def _normalize_target_text(raw_text: str | None, *, preserve_full_clause: bool = False) -> str:
    raw = str(raw_text or "").strip()
    if not raw:
        return ""
    if preserve_full_clause:
        return raw
    return _sanitize_ai_target_text(raw) or raw

def _find_clause_for_risk(risk: dict[str, Any], clauses: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_uid: dict[str, dict[str, Any]] = {}
    by_ref: dict[str, dict[str, Any]] = {}
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        uid = str(clause.get("clause_uid") or "").strip()
        if uid:
            by_uid[uid] = clause
            by_ref.setdefault(uid, clause)
        for field in ("clause_id", "display_clause_id", "local_clause_id", "source_clause_id"):
            for ref in _as_clause_ref_list(clause.get(field)):
                by_ref.setdefault(ref, clause)

    for field in ("clause_uids", "related_clause_uids", "clause_uid"):
        for uid in _as_clause_ref_list(risk.get(field)):
            clause = by_uid.get(uid) or by_ref.get(uid)
            if clause is not None:
                return clause

    for field in ("clause_ids", "related_clause_ids", "display_clause_ids", "clause_id", "display_clause_id"):
        for ref in _as_clause_ref_list(risk.get(field)):
            clause = by_ref.get(ref) or by_uid.get(ref)
            if clause is not None:
                return clause
    return None


def _clause_text_window(clause_text: str, target_text: str, limit: int = 1200) -> str:
    clause = str(clause_text or "").strip()
    if len(clause) <= limit:
        return clause
    target = str(target_text or "").strip()
    if target:
        idx = clause.find(target)
        if idx >= 0:
            half = limit // 2
            start = max(0, idx - half)
            end = min(len(clause), start + limit)
            if end - start < limit:
                start = max(0, end - limit)
            return clause[start:end]
    return clause[:limit]


def _parse_rewrite_outputs(outputs: dict[str, Any]) -> tuple[str, str, str]:
    structured = outputs.get("structured_output")
    structured_dict: dict[str, Any] | None = None
    if isinstance(structured, dict):
        structured_dict = structured
    elif isinstance(structured, str):
        cleaned = strip_markdown_json(structured)
        parsed = _load_json_with_repair(cleaned)
        if isinstance(parsed, dict):
            structured_dict = parsed

    if structured_dict is not None:
        revised_text = str(structured_dict.get("revised_text") or "").strip()
        rationale = str(structured_dict.get("rationale") or "").strip()
        edit_type = str(structured_dict.get("edit_type") or "").strip()
        if revised_text:
            return revised_text, rationale, edit_type

    revised_text = str(outputs.get("revised_text") or "").strip()
    rationale = str(outputs.get("rationale") or "").strip()
    edit_type = str(outputs.get("edit_type") or "").strip()
    if revised_text:
        return revised_text, rationale, edit_type

    text_payload = outputs.get("text")
    if not isinstance(text_payload, str):
        raise HTTPException(status_code=500, detail="rewrite workflow outputs 缺少 revised_text（structured_output/revised_text/text 均未提供）")
    cleaned = strip_markdown_json(text_payload)
    parsed = _load_json_with_repair(cleaned)
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=500, detail="rewrite workflow text 不是 JSON 对象")
    revised_text = str(parsed.get("revised_text") or "").strip()
    rationale = str(parsed.get("rationale") or "").strip()
    edit_type = str(parsed.get("edit_type") or "").strip()
    if not revised_text:
        raise HTTPException(status_code=500, detail="rewrite workflow 返回 revised_text 为空")
    return revised_text, rationale, edit_type


def _build_ai_comment_text(
    *,
    target_text: str,
    revised_text: str,
) -> str:
    before = str(target_text or "").strip()
    after = str(revised_text or "").strip()

    prefix = 0
    max_prefix = min(len(before), len(after))
    while prefix < max_prefix and before[prefix] == after[prefix]:
        prefix += 1

    suffix = 0
    max_suffix = min(len(before) - prefix, len(after) - prefix)
    while suffix < max_suffix and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]:
        suffix += 1

    before_changed = before[prefix : len(before) - suffix if suffix > 0 else len(before)]
    after_changed = after[prefix : len(after) - suffix if suffix > 0 else len(after)]
    before_piece = _short_text(before_changed or before, 120) or "原文片段"
    after_piece = _short_text(after_changed or after, 120) or "修改后片段"
    return f"将“{before_piece}”修改为“{after_piece}”。"


def _ensure_risk_items_status(payload: dict[str, Any]) -> dict[str, Any]:
    risk_items = (((payload or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        return payload
    for item in risk_items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "") or "").strip()
        item["status"] = status or "pending"
    return payload


def _is_accepted_risk_status(value: Any) -> bool:
    return str(value or "").strip().lower() in _ACCEPTED_RISK_STATUSES


def _as_clause_ref_list(value: Any) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    raw_values = value if isinstance(value, (list, tuple, set)) else [value]
    for raw in raw_values:
        text = str(raw or "").strip()
        if not text:
            continue
        parts = [p.strip() for p in _CLAUSE_REF_SPLIT_RE.split(text) if p.strip()]
        if not parts:
            continue
        for part in parts:
            if part in seen:
                continue
            seen.add(part)
            refs.append(part)
    return refs


def _load_run_clauses(run_dir: Path) -> list[dict[str, Any]]:
    payload = _safe_json(run_dir / "merged_clauses.json")
    raw_items: list[Any] = []
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("clauses"), list):
        raw_items = payload.get("clauses") or []
    return [item for item in raw_items if isinstance(item, dict)]


def _build_clause_uid_alias_map(clauses: list[dict[str, Any]]) -> dict[str, str]:
    alias: dict[str, str] = {}
    for clause in clauses:
        uid = str(clause.get("clause_uid") or "").strip()
        if not uid:
            continue
        alias.setdefault(uid, uid)
        for field in ("clause_id", "display_clause_id", "local_clause_id", "source_clause_id"):
            for ref in _as_clause_ref_list(clause.get(field)):
                alias.setdefault(ref, uid)
    return alias


def _collect_risk_clause_keys(risk: dict[str, Any], clause_alias_map: dict[str, str] | None = None) -> set[str]:
    alias_map = clause_alias_map or {}
    keys: set[str] = set()

    for field in ("clause_uids", "related_clause_uids", "clause_uid"):
        for uid in _as_clause_ref_list(risk.get(field)):
            keys.add(alias_map.get(uid) or uid)

    for field in ("clause_ids", "related_clause_ids", "display_clause_ids", "clause_id", "display_clause_id"):
        for ref in _as_clause_ref_list(risk.get(field)):
            keys.add(alias_map.get(ref) or ref)

    return keys


def _sanitize_reviewed_ai_payload(payload: dict[str, Any]) -> bool:
    risk_items = (((payload or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        return False

    changed = False
    for item in risk_items:
        if not isinstance(item, dict):
            continue
        preserve_full_clause = _use_full_clause_target(item)
        fallback_target = _normalize_target_text(
            str(item.get("target_text") or item.get("evidence_text") or item.get("anchor_text") or ""),
            preserve_full_clause=preserve_full_clause,
        )
        for field in ("ai_rewrite", "ai_apply"):
            ai_payload = item.get(field)
            if not isinstance(ai_payload, dict):
                continue
            old_target = str(ai_payload.get("target_text") or "").strip()
            cleaned_target = _normalize_target_text(old_target, preserve_full_clause=preserve_full_clause) or fallback_target
            if cleaned_target and cleaned_target != old_target:
                ai_payload["target_text"] = cleaned_target
                changed = True

            revised_text = str(ai_payload.get("revised_text") or "").strip()
            if not revised_text:
                continue
            workflow_kind = str(ai_payload.get("workflow_kind") or "").strip().lower()
            is_aggregate = workflow_kind == "aggregate" or bool(str(item.get("aggregate_id") or "").strip())
            if is_aggregate:
                target_for_comment, next_revised = _finalize_aggregate_patch_pair(item, ai_payload, revised_text)
                if target_for_comment and str(ai_payload.get("target_text") or "").strip() != target_for_comment:
                    ai_payload["target_text"] = target_for_comment
                    changed = True
                revised_text = next_revised
            else:
                target_for_comment = str(ai_payload.get("target_text") or "").strip() or fallback_target
            next_comment = _build_ai_comment_text(target_text=target_for_comment, revised_text=revised_text)
            if str(ai_payload.get("comment_text") or "").strip() != next_comment:
                ai_payload["comment_text"] = next_comment
                changed = True
    return changed

def _rewrite_client(*, aggregate: bool = False) -> DifyWorkflowClient:
    api_key = settings.aggregate_rewrite_api_key() if aggregate else settings.dify_rewrite_workflow_api_key
    if not api_key:
        missing_key = "DIFY_AGGREGATE_REWRITE_WORKFLOW_API_KEY / DIFY_REWRITE_WORKFLOW_API_KEY" if aggregate else "DIFY_REWRITE_WORKFLOW_API_KEY"
        raise HTTPException(status_code=500, detail=f"未配置 {missing_key}")
    return DifyWorkflowClient(
        base_url=settings.dify_base_url,
        api_key=api_key,
        timeout_seconds=settings.request_timeout_seconds,
    )


def _clone_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _risk_id_str(risk: dict[str, Any]) -> str:
    return str(risk.get("risk_id") or "").strip()


def _risk_source_type(risk: dict[str, Any]) -> str:
    return str(risk.get("risk_source_type") or "").strip().lower()


def _is_missing_clause_risk(risk: dict[str, Any] | None) -> bool:
    return _risk_source_type(risk or {}) == "missing_clause"


def _aggregation_file_path(run_dir: Path) -> Path:
    return run_dir / "risk_result_ai_aggregated.json"


def _aggregate_group_id(host_risk: dict[str, Any], clause_key: str | None = None) -> str:
    clause_ref = str(clause_key or "").strip()
    if not clause_ref:
        for field in ("clause_uid", "clause_uids", "clause_id", "display_clause_id", "clause_ids"):
            refs = _as_clause_ref_list(host_risk.get(field))
            if refs:
                clause_ref = refs[0]
                break
    clause_token = re.sub(r"[^A-Za-z0-9_.:-]+", "_", clause_ref or "clause")
    risk_token = re.sub(r"[^A-Za-z0-9_.:-]+", "_", _risk_id_str(host_risk) or "risk")
    return f"agg_{clause_token}_{risk_token}"


def _first_non_empty_text(values: list[Any]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _distinct_non_empty_texts(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _find_clause_by_key(clause_key: str, clauses: list[dict[str, Any]], alias_map: dict[str, str] | None = None) -> dict[str, Any] | None:
    alias = alias_map or _build_clause_uid_alias_map(clauses)
    normalized_key = alias.get(str(clause_key or "").strip()) or str(clause_key or "").strip()
    if not normalized_key:
        return None
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        clause_uid = str(clause.get("clause_uid") or "").strip()
        if clause_uid and clause_uid == normalized_key:
            return clause
        refs: set[str] = set()
        for field in ("clause_id", "display_clause_id", "local_clause_id", "source_clause_id"):
            refs.update(_as_clause_ref_list(clause.get(field)))
        if normalized_key in refs:
            return clause
    return None


def _select_group_representative_risk(anchored_risks: list[dict[str, Any]], multi_risks: list[dict[str, Any]]) -> dict[str, Any]:
    if anchored_risks:
        return anchored_risks[0]
    if multi_risks:
        return multi_risks[0]
    return {}


def _select_aggregate_target_text(
    host_risk: dict[str, Any],
    multi_risks: list[dict[str, Any]],
    clause_source: str,
) -> tuple[str, str]:
    clause_text = str(clause_source or "").strip()
    if clause_text:
        return clause_text, "host_clause_text"

    host_candidates = [
        ("anchored.target_text", str(host_risk.get("target_text") or "").strip()),
        ("anchored.evidence_text", str(host_risk.get("evidence_text") or "").strip()),
        ("anchored.anchor_text", str(host_risk.get("anchor_text") or "").strip()),
    ]
    for source_name, raw in host_candidates:
        cleaned = _sanitize_ai_target_text(raw)
        if cleaned:
            return cleaned, source_name

    for idx, multi_risk in enumerate(multi_risks, start=1):
        for field in ("main_text", "target_text", "evidence_text", "anchor_text"):
            raw = str(multi_risk.get(field) or "").strip()
            cleaned = _sanitize_ai_target_text(raw)
            if not cleaned:
                continue
            return cleaned, f"multi_clause[{idx - 1}].{field}"

    fallback = _sanitize_ai_target_text(clause_text) or clause_text
    return fallback, "host_clause_text"


def _build_ai_aggregation_groups(
    risk_items: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    alias_map = _build_clause_uid_alias_map(clauses)
    buckets: dict[str, dict[str, Any]] = {}

    for item in risk_items:
        if not isinstance(item, dict):
            continue
        source_type = _risk_source_type(item)
        if source_type not in {"anchored", "multi_clause"}:
            continue
        clause_keys = sorted(_collect_risk_clause_keys(item, alias_map))
        if not clause_keys:
            continue
        risk_id = _risk_id_str(item)
        for clause_key in clause_keys:
            bucket = buckets.setdefault(
                clause_key,
                {
                    "clause_key": clause_key,
                    "anchored_risks": [],
                    "multi_clause_risks": [],
                    "anchored_ids": set(),
                    "multi_ids": set(),
                },
            )
            if source_type == "anchored":
                if risk_id and risk_id in bucket["anchored_ids"]:
                    continue
                bucket["anchored_ids"].add(risk_id)
                bucket["anchored_risks"].append(_clone_jsonable(item))
            else:
                if risk_id and risk_id in bucket["multi_ids"]:
                    continue
                bucket["multi_ids"].add(risk_id)
                bucket["multi_clause_risks"].append(_clone_jsonable(item))

    groups: list[dict[str, Any]] = []
    for clause_key in sorted(buckets.keys()):
        bucket = buckets[clause_key]
        anchored_risks = list(bucket.get("anchored_risks") or [])
        multi_risks = list(bucket.get("multi_clause_risks") or [])
        if not anchored_risks or not multi_risks:
            continue

        representative = _select_group_representative_risk(anchored_risks, multi_risks)
        clause = _find_clause_by_key(clause_key, clauses, alias_map) or _find_clause_for_risk(representative, clauses)
        clause_source = ""
        if clause is not None:
            clause_source = str(clause.get("source_excerpt") or clause.get("clause_text") or "").strip()
        target_text, target_text_source = _select_aggregate_target_text(representative, multi_risks, clause_source)
        anchored_ids = [_risk_id_str(item) for item in anchored_risks if _risk_id_str(item)]
        multi_ids = [_risk_id_str(item) for item in multi_risks if _risk_id_str(item)]
        source_ids = anchored_ids + multi_ids
        aggregate_id = _aggregate_group_id(representative, clause_key=clause_key)
        group = {
            "aggregate_id": aggregate_id,
            "aggregate_scope": "clause",
            "aggregate_type": "anchored_multi_clause",
            "host_risk_id": _risk_id_str(representative),
            "representative_risk_id": _risk_id_str(representative),
            "host_clause_uid": str((clause or {}).get("clause_uid") or representative.get("clause_uid") or clause_key or "").strip(),
            "host_clause_id": str((clause or {}).get("display_clause_id") or (clause or {}).get("clause_id") or representative.get("clause_id") or representative.get("display_clause_id") or "").strip(),
            "source_risk_ids": source_ids,
            "anchored_risk_ids": anchored_ids,
            "multi_clause_risk_ids": multi_ids,
            "anchored_risk": _clone_jsonable(representative),
            "anchored_risks": anchored_risks,
            "multi_clause_risks": multi_risks,
            "target_text": str(target_text or ""),
            "target_text_source": target_text_source,
            "clause_text": str(clause_source or target_text or "").strip(),
        }
        groups.append(group)
    return groups


def _overlay_review_state(target_item: dict[str, Any], previous_item: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(previous_item, dict):
        target_item["status"] = str(target_item.get("status") or "pending").strip() or "pending"
        return target_item
    for field in ("status", "ai_rewrite", "ai_rewrite_decision", "accepted_patch", "locator"):
        if field in previous_item:
            target_item[field] = _clone_jsonable(previous_item[field])
    target_item["status"] = str(target_item.get("status") or "pending").strip() or "pending"
    return target_item


def _project_reviewed_risk_payload(
    *,
    run_dir: Path,
    validated: dict[str, Any],
    previous_reviewed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reviewed = _clone_jsonable(validated)
    risk_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        return reviewed

    previous_items = (((previous_reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    previous_by_id: dict[str, dict[str, Any]] = {}
    previous_by_aggregate_id: dict[str, dict[str, Any]] = {}
    if isinstance(previous_items, list):
        for item in previous_items:
            if not isinstance(item, dict):
                continue
            rid = _risk_id_str(item)
            aggregate_id = str(item.get("aggregate_id") or "").strip()
            if rid:
                previous_by_id[rid] = item
            if aggregate_id:
                previous_by_aggregate_id[aggregate_id] = item

    clauses = _load_run_clauses(run_dir)
    groups = _build_ai_aggregation_groups([item for item in risk_items if isinstance(item, dict)], clauses)
    grouped_member_ids: set[str] = set()
    groups_by_representative_id: dict[str, dict[str, Any]] = {}
    for group in groups:
        representative_id = str(group.get("representative_risk_id") or group.get("host_risk_id") or "").strip()
        if representative_id:
            groups_by_representative_id[representative_id] = group
        for source_risk_id in group.get("source_risk_ids") or []:
            sid = str(source_risk_id or "").strip()
            if sid and sid != representative_id:
                grouped_member_ids.add(sid)

    projected_items: list[dict[str, Any]] = []
    for raw_item in risk_items:
        if not isinstance(raw_item, dict):
            continue
        risk_id = _risk_id_str(raw_item)
        if risk_id in grouped_member_ids:
            continue

        item = _clone_jsonable(raw_item)
        group = groups_by_representative_id.get(risk_id)
        if group is not None:
            aggregate_id = str(group.get("aggregate_id") or "").strip()
            item["aggregate_id"] = aggregate_id
            item["aggregate_scope"] = str(group.get("aggregate_scope") or "clause")
            item["aggregate_type"] = str(group.get("aggregate_type") or "anchored_multi_clause")
            item["aggregate_source_types"] = ["anchored", "multi_clause"]
            item["aggregate_member_risk_ids"] = list(group.get("source_risk_ids") or [])
            item["aggregate_anchored_risk_ids"] = list(group.get("anchored_risk_ids") or [])
            item["aggregate_multi_clause_risk_ids"] = list(group.get("multi_clause_risk_ids") or [])
            item["source_risk_ids"] = list(group.get("source_risk_ids") or [])
            item["target_text"] = str(group.get("target_text") or item.get("target_text") or "")
            item["target_text_source"] = str(group.get("target_text_source") or "")
            item["host_clause_uid"] = str(group.get("host_clause_uid") or "")
            item["host_clause_id"] = str(group.get("host_clause_id") or "")
            item["risk_source_type"] = "anchored_multi_clause"
            previous_state = previous_by_aggregate_id.get(aggregate_id) or previous_by_id.get(risk_id)
        else:
            previous_state = previous_by_id.get(risk_id)
        _overlay_review_state(item, previous_state)
        projected_items.append(item)

    reviewed.setdefault("risk_result", {})["risk_items"] = projected_items
    return reviewed


def _sync_ai_aggregation_file(*, run_dir: Path, validated: dict[str, Any], reviewed: dict[str, Any]) -> dict[str, Any]:
    validated_items = (((validated or {}).get("risk_result") or {}).get("risk_items") or [])
    groups = _build_ai_aggregation_groups([item for item in validated_items if isinstance(item, dict)], _load_run_clauses(run_dir))

    reviewed_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    reviewed_by_id: dict[str, dict[str, Any]] = {}
    reviewed_by_aggregate_id: dict[str, dict[str, Any]] = {}
    if isinstance(reviewed_items, list):
        for item in reviewed_items:
            if not isinstance(item, dict):
                continue
            risk_id = _risk_id_str(item)
            aggregate_id = str(item.get("aggregate_id") or "").strip()
            if risk_id:
                reviewed_by_id[risk_id] = item
            if aggregate_id:
                reviewed_by_aggregate_id[aggregate_id] = item

    for group in groups:
        aggregate_id = str(group.get("aggregate_id") or "").strip()
        representative_risk_id = str(group.get("representative_risk_id") or group.get("host_risk_id") or "").strip()
        reviewed_item = reviewed_by_aggregate_id.get(aggregate_id)
        if not isinstance(reviewed_item, dict):
            for source_risk_id in group.get("source_risk_ids") or []:
                sid = str(source_risk_id or "").strip()
                if sid and sid in reviewed_by_id:
                    reviewed_item = reviewed_by_id[sid]
                    break
        if not isinstance(reviewed_item, dict) and representative_risk_id:
            reviewed_item = reviewed_by_id.get(representative_risk_id)
        if not isinstance(reviewed_item, dict):
            continue
        for field in ("status", "ai_rewrite", "ai_rewrite_decision", "accepted_patch"):
            if field in reviewed_item:
                group[field] = _clone_jsonable(reviewed_item[field])

    payload = {
        "version": 1,
        "generated_at": _iso_now(),
        "groups": groups,
    }
    _aggregation_file_path(run_dir).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _load_ai_aggregation_group(run_dir: Path, risk: dict[str, Any]) -> dict[str, Any] | None:
    aggregate_id = str(risk.get("aggregate_id") or "").strip()
    risk_id = _risk_id_str(risk)
    payload = _safe_json(_aggregation_file_path(run_dir))
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return None
    for group in groups:
        if not isinstance(group, dict):
            continue
        if aggregate_id and str(group.get("aggregate_id") or "").strip() == aggregate_id:
            return group
        source_risk_ids = {str(item or "").strip() for item in group.get("source_risk_ids") or [] if str(item or "").strip()}
        representative_risk_id = str(group.get("representative_risk_id") or group.get("host_risk_id") or "").strip()
        if risk_id and (risk_id in source_risk_ids or risk_id == representative_risk_id):
            return group
    return None


def _extract_target_text(risk: dict[str, Any]) -> str:
    preserve_full_clause = _use_full_clause_target(risk)
    candidates = [
        str(risk.get("target_text") or "").strip(),
        str(risk.get("evidence_text") or "").strip(),
        str(risk.get("anchor_text") or "").strip(),
    ]
    fallback = ""
    for raw in candidates:
        if raw and not fallback:
            fallback = raw
        cleaned = _normalize_target_text(raw, preserve_full_clause=preserve_full_clause)
        if cleaned:
            return cleaned
    return _normalize_target_text(fallback, preserve_full_clause=preserve_full_clause) or fallback

def _build_rewrite_inputs(*, run_id: str, run_dir: Path, risk: dict[str, Any]) -> dict[str, Any]:
    aggregate_group = _load_ai_aggregation_group(run_dir, risk)
    meta = _read_meta(run_id)

    if isinstance(aggregate_group, dict) and (aggregate_group.get("multi_clause_risks") or []):
        preserve_full_clause = _use_full_clause_target(aggregate_group)
        target_text = _normalize_target_text(
            str(aggregate_group.get("target_text") or ""),
            preserve_full_clause=preserve_full_clause,
        ) or _extract_target_text(risk)
        clause_text = str(aggregate_group.get("clause_text") or "").strip()
        anchored_risks = aggregate_group.get("anchored_risks") if isinstance(aggregate_group.get("anchored_risks"), list) else []
        anchored_risk = aggregate_group.get("anchored_risk") if isinstance(aggregate_group.get("anchored_risk"), dict) else (anchored_risks[0] if anchored_risks and isinstance(anchored_risks[0], dict) else {})
        multi_clause_risks = aggregate_group.get("multi_clause_risks") if isinstance(aggregate_group.get("multi_clause_risks"), list) else []
        suggestion = _first_non_empty_text([risk.get("suggestion")] + [item.get("suggestion") for item in anchored_risks])
        issue_values = _distinct_non_empty_texts([risk.get("issue")] + [item.get("issue") for item in anchored_risks])
        label_values = _distinct_non_empty_texts([risk.get("risk_label")] + [item.get("risk_label") for item in anchored_risks])
        issue = "；".join(issue_values[:5])
        risk_label = "；".join(label_values[:5])
        inputs = {
            "target_text": str(target_text or ""),
            "suggestion": suggestion,
            "clause_text": str(clause_text or ""),
            "issue": issue,
            "risk_label": risk_label,
            "review_side": meta.get("review_side"),
            "contract_type_hint": meta.get("contract_type_hint"),
            "anchored_risks_json": json.dumps(anchored_risks, ensure_ascii=False),
            "anchored_risk_json": json.dumps(anchored_risk, ensure_ascii=False),
            "multi_clause_risks_json": json.dumps(multi_clause_risks, ensure_ascii=False),
            "aggregate_id": str(aggregate_group.get("aggregate_id") or ""),
            "host_clause_uid": str(aggregate_group.get("host_clause_uid") or ""),
            "host_clause_id": str(aggregate_group.get("host_clause_id") or ""),
            "target_text_source": str(aggregate_group.get("target_text_source") or ""),
        }
        if len(multi_clause_risks) == 1 and isinstance(multi_clause_risks[0], dict):
            inputs["multi_clause_risk_json"] = json.dumps(multi_clause_risks[0], ensure_ascii=False)
        return inputs

    target_text = _extract_target_text(risk)
    merged_path = run_dir / "merged_clauses.json"
    merged_clauses = _safe_json(merged_path)
    if not isinstance(merged_clauses, list):
        raise HTTPException(status_code=404, detail="merged_clauses.json 不存在或格式错误")
    clause = _find_clause_for_risk(risk, merged_clauses)
    clause_source = ""
    if clause is not None:
        clause_source = str(clause.get("source_excerpt") or clause.get("clause_text") or "").strip()
    clause_text = _clause_text_window(clause_source, target_text, limit=1200)

    suggestion = str(risk.get("suggestion") or "").strip()
    return {
        "target_text": str(target_text or ""),
        "suggestion": suggestion,
        "clause_text": str(clause_text or ""),
        "issue": str(risk.get("issue") or ""),
        "risk_label": str(risk.get("risk_label") or ""),
        "review_side": meta.get("review_side"),
        "contract_type_hint": meta.get("contract_type_hint"),
    }


def _generate_ai_rewrite(
    *,
    run_id: str,
    run_dir: Path,
    risk: dict[str, Any],
    client: DifyWorkflowClient | None = None,
) -> dict[str, Any]:
    aggregate_group = _load_ai_aggregation_group(run_dir, risk)
    is_aggregate = isinstance(aggregate_group, dict) and bool(aggregate_group.get("multi_clause_risks"))
    active_client = client or _rewrite_client(aggregate=is_aggregate)
    inputs = _build_rewrite_inputs(run_id=run_id, run_dir=run_dir, risk=risk)
    workflow_response = active_client.run_workflow(inputs=inputs, user=f"rewrite-{run_id}", response_mode="blocking")
    outputs = extract_blocking_outputs(workflow_response)
    revised_text, rationale, edit_type = _parse_rewrite_outputs(outputs)
    preserve_full_clause = _use_full_clause_target(aggregate_group if is_aggregate else risk)
    target_text = _normalize_target_text(
        str(inputs.get("target_text") or ""),
        preserve_full_clause=preserve_full_clause,
    ) or _extract_target_text(risk)
    rewrite_target_text = target_text
    rewrite_revised_text = revised_text
    if is_aggregate and isinstance(aggregate_group, dict):
        rewrite_target_text, rewrite_revised_text = _finalize_aggregate_patch_pair(
            aggregate_group,
            {"target_text": target_text, "revised_text": revised_text, "workflow_kind": "aggregate"},
            revised_text,
        )
    return {
        "state": "succeeded",
        "target_text": rewrite_target_text,
        "revised_text": rewrite_revised_text,
        "comment_text": _build_ai_comment_text(target_text=rewrite_target_text, revised_text=rewrite_revised_text),
        "rationale": rationale,
        "edit_type": edit_type or "replace",
        "workflow_kind": "aggregate" if is_aggregate else "default",
        "created_at": _iso_now(),
    }


def get_or_create_reviewed_risks(run_id: str) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_id 不存在")

    reviewed_path = run_dir / "risk_result_reviewed.json"
    validated_path = run_dir / "risk_result_validated.json"

    validated = _safe_json(validated_path)
    if not isinstance(validated, dict):
        raise HTTPException(status_code=404, detail="risk_result_validated.json 不存在")

    previous_reviewed = _safe_json(reviewed_path) if reviewed_path.exists() else None
    if previous_reviewed is not None and not isinstance(previous_reviewed, dict):
        raise HTTPException(status_code=500, detail="risk_result_reviewed.json 格式错误")

    reviewed = _project_reviewed_risk_payload(run_dir=run_dir, validated=validated, previous_reviewed=previous_reviewed)
    reviewed = _ensure_risk_items_status(reviewed)
    _sanitize_reviewed_ai_payload(reviewed)
    reviewed_path.write_text(json.dumps(reviewed, ensure_ascii=False, indent=2), encoding="utf-8")
    _sync_ai_aggregation_file(run_dir=run_dir, validated=validated, reviewed=reviewed)
    return reviewed


def _persist_reviewed_payload(run_dir: Path, reviewed: dict[str, Any]) -> None:
    reviewed_path = run_dir / "risk_result_reviewed.json"
    reviewed_path.write_text(json.dumps(reviewed, ensure_ascii=False, indent=2), encoding="utf-8")
    validated = _safe_json(run_dir / "risk_result_validated.json")
    if isinstance(validated, dict):
        _sync_ai_aggregation_file(run_dir=run_dir, validated=validated, reviewed=reviewed)


def _build_result_payload(run_id: str) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    clauses = _safe_json(run_dir / "merged_clauses.json")
    if clauses is None:
        raise HTTPException(status_code=404, detail="结果尚未生成完成")
    validated = get_or_create_reviewed_risks(run_id)
    meta = _read_meta(run_id)
    reviewed_docx = run_dir / "reviewed_comments.docx"
    return {
        "run_id": run_id,
        "status": meta.get("status"),
        "file_name": meta.get("file_name"),
        "review_side": meta.get("review_side"),
        "contract_type_hint": meta.get("contract_type_hint"),
        "merged_clauses": clauses,
        "risk_result_validated": validated,
        "risk_result_ai_aggregated": _safe_json(_aggregation_file_path(run_dir)),
        "download_ready": reviewed_docx.exists(),
        "download_url": f"/api/reviews/{run_id}/download" if reviewed_docx.exists() else None,
    }


def _resolve_document_path(run_id: str) -> Path | None:
    run_dir = RUN_ROOT / run_id
    for candidate in (
        run_dir / "source.docx",
        UPLOAD_ROOT / f"{run_id}.docx",
        run_dir / "reviewed_comments.docx",
    ):
        if candidate.exists():
            return candidate
    return None


def _to_history_item(meta: dict[str, Any]) -> dict[str, Any]:
    run_id = str(meta.get("run_id") or "")
    run_dir = RUN_ROOT / run_id
    reviewed_docx = run_dir / "reviewed_comments.docx"
    document_path = _resolve_document_path(run_id)
    return {
        "run_id": run_id,
        "file_name": meta.get("file_name"),
        "status": meta.get("status") or "running",
        "review_side": meta.get("review_side"),
        "contract_type_hint": meta.get("contract_type_hint"),
        "updated_at": meta.get("updated_at") or (_latest_mtime_iso(run_dir) if run_dir.exists() else None),
        "step": meta.get("step"),
        "warning": meta.get("warning"),
        "error": meta.get("error"),
        "download_ready": reviewed_docx.exists(),
        "document_ready": document_path is not None,
    }


def _list_history_items(limit: int) -> list[dict[str, Any]]:
    run_ids: set[str] = set()
    for path in WEB_META_ROOT.glob("*.json"):
        run_ids.add(path.stem)
    for path in RUN_ROOT.iterdir():
        if path.is_dir():
            run_ids.add(path.name)

    items: list[dict[str, Any]] = []
    for run_id in run_ids:
        try:
            meta = _read_meta(run_id)
        except HTTPException:
            continue
        item = _to_history_item(meta)
        items.append(item)

    items.sort(key=lambda x: _parse_iso_datetime(x.get("updated_at")), reverse=True)
    return items[:limit]


def _normalize_review_side(value: str | None) -> str:
    raw = str(value or "").strip()
    mapping = {
        "supplier": "乙方",
        "vendor": "乙方",
        "party_b": "乙方",
        "乙方": "乙方",
        "customer": "甲方",
        "buyer": "甲方",
        "party_a": "甲方",
        "甲方": "甲方",
    }
    normalized = mapping.get(raw.lower(), mapping.get(raw, ""))
    if normalized:
        return normalized
    raise HTTPException(status_code=400, detail='review_side 仅支持"甲方"或"乙方"')


def _run_pipeline(*, run_id: str, file_path: Path, file_name: str, review_side: str, contract_type_hint: str) -> None:
    run_dir = RUN_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    source_docx = run_dir / "source.docx"
    if not source_docx.exists():
        try:
            shutil.copy2(file_path, source_docx)
        except Exception:
            pass
    env = os.environ.copy()
    env["RUN_ROOT"] = str(RUN_ROOT)
    env["REVIEW_SIDE"] = review_side
    env["CONTRACT_TYPE_HINT"] = contract_type_hint

    _write_meta(
        run_id,
        {
            "status": "running",
            "file_name": file_name,
            "review_side": review_side,
            "contract_type_hint": contract_type_hint,
            "run_dir": str(run_dir),
            "step": "排队完成，准备开始审查",
            "progress": 15,
        },
    )

    cmd = ["python", "app.py", str(file_path), "--run-id", run_id]
    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    last_phase = ""
    while True:
        merged_ready = (run_dir / "merged_clauses.json").exists()
        validated_ready = (run_dir / "risk_result_validated.json").exists()
        if validated_ready:
            phase = "assemble"
            phase_step = "风险识别完成，正在生成结果"
            phase_progress = 85
        elif merged_ready:
            phase = "scan"
            phase_step = "正在识别风险点"
            phase_progress = 65
        else:
            phase = "parse"
            phase_step = "正在解析与拆分合同"
            phase_progress = 35

        if phase != last_phase:
            _write_meta(
                run_id,
                {
                    "status": "running",
                    "step": phase_step,
                    "progress": phase_progress,
                },
            )
            last_phase = phase

        if proc.poll() is not None:
            break
        time.sleep(1.0)

    stdout, stderr = proc.communicate()
    (run_dir / "app.stdout.log").write_text(stdout or "", encoding="utf-8")
    (run_dir / "app.stderr.log").write_text(stderr or "", encoding="utf-8")

    if proc.returncode != 0:
        _write_meta(
            run_id,
            {
                "status": "failed",
                "step": "主流程执行失败",
                "progress": 100,
                "error": (stderr or stdout or "未知错误").strip(),
            },
        )
        return

    validated = _safe_json(run_dir / "risk_result_validated.json") or {}
    is_valid = bool(validated.get("is_valid"))
    if not is_valid:
        _write_meta(
            run_id,
            {
                "status": "failed",
                "step": "风险结果校验失败",
                "error": validated.get("error_message") or "risk_result_validated.json 校验未通过",
            },
        )
        return

    _write_meta(
        run_id,
        {
            "status": "running",
            "step": "风险识别完成，正在导出结果文档",
            "progress": 92,
        },
    )

    export_cmd = [
        "python",
        "-m",
        "src.docx_comments",
        str(file_path),
        str(run_dir / "merged_clauses.json"),
        str(run_dir / "risk_result_validated.json"),
        "--out",
        str(run_dir / "reviewed_comments.docx"),
        "--author",
        "合同审查系统",
    ]
    export_proc = subprocess.run(
        export_cmd,
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    (run_dir / "export.stdout.log").write_text(export_proc.stdout or "", encoding="utf-8")
    (run_dir / "export.stderr.log").write_text(export_proc.stderr or "", encoding="utf-8")

    export_warning = ""
    export_completed = export_proc.returncode == 0
    if not export_completed:
        export_warning = (export_proc.stderr or export_proc.stdout or "DOCX 导出失败").strip()

    _write_meta(
        run_id,
        {
            "status": "running",
            "step": "风险识别完成，正在生成 AI 改写建议",
            "progress": 96,
            "warning": export_warning or None,
        },
    )

    ai_summary, ai_warning = _maybe_auto_generate_ai_rewrites(run_id)

    final_warnings = [part for part in [export_warning, ai_warning] if str(part or "").strip()]
    final_payload: dict[str, Any] = {
        "status": "completed",
        "progress": 100,
        "warning": "；".join(final_warnings) if final_warnings else None,
        "ai_rewrite_summary": (ai_summary or {}).get("summary") if isinstance(ai_summary, dict) else None,
    }
    if export_completed:
        final_payload["step"] = "审查、AI 改写与 DOCX 批注导出已完成"
    else:
        final_payload["step"] = "审查与 AI 改写已完成，但 DOCX 导出失败"

    _write_meta(run_id, final_payload)


@app.get("/api/config")
def get_config() -> dict[str, str]:
    normalized_review_side = '乙方'
    try:
        normalized_review_side = _normalize_review_side(settings.review_side) if str(settings.review_side or '').strip() else '乙方'
    except HTTPException:
        normalized_review_side = '乙方'
    return {
        "review_side": normalized_review_side,
        "contract_type_hint": settings.contract_type_hint,
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/reviews")
async def create_review(
    file: UploadFile = File(...),
    review_side: str = Form(settings.review_side),
    contract_type_hint: str = Form("service_agreement"),
) -> dict[str, Any]:
    normalized_review_side = _normalize_review_side(review_side or settings.review_side)
    suffix = Path(file.filename or "contract.docx").suffix.lower()
    if suffix != ".docx":
        raise HTTPException(status_code=400, detail="目前仅支持 .docx 文件")

    run_id = datetime.now().strftime("web_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    upload_path = UPLOAD_ROOT / f"{run_id}{suffix}"
    with upload_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    run_dir = RUN_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(upload_path, run_dir / "source.docx")
    except Exception:
        pass

    _write_meta(
        run_id,
        {
            "status": "queued",
            "file_name": file.filename,
            "review_side": normalized_review_side,
            "contract_type_hint": contract_type_hint,
            "step": "任务已创建，等待执行",
            "progress": 8,
        },
    )
    threading.Thread(
        target=_run_pipeline,
        kwargs=dict(
            run_id=run_id,
            file_path=upload_path,
            file_name=file.filename or upload_path.name,
            review_side=normalized_review_side,
            contract_type_hint=contract_type_hint,
        ),
        daemon=True,
    ).start()
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/reviews/history")
def get_review_history(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    return {"items": _list_history_items(limit)}


@app.get("/api/reviews/{run_id}")
def get_review_status(run_id: str) -> dict[str, Any]:
    return _read_meta(run_id)


@app.get("/api/reviews/{run_id}/result")
def get_review_result(run_id: str) -> dict[str, Any]:
    meta = _read_meta(run_id)
    if meta.get("status") != "completed":
        raise HTTPException(status_code=409, detail="任务尚未完成")
    return _build_result_payload(run_id)


@app.get("/api/reviews/{run_id}/document")
def get_review_document(run_id: str) -> FileResponse:
    output = _resolve_document_path(run_id)
    if output is None:
        raise HTTPException(status_code=404, detail="未找到该 run 对应的 DOCX")
    meta = _read_meta(run_id)
    preferred_name = str(meta.get("file_name") or output.name or f"{run_id}.docx").strip() or output.name
    return FileResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=preferred_name,
    )


class RiskPatchBody(BaseModel):
    status: str


class AiAcceptBody(BaseModel):
    revised_text: str | None = None
    target_text: str | None = None


class AiEditBody(BaseModel):
    revised_text: str


def _export_docx_with_reviewed_risks(run_id: str) -> Path:
    run_dir = RUN_ROOT / run_id
    source_doc = run_dir / "source.docx"
    if not source_doc.exists():
        upload_doc = UPLOAD_ROOT / f"{run_id}.docx"
        if upload_doc.exists():
            source_doc = upload_doc
        else:
            raise HTTPException(status_code=404, detail="原始 DOCX 不存在")

    merged_path = run_dir / "merged_clauses.json"
    if not merged_path.exists():
        raise HTTPException(status_code=404, detail="merged_clauses.json 不存在")

    reviewed_payload = get_or_create_reviewed_risks(run_id)
    reviewed_path = run_dir / "risk_result_reviewed.json"
    reviewed_path.write_text(json.dumps(reviewed_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    patched_docx = run_dir / "ai_patched.docx"
    patch_cmd = [
        "python",
        "-m",
        "src.docx_apply_patches",
        str(source_doc),
        str(reviewed_path),
        "--out",
        str(patched_docx),
        "--author",
        "合同审查系统",
    ]
    patch_proc = subprocess.run(
        patch_cmd,
        cwd=str(BASE_DIR),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    out_path = run_dir / "reviewed_comments.docx"
    comment_cmd = [
        "python",
        "-m",
        "src.docx_comments",
        str(patched_docx),
        str(merged_path),
        str(reviewed_path),
        "--out",
        str(out_path),
        "--author",
        "合同审查系统",
        "--statuses",
        "accepted",
    ]
    comment_proc = subprocess.run(
        comment_cmd,
        cwd=str(BASE_DIR),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    stdout = "\n".join(
        [
            "[ai_patch]",
            patch_proc.stdout or "",
            "[risk_comments]",
            comment_proc.stdout or "",
        ]
    )
    stderr = "\n".join(
        [
            "[ai_patch]",
            patch_proc.stderr or "",
            "[risk_comments]",
            comment_proc.stderr or "",
        ]
    )
    (run_dir / "export.stdout.log").write_text(stdout, encoding="utf-8")
    (run_dir / "export.stderr.log").write_text(stderr, encoding="utf-8")
    if patch_proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(patch_proc.stderr or patch_proc.stdout or "AI 改写应用失败").strip()[:1000],
        )
    if comment_proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(comment_proc.stderr or comment_proc.stdout or "DOCX 导出失败").strip()[:1000],
        )
    return out_path


@app.get("/api/reviews/{run_id}/download")
def download_reviewed_docx(run_id: str) -> FileResponse:
    output = _export_docx_with_reviewed_risks(run_id)
    meta = _read_meta(run_id)
    original_name = str(meta.get("file_name") or f"{run_id}.docx").strip() or f"{run_id}.docx"
    stem = Path(original_name).stem or run_id
    filename = f"{stem}_reviewed_comments.docx"
    return FileResponse(output, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=filename)


@app.patch("/api/reviews/{run_id}/risks/{risk_id}")
def patch_risk_status(run_id: str, risk_id: str, body: RiskPatchBody) -> dict[str, Any]:
    status = str(body.status or "").strip().lower()
    if status not in {"pending", "accepted", "rejected"}:
        raise HTTPException(status_code=400, detail="status 仅支持 pending/accepted/rejected")

    run_dir = RUN_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_id 不存在")

    reviewed = get_or_create_reviewed_risks(run_id)
    risk_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        raise HTTPException(status_code=500, detail="reviewed 风险数据格式错误")

    target: dict[str, Any] | None = None
    for item in risk_items:
        if isinstance(item, dict) and str(item.get("risk_id", "")) == str(risk_id):
            target = item
            break

    if target is None:
        raise HTTPException(status_code=404, detail="risk_id 不存在")

    target["status"] = status
    if status == "accepted":
        ai_rewrite = target.get("ai_rewrite") if isinstance(target.get("ai_rewrite"), dict) else {}
        if str(ai_rewrite.get("state") or "").strip().lower() == "succeeded":
            target["ai_rewrite_decision"] = "accepted"
    elif status == "rejected":
        target["ai_rewrite_decision"] = "rejected"
    elif status == "pending":
        if isinstance(target.get("ai_rewrite"), dict):
            target["ai_rewrite_decision"] = "proposed"

    _persist_reviewed_payload(run_dir, reviewed)
    return {"ok": True, "item": target}


@app.post("/api/reviews/{run_id}/risks/accept_all")
def accept_all_risks(run_id: str) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_id 不存在")

    reviewed = get_or_create_reviewed_risks(run_id)
    risk_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        raise HTTPException(status_code=500, detail="reviewed 风险数据格式错误")

    accepted = 0
    skipped = 0
    for item in risk_items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status == "rejected":
            skipped += 1
            continue
        if _is_accepted_risk_status(status):
            skipped += 1
            continue
        item["status"] = "accepted"
        ai_rewrite = item.get("ai_rewrite") if isinstance(item.get("ai_rewrite"), dict) else {}
        ai_state = str(ai_rewrite.get("state") or "").strip().lower()
        if ai_state == "succeeded":
            item["ai_rewrite_decision"] = "accepted"
        accepted += 1

    _persist_reviewed_payload(run_dir, reviewed)
    return {"ok": True, "summary": {"accepted": accepted, "skipped": skipped}, "risk_items": risk_items}


@app.post("/api/reviews/{run_id}/risks/{risk_id}/ai_apply")
def ai_apply_risk(run_id: str, risk_id: str) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_id 不存在")

    reviewed = get_or_create_reviewed_risks(run_id)
    risk_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        raise HTTPException(status_code=500, detail="reviewed 风险数据格式错误")

    target: dict[str, Any] | None = None
    for item in risk_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("risk_id", "")) == str(risk_id):
            target = item
            break
    if target is None:
        raise HTTPException(status_code=404, detail="risk_id 不存在")
    if str(target.get("status") or "pending").strip().lower() == "rejected":
        raise HTTPException(status_code=409, detail="rejected 风险不允许 AI 自动修改")
    target["ai_rewrite"] = _generate_ai_rewrite(run_id=run_id, run_dir=run_dir, risk=target)
    target["ai_rewrite_decision"] = "proposed"

    _persist_reviewed_payload(run_dir, reviewed)
    return {"ok": True, "item": target}


def _ai_apply_all_risks_impl(run_id: str) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_id 不存在")

    reviewed = get_or_create_reviewed_risks(run_id)
    risk_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        raise HTTPException(status_code=500, detail="reviewed 风险数据格式错误")

    total = len(risk_items)
    created = 0
    skipped = 0
    failed = 0
    tasks: list[tuple[int, dict[str, Any]]] = []
    for idx, item in enumerate(risk_items):
        if not isinstance(item, dict):
            skipped += 1
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status == "rejected":
            skipped += 1
            continue
        if str(item.get("risk_source_type") or "").strip().lower() == "missing_clause":
            skipped += 1
            continue
        ai_rewrite = item.get("ai_rewrite") if isinstance(item.get("ai_rewrite"), dict) else {}
        if str(ai_rewrite.get("state") or "").strip().lower() == "succeeded":
            skipped += 1
            continue
        tasks.append((idx, item))

    max_workers = max(
        1,
        int(os.getenv("AI_REWRITE_MAX_CONCURRENCY", str(getattr(settings, "dify_max_concurrency", 6))) or 6),
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map: dict[Any, int] = {}
        for idx, risk in tasks:
            future = executor.submit(_generate_ai_rewrite, run_id=run_id, run_dir=run_dir, risk=dict(risk))
            future_map[future] = idx
        for future in as_completed(future_map):
            idx = future_map[future]
            risk = risk_items[idx]
            try:
                ai_rewrite = future.result()
                risk["ai_rewrite"] = ai_rewrite
                risk["ai_rewrite_decision"] = "proposed"
                created += 1
            except Exception as exc:
                failed += 1
                failure_target = _extract_target_text(risk)
                aggregate_group = _load_ai_aggregation_group(run_dir, risk)
                if isinstance(aggregate_group, dict) and (aggregate_group.get("multi_clause_risks") or []):
                    preserve_full_clause = _use_full_clause_target(aggregate_group)
                    failure_target = _normalize_target_text(
                        str(aggregate_group.get("target_text") or ""),
                        preserve_full_clause=preserve_full_clause,
                    ) or failure_target
                risk["ai_rewrite"] = {
                    "state": "failed",
                    "target_text": failure_target,
                    "revised_text": "",
                    "comment_text": str(exc),
                    "workflow_kind": "aggregate" if str(risk.get("aggregate_id") or "").strip() else "default",
                    "created_at": _iso_now(),
                }
            _persist_reviewed_payload(run_dir, reviewed)

    _persist_reviewed_payload(run_dir, reviewed)
    return {
        "ok": True,
        "summary": {
            "total": total,
            "created": created,
            "skipped": skipped,
            "failed": failed,
        },
        "risk_items": risk_items,
    }


def _maybe_auto_generate_ai_rewrites(run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    api_key = str(settings.dify_rewrite_workflow_api_key or "").strip()
    if not api_key:
        return None, "未配置 DIFY_REWRITE_WORKFLOW_API_KEY，已跳过 AI 改写建议生成"
    try:
        return _ai_apply_all_risks_impl(run_id), None
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
        return None, str(detail or exc)
    except Exception as exc:
        return None, str(exc)


@app.post("/api/reviews/{run_id}/ai_apply_all")
def ai_apply_all_risks(run_id: str) -> dict[str, Any]:
    return _ai_apply_all_risks_impl(run_id)


@app.post("/api/reviews/{run_id}/risks/{risk_id}/ai_accept")
def ai_accept_risk(run_id: str, risk_id: str, body: AiAcceptBody) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_id 不存在")
    reviewed = get_or_create_reviewed_risks(run_id)
    risk_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        raise HTTPException(status_code=500, detail="reviewed 风险数据格式错误")

    target: dict[str, Any] | None = None
    for item in risk_items:
        if isinstance(item, dict) and str(item.get("risk_id", "")) == str(risk_id):
            target = item
            break
    if target is None:
        raise HTTPException(status_code=404, detail="risk_id 不存在")

    ai_rewrite = target.get("ai_rewrite") if isinstance(target.get("ai_rewrite"), dict) else None
    if not ai_rewrite or str(ai_rewrite.get("state") or "") != "succeeded":
        raise HTTPException(status_code=409, detail="当前风险不存在可接受的 AI 改写建议")

    revised_text = str(body.revised_text or "").strip()
    preserve_full_clause = _use_full_clause_target(target)
    is_aggregate = str(ai_rewrite.get("workflow_kind") or "").strip().lower() == "aggregate" or bool(str(target.get("aggregate_id") or "").strip())
    existing_target = _normalize_target_text(
        str(ai_rewrite.get("target_text") or ""),
        preserve_full_clause=preserve_full_clause,
    )
    submitted_target = "" if is_aggregate else _normalize_target_text(str(body.target_text or ""), preserve_full_clause=preserve_full_clause)
    if is_aggregate:
        current_target = submitted_target or existing_target
    else:
        current_target = existing_target or _extract_target_text(target)
    if revised_text:
        ai_rewrite["revised_text"] = revised_text
    if is_aggregate:
        current_target, _ = _finalize_aggregate_patch_pair(
            target,
            ai_rewrite,
            str(ai_rewrite.get("revised_text") or revised_text or ""),
        )
    if current_target:
        ai_rewrite["target_text"] = current_target
    ai_rewrite["comment_text"] = _build_ai_comment_text(
        target_text=str(ai_rewrite.get("target_text") or current_target or ""),
        revised_text=str(ai_rewrite.get("revised_text") or ""),
    )
    target["status"] = "ai_applied"
    target["ai_rewrite_decision"] = "accepted"

    _persist_reviewed_payload(run_dir, reviewed)
    return {"ok": True, "item": target}


@app.patch("/api/reviews/{run_id}/risks/{risk_id}/ai_edit")
def ai_edit_risk(run_id: str, risk_id: str, body: AiEditBody) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_id 不存在")
    reviewed = get_or_create_reviewed_risks(run_id)
    risk_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        raise HTTPException(status_code=500, detail="reviewed 风险数据格式错误")

    target: dict[str, Any] | None = None
    for item in risk_items:
        if isinstance(item, dict) and str(item.get("risk_id", "")) == str(risk_id):
            target = item
            break
    if target is None:
        raise HTTPException(status_code=404, detail="risk_id 不存在")

    ai_rewrite = target.get("ai_rewrite") if isinstance(target.get("ai_rewrite"), dict) else None
    if not ai_rewrite or str(ai_rewrite.get("state") or "") != "succeeded":
        raise HTTPException(status_code=409, detail="当前风险不存在可编辑的 AI 改写建议")

    revised_text = str(body.revised_text or "").strip()
    if not revised_text:
        raise HTTPException(status_code=400, detail="revised_text 不能为空")

    preserve_full_clause = _use_full_clause_target(target)
    current_target = _normalize_target_text(
        str(ai_rewrite.get("target_text") or ""),
        preserve_full_clause=preserve_full_clause,
    )
    ai_rewrite["revised_text"] = revised_text
    workflow_kind = str(ai_rewrite.get("workflow_kind") or "").strip().lower()
    is_aggregate = workflow_kind == "aggregate" or bool(str(target.get("aggregate_id") or "").strip())
    if is_aggregate:
        resolved_target, _ = _finalize_aggregate_patch_pair(target, ai_rewrite, revised_text)
    else:
        resolved_target = current_target
    if resolved_target:
        ai_rewrite["target_text"] = resolved_target
        current_target = resolved_target
    elif current_target:
        ai_rewrite["target_text"] = current_target
    ai_rewrite["comment_text"] = _build_ai_comment_text(
        target_text=str(ai_rewrite.get("target_text") or current_target or ""),
        revised_text=str(ai_rewrite.get("revised_text") or revised_text),
    )
    target["ai_rewrite_decision"] = "proposed"

    _persist_reviewed_payload(run_dir, reviewed)
    return {"ok": True, "item": target}


@app.post("/api/reviews/{run_id}/risks/{risk_id}/ai_reject")
def ai_reject_risk(run_id: str, risk_id: str) -> dict[str, Any]:
    run_dir = RUN_ROOT / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="run_id 不存在")
    reviewed = get_or_create_reviewed_risks(run_id)
    risk_items = (((reviewed or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        raise HTTPException(status_code=500, detail="reviewed 风险数据格式错误")

    target: dict[str, Any] | None = None
    for item in risk_items:
        if isinstance(item, dict) and str(item.get("risk_id", "")) == str(risk_id):
            target = item
            break
    if target is None:
        raise HTTPException(status_code=404, detail="risk_id 不存在")

    target.pop("ai_rewrite", None)
    target["ai_rewrite_decision"] = "rejected"
    target["status"] = "rejected"

    _persist_reviewed_payload(run_dir, reviewed)
    return {"ok": True, "item": target}
