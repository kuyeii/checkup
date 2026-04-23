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

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from config import settings
from src.clause_ref_display import build_clause_alias_map, humanize_clause_refs
from src.dify_client import DifyWorkflowClient, DifyWorkflowError, extract_blocking_outputs
from src.docx_locator import enrich_reviewed_risks_with_locators
from src.analysis_scope import analysis_scope_label, normalize_analysis_scope
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



_STRONG_SENTENCE_BOUNDARY_CHARS = "。!！？?；;\n"
_TERMINAL_STRONG_BOUNDARY_RE = re.compile(r"([。!！?？；;])\s*$")
_TERMINAL_WEAK_PUNCT_RE = re.compile(r"[，,、：:]\s*$")
_AGGREGATE_GENERIC_OVERLAP_FRAGMENTS = {
    "甲方",
    "乙方",
    "双方",
    "合同",
    "项目",
    "条款",
    "规定",
    "约定",
    "样品",
    "检测",
    "验收",
    "进行",
    "过程",
    "结果",
    "完成",
    "支付",
    "解除",
    "通知",
    "期间",
    "提供",
    "技术资料",
    "检测过程",
    "检测结果",
    "测试过程",
    "送检样品",
    "受测样品",
    "验收检测",
}


def _stringify_error_detail(detail: Any) -> str:
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail.strip()
    if isinstance(detail, list):
        texts = [_stringify_error_detail(item) for item in detail]
        return "；".join(text for text in texts if text)
    if isinstance(detail, dict):
        for key in ("user_message", "message", "detail", "msg"):
            text = _stringify_error_detail(detail.get(key))
            if text:
                return text
        for value in detail.values():
            text = _stringify_error_detail(value)
            if text:
                return text
        return ""
    return str(detail).strip()


def _build_user_facing_error(status_code: int, detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        code = str(detail.get("code") or "").strip()
        title = str(detail.get("title") or "").strip()
        user_message = str(detail.get("user_message") or detail.get("message") or "").strip()
        if code or title or user_message:
            return {
                "code": code or "API_ERROR",
                "title": title or "操作未完成",
                "message": user_message or _stringify_error_detail(detail) or "操作未完成，请稍后重试。",
                "status": status_code,
            }

    detail_text = _stringify_error_detail(detail)
    detail_lower = detail_text.lower()

    if ("仅支持" in detail_text and ".docx" in detail_text) or ("unsupported" in detail_lower and "docx" in detail_lower):
        return {
            "code": "UNSUPPORTED_FILE_TYPE",
            "title": "文件格式不支持",
            "message": "请上传 .docx 格式的合同文件后再试。",
            "status": status_code,
        }
    if "run_id 不存在" in detail_text:
        return {
            "code": "REVIEW_NOT_FOUND",
            "title": "审查记录不存在",
            "message": "未找到对应的审查记录，请返回首页重新上传合同。",
            "status": status_code,
        }
    if "risk_id 不存在" in detail_text:
        return {
            "code": "RISK_NOT_FOUND",
            "title": "风险项不存在",
            "message": "未找到对应的风险项，请刷新页面后再试。",
            "status": status_code,
        }
    if "结果尚未生成完成" in detail_text or "任务尚未完成" in detail_text:
        return {
            "code": "REVIEW_NOT_READY",
            "title": "审查尚未完成",
            "message": "合同还在处理中，请稍后再试。",
            "status": status_code,
        }
    if status_code in (400, 422):
        return {
            "code": "REQUEST_VALIDATION_ERROR",
            "title": "提交内容有误",
            "message": "请检查输入内容后重试。",
            "status": status_code,
        }
    if status_code == 404:
        return {
            "code": "RESOURCE_NOT_FOUND",
            "title": "内容不存在",
            "message": "请求的内容不存在或已失效，请返回上一步重试。",
            "status": status_code,
        }
    if status_code == 409:
        return {
            "code": "STATE_CONFLICT",
            "title": "当前状态暂不可操作",
            "message": "当前状态暂不支持该操作，请稍后再试。",
            "status": status_code,
        }
    if status_code >= 500:
        return {
            "code": "INTERNAL_ERROR",
            "title": "服务暂时不可用",
            "message": "服务开小差了，请稍后重试。",
            "status": status_code,
        }
    return {
        "code": "API_ERROR",
        "title": "操作未完成",
        "message": detail_text or "操作未完成，请稍后重试。",
        "status": status_code,
    }


def _build_error_response_content(status_code: int, detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        payload = dict(detail)
        payload.setdefault("detail", detail.get("detail"))
    else:
        payload = {"detail": detail}
    payload["error"] = _build_user_facing_error(status_code, detail)
    return payload


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_build_error_response_content(exc.status_code, exc.detail),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    detail = {
        "code": "REQUEST_VALIDATION_ERROR",
        "title": "提交内容有误",
        "user_message": "请检查上传文件和必填项后重试。",
        "detail": exc.errors(),
    }
    return JSONResponse(status_code=422, content=_build_error_response_content(422, detail))


def _split_text_into_sentence_spans(text: str) -> list[tuple[int, int]]:
    source = str(text or "")
    if not source:
        return []

    spans: list[tuple[int, int]] = []
    start = 0
    for idx, ch in enumerate(source):
        if ch in _STRONG_SENTENCE_BOUNDARY_CHARS:
            end = idx + 1
            if source[start:end].strip():
                spans.append((start, end))
            start = end

    if start < len(source) and source[start:].strip():
        spans.append((start, len(source)))

    return spans or [(0, len(source))]


def _sequence_match_stats(source: str, revised: str) -> tuple[int, int, list[Any]]:
    matcher = SequenceMatcher(None, str(source or ""), str(revised or ""), autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
    matched_chars = sum(block.size for block in blocks)
    longest_block = max((block.size for block in blocks), default=0)
    return matched_chars, longest_block, blocks


def _aggregate_overlap_fragment(fragment: str) -> str:
    return re.sub(r"[\s，,；;。!！？?：:、（）()【】\[\]{}《》“”\"'‘’]", "", str(fragment or ""))


def _has_strong_sentence_overlap(sentence: str, revised: str) -> bool:
    _, _, blocks = _sequence_match_stats(sentence, revised)
    for block in blocks:
        if block.size < 4:
            continue
        fragment = _aggregate_overlap_fragment(sentence[block.a : block.a + block.size])
        if len(fragment) < 4:
            continue
        if fragment in _AGGREGATE_GENERIC_OVERLAP_FRAGMENTS:
            continue
        return True
    return False


def _select_aggregate_sentence_window(original: str, revised: str) -> tuple[int, int]:
    spans = _split_text_into_sentence_spans(original)
    if not spans:
        return 0, len(original)
    if len(spans) == 1:
        return spans[0]

    sentence_scores: list[tuple[tuple[int, int, int], int, int]] = []
    for idx, (start, end) in enumerate(spans):
        sentence_text = original[start:end].strip()
        matched_chars, longest_block, _ = _sequence_match_stats(sentence_text, revised)
        score = (longest_block, matched_chars, -len(sentence_text))
        sentence_scores.append((score, idx, matched_chars))

    best_score, anchor_idx, anchor_matched_chars = max(sentence_scores)
    if best_score[0] < 2 and anchor_matched_chars < 6:
        return 0, len(original)

    anchor_start, anchor_end = spans[anchor_idx]
    best_window = (anchor_start, anchor_end)
    best_window_score = best_score
    best_window_matched_chars = anchor_matched_chars

    for neighbor_idx in (anchor_idx - 1, anchor_idx + 1):
        if neighbor_idx < 0 or neighbor_idx >= len(spans):
            continue
        neighbor_start, neighbor_end = spans[neighbor_idx]
        neighbor_text = original[neighbor_start:neighbor_end].strip()
        if not _has_strong_sentence_overlap(neighbor_text, revised):
            continue

        window_start = min(anchor_start, neighbor_start)
        window_end = max(anchor_end, neighbor_end)
        window_text = original[window_start:window_end].strip()
        matched_chars, longest_block, _ = _sequence_match_stats(window_text, revised)
        improvement = matched_chars - best_window_matched_chars
        if improvement < max(4, min(10, len(revised) // 6)) and longest_block <= best_window_score[0]:
            continue

        window_score = (longest_block, matched_chars, -len(window_text))
        if window_score > best_window_score:
            best_window = (window_start, window_end)
            best_window_score = window_score
            best_window_matched_chars = matched_chars

    return best_window


def _heal_aggregate_revised_text_tail(target_text: str | None, revised_text: str | None) -> str:
    target = str(target_text or "").rstrip()
    revised = str(revised_text or "").rstrip()
    if not target or not revised:
        return revised

    target_boundary_match = _TERMINAL_STRONG_BOUNDARY_RE.search(target)
    if not target_boundary_match:
        return revised

    if _TERMINAL_STRONG_BOUNDARY_RE.search(revised):
        return revised

    boundary = target_boundary_match.group(1)
    revised = _TERMINAL_WEAK_PUNCT_RE.sub("", revised).rstrip()
    if not revised:
        return revised

    return revised + boundary


def _significant_match_blocks(source: str, revised: str) -> list[Any]:
    _, longest_block, blocks = _sequence_match_stats(source, revised)
    if not blocks:
        return []

    min_block_size = 2 if longest_block >= 4 else 3
    significant_blocks = [block for block in blocks if block.size >= min_block_size]
    return significant_blocks or blocks


def _effective_shrink_match_blocks(source: str, revised: str) -> list[Any]:
    use_blocks = _significant_match_blocks(source, revised)
    if not use_blocks:
        return []

    strong_blocks: list[Any] = []
    for block in use_blocks:
        fragment = _aggregate_overlap_fragment(source[block.a : block.a + block.size])
        if len(fragment) < 4:
            continue
        if fragment in _AGGREGATE_GENERIC_OVERLAP_FRAGMENTS:
            continue
        strong_blocks.append(block)

    return strong_blocks or use_blocks



def _first_significant_match_start(source: str, revised: str) -> int | None:
    use_blocks = _effective_shrink_match_blocks(source, revised)
    if not use_blocks:
        return None
    return use_blocks[0].a



def _last_significant_match_end(source: str, revised: str) -> int | None:
    use_blocks = _effective_shrink_match_blocks(source, revised)
    if not use_blocks:
        return None
    return use_blocks[-1].a + use_blocks[-1].size


def _shrink_aggregate_target_text(original_target_text: str | None, revised_text: str | None) -> str:
    original = str(original_target_text or "").strip()
    revised = str(revised_text or "").strip()
    if not original or not revised:
        return original
    if len(revised) >= len(original):
        return original

    window_start, window_end = _select_aggregate_sentence_window(original, revised)
    window_text = original[window_start:window_end].strip()
    if not window_text:
        return original

    relative_start = _first_significant_match_start(window_text, revised)
    relative_end = _last_significant_match_end(window_text, revised)
    if relative_start is None or relative_end is None or relative_end <= relative_start:
        return original

    start = window_start + relative_start
    end = window_start + relative_end
    candidate = original[start:end].strip()
    if not candidate:
        return original
    if len(candidate) >= len(original):
        return original

    matched_chars, _, _ = _sequence_match_stats(candidate, revised)
    min_match_chars = max(6, min(len(revised), len(candidate)) // 3)
    if matched_chars < min_match_chars:
        return original

    if len(candidate) > int(len(original) * 0.95):
        return original

    return candidate


def _count_fragment_occurrences(source_text: str | None, fragment: str | None) -> int:
    source = str(source_text or "")
    target = str(fragment or "")
    if not source or not target:
        return 0
    return len(list(re.finditer(re.escape(target), source)))



def _sentence_span_containing_index(text: str | None, index: int) -> tuple[int, int] | None:
    source = str(text or "")
    if not source or index < 0 or index >= len(source):
        return None
    for start, end in _split_text_into_sentence_spans(source):
        if start <= index < end:
            return start, end
    return None



def _is_unique_stable_sentence_span(source_text: str | None, candidate_text: str | None) -> bool:
    source = str(source_text or "")
    candidate = str(candidate_text or "").strip()
    if not source or not candidate or candidate not in source:
        return False
    if _count_fragment_occurrences(source, candidate) != 1:
        return False
    spans = _split_text_into_sentence_spans(source)
    return any(source[start:end].strip() == candidate for start, end in spans)



def _unique_sentence_aligned_fragment(source_text: str | None, fragment: str | None) -> str:
    source = str(source_text or "")
    raw = str(fragment or "").strip()
    if not source or not raw or raw == source or raw not in source:
        return ""
    if _count_fragment_occurrences(source, raw) != 1:
        return ""

    match = re.search(re.escape(raw), source)
    if not match:
        return ""
    start, end = match.span()

    start_aligned = False
    end_aligned = False
    for sent_start, sent_end in _split_text_into_sentence_spans(source):
        if sent_start == start:
            start_aligned = True
        if sent_end == end:
            end_aligned = True
        if start_aligned and end_aligned:
            return source[start:end].strip()

    return ""



def _expand_fragment_to_unique_sentence(source_text: str | None, fragment: str | None) -> str:
    source = str(source_text or "")
    raw = str(fragment or "").strip()
    if not source or not raw or raw == source or raw not in source:
        return ""

    preserved = _unique_sentence_aligned_fragment(source, raw)
    if preserved:
        return preserved

    if _count_fragment_occurrences(source, raw) != 1:
        return ""

    match = re.search(re.escape(raw), source)
    if not match:
        return ""
    span = _sentence_span_containing_index(source, match.start())
    if not span:
        return ""
    start, end = span
    candidate = source[start:end].strip()
    if not candidate or candidate == source:
        return ""
    if _count_fragment_occurrences(source, candidate) != 1:
        return ""
    return candidate



def _aggregate_primary_anchor_field_text(item: dict[str, Any], field: str) -> str:
    anchored_risk = item.get("anchored_risk")
    if isinstance(anchored_risk, dict):
        text = str(anchored_risk.get(field) or "").strip()
        if text:
            return text

    anchored_risks = item.get("anchored_risks")
    if isinstance(anchored_risks, list):
        for anchored_item in anchored_risks:
            if not isinstance(anchored_item, dict):
                continue
            text = str(anchored_item.get(field) or "").strip()
            if text:
                return text

    return str(item.get(field) or "").strip()



def _select_mixed_aggregate_primary_target(item: dict[str, Any], fallback_target: str | None = None) -> str:
    clause_text = str(item.get("clause_text") or item.get("target_text") or "").strip()
    fallback = str(fallback_target or "").strip()
    if not clause_text:
        return fallback

    fragment_candidates = [
        _aggregate_primary_anchor_field_text(item, "evidence_text"),
        _aggregate_primary_anchor_field_text(item, "target_text"),
        _aggregate_primary_anchor_field_text(item, "main_text"),
        _aggregate_primary_anchor_field_text(item, "anchor_text"),
        fallback,
    ]
    seen: set[str] = set()
    for raw in fragment_candidates:
        candidate = _sanitize_ai_target_text(raw)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        expanded = _expand_fragment_to_unique_sentence(clause_text, candidate)
        if expanded:
            return expanded

    if fallback and fallback in clause_text and _is_unique_stable_sentence_span(clause_text, fallback):
        return fallback
    return fallback or clause_text



def _is_short_revised_patch(target_text: str | None, revised_text: str | None) -> bool:
    target = str(target_text or "").strip()
    revised = str(revised_text or "").strip()
    if not target or not revised:
        return False
    before_changed, after_changed = _minimize_patch_pair(target, revised)
    before_core = _aggregate_overlap_fragment(before_changed)
    after_core = _aggregate_overlap_fragment(after_changed)
    changed_before_len = len(before_core) or len(before_changed.strip())
    changed_after_len = len(after_core) or len(after_changed.strip())
    return changed_after_len <= 20 or (changed_after_len <= 28 and changed_before_len <= 18)



def _apply_mixed_aggregate_target_floor(
    item: dict[str, Any],
    baseline: str | None,
    resolved: str | None,
    revised_text: str | None = None,
) -> str:
    aggregate_type = str(item.get("aggregate_type") or "").strip().lower()
    current = str(resolved or "").strip()
    if aggregate_type != "mixed_clause_risks" or not current:
        return current

    clause_text = str(item.get("clause_text") or item.get("target_text") or "").strip()
    floor_target = _select_mixed_aggregate_primary_target(item, baseline)
    if not clause_text or not floor_target or floor_target == current:
        return current
    if floor_target not in clause_text or current not in clause_text:
        return current
    if current not in floor_target:
        return current
    if not _is_unique_stable_sentence_span(clause_text, floor_target):
        return current
    if _is_unique_stable_sentence_span(clause_text, current):
        return current
    if not _is_short_revised_patch(floor_target, revised_text):
        return current
    return floor_target



def _pick_narrow_aggregate_target(
    item: dict[str, Any],
    ai_payload: dict[str, Any],
    revised_text: str | None = None,
) -> str:
    clause_text = str(item.get("clause_text") or item.get("target_text") or "").strip()
    current_target = str(ai_payload.get("target_text") or "").strip()
    evidence_text = str(item.get("evidence_text") or "").strip()
    anchor_text = str(item.get("anchor_text") or "").strip()
    aggregate_type = str(item.get("aggregate_type") or "").strip().lower()
    revised = str(revised_text or ai_payload.get("revised_text") or "").strip()

    if aggregate_type == "mixed_clause_risks":
        return _select_mixed_aggregate_primary_target(item, current_target or evidence_text or anchor_text or clause_text)

    if aggregate_type != "anchored_only":
        return current_target or evidence_text or anchor_text or clause_text

    # anchored_only 默认保留当前 AI target，避免把句级替换误缩成一个很短的尾部片段。
    best = current_target or evidence_text or anchor_text or clause_text

    # 仅当 current_target 明显过宽、且 evidence_text 可以形成稳定替换时，才缩到证据文本。
    if current_target and evidence_text and evidence_text in current_target:
        before_e, after_e = _minimize_patch_pair(evidence_text, revised)
        current_is_overwide = len(current_target) >= max(len(evidence_text) * 2, len(evidence_text) + 12)
        evidence_is_safe = bool(before_e.strip()) and bool(after_e.strip())
        if current_is_overwide and evidence_is_safe:
            return evidence_text

    return best



def _aggregate_group_fallback_text(item: dict[str, Any], field: str) -> str:
    text = str(item.get(field) or "").strip()
    if text:
        return text

    anchored_risk = item.get("anchored_risk")
    if isinstance(anchored_risk, dict):
        text = str(anchored_risk.get(field) or "").strip()
        if text:
            return text

    anchored_risks = item.get("anchored_risks")
    if isinstance(anchored_risks, list):
        for anchored_item in anchored_risks:
            if not isinstance(anchored_item, dict):
                continue
            text = str(anchored_item.get(field) or "").strip()
            if text:
                return text

    return ""


def _aggregate_suggestion_texts(item: dict[str, Any]) -> list[str]:
    values = [
        _aggregate_group_fallback_text(item, "suggestion"),
        _aggregate_group_fallback_text(item, "suggestion_minimal"),
        _aggregate_group_fallback_text(item, "suggestion_optimized"),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _looks_placeholder_replace_text(text: str | None) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    placeholder_markers = ("……", "...", "…", "XXX", "xxx", "待补充")
    return any(marker in value for marker in placeholder_markers)


def _parse_aggregate_suggestion_ops(item: dict[str, Any]) -> dict[str, Any]:
    quote_open = r"[“\"'‘「]"
    quote_close = r"[”\"'’」]"
    pair_patterns = [
        re.compile(
            rf"(?:将|把)\s*{quote_open}([^”\"'’」]{{1,80}}){quote_close}\s*(?:修改|改|替换)为\s*{quote_open}([^”\"'’」]{{1,120}}){quote_close}"
        ),
        re.compile(
            rf"{quote_open}([^”\"'’」]{{1,80}}){quote_close}\s*(?:修改|改|替换)为\s*{quote_open}([^”\"'’」]{{1,120}}){quote_close}"
        ),
    ]
    delete_pattern = re.compile(
        rf"(?:删除|去掉|移除)\s*{quote_open}([^”\"'’」]{{1,80}}){quote_close}(?:字样|表述|内容)?"
    )
    replace_pattern = re.compile(
        rf"(?:改为|修改为|替换为)\s*{quote_open}([^”\"'’」]{{1,120}}){quote_close}"
    )

    replace_pairs: list[tuple[str, str]] = []
    delete_phrases: list[str] = []
    replace_texts: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_delete: set[str] = set()
    seen_replace: set[str] = set()

    for suggestion in _aggregate_suggestion_texts(item):
        for pattern in pair_patterns:
            for match in pattern.finditer(suggestion):
                from_text = str(match.group(1) or "").strip()
                to_text = str(match.group(2) or "").strip()
                if not from_text or not to_text:
                    continue
                pair = (from_text, to_text)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                replace_pairs.append(pair)
        for match in delete_pattern.finditer(suggestion):
            phrase = str(match.group(1) or "").strip()
            if not phrase or phrase in seen_delete:
                continue
            seen_delete.add(phrase)
            delete_phrases.append(phrase)
        for match in replace_pattern.finditer(suggestion):
            phrase = str(match.group(1) or "").strip()
            if not phrase or phrase in seen_replace:
                continue
            seen_replace.add(phrase)
            replace_texts.append(phrase)

    return {
        "replace_pairs": replace_pairs,
        "delete_phrases": delete_phrases,
        "replace_texts": replace_texts,
    }


def _clean_deleted_phrase_artifacts(text: str | None) -> str:
    cleaned = str(text or "")
    if not cleaned:
        return ""

    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = re.sub(r"[、，,]{2,}", lambda m: m.group(0)[0], cleaned)
        cleaned = re.sub(r"([（(])\s*[、，,]\s*", r"\1", cleaned)
        cleaned = re.sub(r"\s*[、，,]\s*([）)])", r"\1", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"^[、，,]\s*", "", cleaned)
        cleaned = re.sub(r"\s*[、，,]$", "", cleaned)
    return cleaned.strip()


def _strip_leading_list_introducer(text: str | None) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    for prefix in ("包括但不限于", "包括", "包含", "例如", "比如", "如"):
        if not cleaned.startswith(prefix):
            continue
        candidate = cleaned[len(prefix) :].strip()
        if len(_aggregate_overlap_fragment(candidate)) < 4:
            continue
        if "、" in candidate or "，" in candidate or "," in candidate:
            return candidate
    return cleaned


def _build_suggestion_guided_patch_for_target(
    item: dict[str, Any],
    target_text: str | None,
    ops: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    resolved_target = str(target_text or "").strip()
    if not resolved_target:
        return None

    resolved_ops = ops or _parse_aggregate_suggestion_ops(item)
    replace_pairs = list(resolved_ops.get("replace_pairs") or [])
    delete_phrases = [str(v or "").strip() for v in (resolved_ops.get("delete_phrases") or []) if str(v or "").strip()]
    replace_texts = [str(v or "").strip() for v in (resolved_ops.get("replace_texts") or []) if str(v or "").strip()]
    if not replace_pairs and not delete_phrases and not replace_texts:
        return None

    revised_text = ""
    for from_text, to_text in replace_pairs:
        if _looks_placeholder_replace_text(to_text):
            continue
        if from_text in resolved_target:
            revised_text = resolved_target.replace(from_text, to_text, 1)
            break

    if not revised_text and delete_phrases and not replace_pairs and not replace_texts:
        revised_text = resolved_target
        changed = False
        for phrase in delete_phrases:
            if phrase and phrase in revised_text:
                revised_text = revised_text.replace(phrase, "", 1)
                changed = True
        if changed:
            revised_text = _clean_deleted_phrase_artifacts(revised_text)
        else:
            revised_text = ""

    if not revised_text and not replace_pairs and not delete_phrases:
        for phrase in replace_texts:
            if _looks_placeholder_replace_text(phrase):
                continue
            revised_text = phrase
            break

    revised_text = str(revised_text or "").strip()
    if not revised_text or revised_text == resolved_target:
        return None
    return resolved_target, revised_text, resolved_ops


def _extract_clause_refs_from_text(text: str | None) -> list[str]:
    raw = str(text or "")
    if not raw:
        return []
    refs: list[str] = []
    seen: set[str] = set()
    patterns = [
        re.compile(r"(?:条款\s*)?第\s*(\d+(?:\.\d+)*)\s*条"),
        re.compile(r"(\d+\.\d+(?:\.\d+)*)"),
    ]
    for pattern in patterns:
        for match in pattern.finditer(raw):
            ref = str(match.group(1) or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            refs.append(ref)
    return refs


def _ordered_clause_search_candidates(risk: dict[str, Any], clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(clause: dict[str, Any] | None) -> None:
        if not isinstance(clause, dict):
            return
        key = str(clause.get("clause_uid") or clause.get("display_clause_id") or clause.get("clause_id") or id(clause)).strip()
        if not key or key in seen:
            return
        seen.add(key)
        ordered.append(clause)

    _, by_ref = _build_clause_lookup(clauses)
    current_clause = _find_clause_for_risk(risk, clauses)
    add(current_clause)

    for field in ("issue", "suggestion", "risk_label", "factual_basis", "reasoning_basis"):
        for ref in _extract_clause_refs_from_text(risk.get(field)):
            add(_select_clause_candidate(by_ref.get(ref) or [], risk=risk))

    current_segment_id = str((current_clause or {}).get("segment_id") or "").strip()
    if current_segment_id:
        for clause in clauses:
            if str(clause.get("segment_id") or "").strip() == current_segment_id:
                add(clause)

    current_clause_id = str((current_clause or {}).get("display_clause_id") or (current_clause or {}).get("clause_id") or "").strip()
    article_prefix = current_clause_id.split(".", 1)[0] if "." in current_clause_id else ""
    if article_prefix:
        for clause in clauses:
            display_clause_id = str(clause.get("display_clause_id") or clause.get("clause_id") or "").strip()
            if display_clause_id.startswith(article_prefix + "."):
                add(clause)

    for clause in clauses:
        add(clause)
    return ordered


def _resolve_suggestion_guided_patch_context(
    risk: dict[str, Any],
    clauses: list[dict[str, Any]],
) -> dict[str, Any] | None:
    ops = _parse_aggregate_suggestion_ops(risk)
    replace_pairs = [(str(a or "").strip(), str(b or "").strip()) for a, b in (ops.get("replace_pairs") or [])]
    delete_phrases = [str(v or "").strip() for v in (ops.get("delete_phrases") or []) if str(v or "").strip()]
    if not replace_pairs and not delete_phrases:
        return None

    primary_fragments: list[str] = []
    for from_text, _ in replace_pairs:
        if from_text:
            primary_fragments.append(from_text)
    primary_fragments.extend(delete_phrases)

    base_target = _extract_target_text(risk)
    base_patch = _build_suggestion_guided_patch_for_target(risk, base_target, ops)
    if base_patch is not None:
        target_text, revised_text, _ = base_patch
        return {
            "target_text": target_text,
            "revised_text": revised_text,
            "clause_text": str(target_text or ""),
            "ops": ops,
        }

    for clause in _ordered_clause_search_candidates(risk, clauses):
        clause_text = str(clause.get("source_excerpt") or clause.get("clause_text") or "").strip()
        if not clause_text:
            continue
        for fragment in primary_fragments:
            if not fragment or fragment not in clause_text:
                continue
            target_text = _expand_fragment_to_unique_sentence(clause_text, fragment) or fragment
            patch = _build_suggestion_guided_patch_for_target(risk, target_text, ops)
            if patch is None and target_text != clause_text:
                patch = _build_suggestion_guided_patch_for_target(risk, clause_text, ops)
            if patch is None:
                continue
            resolved_target, revised_text, _ = patch
            return {
                "target_text": resolved_target,
                "revised_text": revised_text,
                "clause_text": clause_text,
                "matched_clause_uid": str(clause.get("clause_uid") or "").strip(),
                "matched_clause_id": str(clause.get("display_clause_id") or clause.get("clause_id") or "").strip(),
                "matched_fragment": fragment,
                "ops": ops,
            }
    return None


def _build_suggestion_guided_aggregate_patch(item: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    aggregate_type = str(item.get("aggregate_type") or "").strip().lower()
    if aggregate_type != "anchored_only":
        return None

    evidence_text = _aggregate_group_fallback_text(item, "evidence_text")
    if not evidence_text:
        return None
    if len(_aggregate_overlap_fragment(evidence_text)) < 6:
        return None

    return _build_suggestion_guided_patch_for_target(item, evidence_text)


def _should_prefer_suggestion_guided_patch(
    current_target: str | None,
    current_revised: str | None,
    guided_target: str,
    guided_revised: str,
    ops: dict[str, Any],
) -> bool:
    target_text = str(current_target or "").strip()
    revised_text = str(current_revised or "").strip()
    if not guided_target or not guided_revised:
        return False
    if target_text == guided_target and revised_text == guided_revised:
        return False
    if not revised_text:
        return True

    delete_phrase_values = [str(phrase or "").strip() for phrase in (ops.get("delete_phrases") or []) if str(phrase or "").strip()]
    replace_pairs = [(str(a or "").strip(), str(b or "").strip()) for a, b in (ops.get("replace_pairs") or [])]
    replace_texts = [
        str(value or "").strip()
        for value in (ops.get("replace_texts") or [])
        if str(value or "").strip() and not _looks_placeholder_replace_text(value)
    ]

    for phrase_text in delete_phrase_values:
        if phrase_text and phrase_text in revised_text:
            return True

    delete_only = bool(delete_phrase_values) and not replace_pairs and not replace_texts
    if delete_only:
        current_without_boundary = _TERMINAL_STRONG_BOUNDARY_RE.sub("", revised_text).rstrip()
        guided_without_boundary = _TERMINAL_STRONG_BOUNDARY_RE.sub("", guided_revised).rstrip()
        if (
            current_without_boundary
            and guided_without_boundary != current_without_boundary
            and guided_without_boundary.endswith(current_without_boundary)
        ):
            dropped_prefix = guided_without_boundary[: len(guided_without_boundary) - len(current_without_boundary)]
            dropped_core = _aggregate_overlap_fragment(dropped_prefix)
            if (
                dropped_core
                and len(dropped_core) <= 6
                and not any(phrase and phrase in dropped_prefix for phrase in delete_phrase_values)
            ):
                return True

    for from_text, to_text in replace_pairs:
        if from_text and from_text in revised_text and to_text and to_text not in revised_text:
            return True

    if replace_texts and all(text not in revised_text for text in replace_texts):
        overlap = SequenceMatcher(None, target_text, revised_text, autojunk=False).ratio() if target_text else 0.0
        if overlap >= 0.75 or revised_text == target_text:
            return True

    return False


def _strip_deleted_phrase_from_revised_change_head(item: dict[str, Any], target_text: str | None, revised_text: str | None) -> str:
    target = str(target_text or "")
    revised = str(revised_text or "")
    if not target or not revised:
        return revised

    ops = _parse_aggregate_suggestion_ops(item)
    delete_phrases = [str(value or "").strip() for value in (ops.get("delete_phrases") or []) if str(value or "").strip()]
    if not delete_phrases:
        return revised

    prefix = 0
    max_prefix = min(len(target), len(revised))
    while prefix < max_prefix and target[prefix] == revised[prefix]:
        prefix += 1

    changed = False
    revised_suffix = revised[prefix:]
    for phrase in delete_phrases:
        if phrase and revised_suffix.startswith(phrase):
            revised_suffix = revised_suffix[len(phrase):].lstrip()
            changed = True
    if not changed:
        return revised

    normalized = revised[:prefix] + revised_suffix
    return _clean_deleted_phrase_artifacts(normalized)


def _apply_anchored_only_target_floor(
    item: dict[str, Any],
    baseline: str,
    resolved: str,
) -> str:
    aggregate_type = str(item.get("aggregate_type") or "").strip().lower()
    evidence_text = _aggregate_group_fallback_text(item, "evidence_text")

    if aggregate_type != "anchored_only":
        return resolved
    if not evidence_text:
        return resolved

    # Once baseline has narrowed to evidence_text, do not allow later shrinkage
    # to cut the span below that evidence floor.
    if baseline == evidence_text:
        return evidence_text

    # If shrinking dropped a leading prefix from the anchored evidence span,
    # keep the evidence span instead of a smaller token-like fragment.
    if (
        resolved
        and len(resolved) < len(evidence_text)
        and resolved in evidence_text
        and not evidence_text.startswith(resolved)
    ):
        return evidence_text

    return resolved


def _aggregate_target_match_quality(target_text: str | None, revised_text: str | None) -> tuple[int, int, int, int]:
    target = str(target_text or "").strip()
    revised = str(revised_text or "").strip()
    if not target or not revised:
        return (0, 0, 0, 0)

    matched_chars, longest_block, _ = _sequence_match_stats(target, revised)
    core_len = max(1, len(_aggregate_overlap_fragment(target)))
    density = int((matched_chars * 1000) / core_len)
    return (density, matched_chars, longest_block, -len(target))



def _can_stably_patch_aggregate_target(target_text: str | None, revised_text: str | None) -> bool:
    target = str(target_text or "").strip()
    revised = str(revised_text or "").strip()
    if not target or not revised:
        return False

    before_changed, after_changed = _minimize_patch_pair(target, revised)
    before_core = _aggregate_overlap_fragment(before_changed)
    after_core = _aggregate_overlap_fragment(after_changed)
    if len(before_core) < 4 or len(after_core) < 4:
        return False

    matched_chars, longest_block, _ = _sequence_match_stats(target, revised)
    min_match_chars = max(6, min(len(before_core), len(after_core)) // 2)
    return longest_block >= 4 or matched_chars >= min_match_chars



def _repair_mixed_aggregate_primary_evidence_drift(
    item: dict[str, Any],
    source_target: str,
    resolved: str,
    revised_text: str | None = None,
) -> str:
    aggregate_type = str(item.get("aggregate_type") or "").strip().lower()
    if aggregate_type != "mixed_clause_risks":
        return str(resolved or "").strip()

    source = str(source_target or "").strip()
    current = str(resolved or "").strip()
    revised = str(revised_text or "").strip()
    evidence_floor = _select_mixed_aggregate_primary_target(item, _aggregate_group_fallback_text(item, "evidence_text") or str(item.get("evidence_text") or "").strip())

    if not source or not current or not evidence_floor:
        return current
    if evidence_floor == source or evidence_floor not in source:
        return current

    # Only repair the specific drift case from a broad host clause to an
    # adjacent sibling fragment. Keep already-correct evidence-aligned or
    # broader evidence-covering targets unchanged.
    if current == evidence_floor:
        return current
    if current in evidence_floor or evidence_floor in current:
        return current
    if current not in source:
        return current
    if len(source) < max(len(evidence_floor) + 12, int(len(evidence_floor) * 1.2)):
        return current
    if not _can_stably_patch_aggregate_target(evidence_floor, revised):
        return current

    current_score = _aggregate_target_match_quality(current, revised)
    evidence_score = _aggregate_target_match_quality(evidence_floor, revised)
    if evidence_score <= current_score:
        return current

    return evidence_floor


def _common_prefix_len(left: str, right: str) -> int:
    prefix = 0
    max_prefix = min(len(left), len(right))
    while prefix < max_prefix and left[prefix] == right[prefix]:
        prefix += 1
    return prefix


def _looks_like_tail_continuation(prefix_text: str, suffix_text: str) -> bool:
    prefix = str(prefix_text or "").rstrip()
    suffix = str(suffix_text or "").lstrip()
    if not prefix or not suffix:
        return False

    if not prefix.endswith(("：", ":", "，", ",", "、", "；", ";")):
        return False

    continuation_starters = (
        "同时",
        "并",
        "并应",
        "并由",
        "且",
        "且应",
        "以及",
        "及",
        "还",
        "还应",
        "应",
        "由",
        "按",
        "其中",
        "但",
    )
    return any(suffix.startswith(starter) for starter in continuation_starters)


def _repair_aggregate_missing_prefix_target(
    source_text: str | None,
    current_target: str | None,
    revised_text: str | None,
) -> str:
    source = str(source_text or "").strip()
    current = str(current_target or "").strip()
    revised = str(revised_text or "").strip()
    if not source or not current or not revised:
        return current

    # Only repair the specific case where aggregate target is still the full
    # host clause, but the model returned a tail fragment that starts from an
    # inner continuation point (e.g. "同时…/并…") and omitted the unchanged
    # leading prefix. This keeps already well-scoped targets unchanged.
    if current != source:
        return current
    if _common_prefix_len(current, revised) >= 2:
        return current

    blocks = _significant_match_blocks(current, revised)
    if not blocks:
        return current

    min_block_size = max(6, min(18, len(current) // 4))
    for block in blocks:
        if block.b > 1 or block.a < 4 or block.size < min_block_size:
            continue

        candidate = current[block.a:].strip()
        if not candidate:
            continue

        dropped_prefix = current[: block.a]
        if not _looks_like_tail_continuation(dropped_prefix, candidate):
            continue

        matched_chars, longest_block, _ = _sequence_match_stats(candidate, revised)
        min_match_chars = max(8, min(len(candidate), len(revised)) // 3)
        if longest_block < min_block_size or matched_chars < min_match_chars:
            continue

        return candidate

    return current


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

    baseline = _pick_narrow_aggregate_target(item, ai_payload, next_revised)
    if not baseline:
        return current_target or source_target

    shrunk = _shrink_aggregate_target_text(baseline, next_revised)
    resolved = str(shrunk or baseline).strip()
    resolved = _repair_aggregate_missing_prefix_target(source_target, resolved, next_revised)
    resolved = _apply_anchored_only_target_floor(item, baseline, resolved)
    resolved = _repair_mixed_aggregate_primary_evidence_drift(item, source_target, resolved, next_revised)
    resolved = _apply_mixed_aggregate_target_floor(item, baseline, resolved, next_revised)
    return resolved


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


def _strip_unsafe_aggregate_revised_tail(source_text: str | None, target_text: str | None, revised_text: str | None) -> str:
    source = str(source_text or "")
    target = str(target_text or "").rstrip()
    revised = str(revised_text or "").rstrip()
    if not source or not target or not revised:
        return revised

    if _TERMINAL_STRONG_BOUNDARY_RE.search(target):
        return revised

    if not _TERMINAL_STRONG_BOUNDARY_RE.search(revised):
        return revised

    target_index = source.find(target)
    if target_index < 0:
        return revised

    suffix = source[target_index + len(target) :]
    if not suffix.strip():
        return revised

    trimmed = _TERMINAL_STRONG_BOUNDARY_RE.sub("", revised).rstrip()
    return trimmed or revised



def _extend_aggregate_target_with_source_suffix_overlap(
    source_text: str | None,
    current_target: str | None,
    revised_text: str | None,
) -> str:
    source = str(source_text or "").strip()
    current = str(current_target or "").strip()
    revised = str(revised_text or "").strip()
    if not source or not current or not revised:
        return current
    if current == source or current not in source:
        return current
    if _count_fragment_occurrences(source, current) != 1:
        return current

    target_start = source.find(current)
    if target_start < 0:
        return current

    source_tail = source[target_start:]
    overlap_len = _common_prefix_len(source_tail, revised)
    if overlap_len <= len(current):
        return current

    extra_overlap = overlap_len - len(current)
    min_extra_overlap = max(6, min(18, max(len(current) // 6, 1)))
    if extra_overlap < min_extra_overlap:
        return current

    matched_last_index = min(target_start + overlap_len - 1, len(source) - 1)
    span = _sentence_span_containing_index(source, matched_last_index)
    if not span:
        return current

    _, sentence_end = span
    candidate = source[target_start:sentence_end].strip()
    if not candidate or len(candidate) <= len(current):
        return current
    if _count_fragment_occurrences(source, candidate) != 1:
        return current

    candidate_overlap = _common_prefix_len(candidate, revised)
    if candidate_overlap < len(current) + min_extra_overlap:
        return current

    return candidate



def _finalize_aggregate_patch_pair(item: dict[str, Any], ai_payload: dict[str, Any], revised_text: str | None = None) -> tuple[str, str]:
    resolved_target = _resolve_aggregate_patch_target(item, ai_payload, revised_text)
    next_revised = str(revised_text or ai_payload.get("revised_text") or "").strip()
    source_target = str(item.get("clause_text") or item.get("target_text") or ai_payload.get("target_text") or "").strip()

    guided_patch = _build_suggestion_guided_aggregate_patch(item)
    if guided_patch is not None:
        guided_target, guided_revised, guided_ops = guided_patch
        if _should_prefer_suggestion_guided_patch(resolved_target, next_revised, guided_target, guided_revised, guided_ops):
            resolved_target = guided_target
            next_revised = guided_revised

    next_revised = _strip_deleted_phrase_from_revised_change_head(item, resolved_target, next_revised)
    # If the model has copied the immediate source suffix that follows the
    # current target, expand the target forward to consume that unchanged span
    # as part of the replacement. Otherwise accepting the rewrite would append
    # duplicated text that already exists right after the target in the source.
    resolved_target = _extend_aggregate_target_with_source_suffix_overlap(source_target, resolved_target, next_revised)
    # Keep the resolved span as target and never re-minimize to a tiny token
    # (e.g. "参照"), otherwise replace may miss and append at tail.
    # When the resolved target ends with a strong sentence boundary but the
    # model rewrite does not, inherit that terminal punctuation to avoid the
    # replacement text sticking to the following original sentence.
    next_revised = _heal_aggregate_revised_text_tail(resolved_target, next_revised)
    # When the resolved target does not consume the original tail, the
    # replacement must not synthesize a new sentence boundary, otherwise the
    # remaining original suffix will be duplicated or split into a new sentence.
    next_revised = _strip_unsafe_aggregate_revised_tail(source_target, resolved_target, next_revised)
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
        raw_updated_at = str(payload.get("updated_at") or "").strip()
        payload_updated_at = _parse_iso_datetime(raw_updated_at) if raw_updated_at else 0.0
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
            is_stale_running = (not payload_updated_at) or (time.time() - payload_updated_at >= 300)
            if isinstance(inferred, dict) and str(inferred.get("status") or "") in {"completed", "failed"} and is_stale_running:
                payload["status"] = inferred.get("status")
                payload["step"] = inferred.get("step")
                payload["progress"] = inferred.get("progress")
                payload["error"] = inferred.get("error")
                payload["updated_at"] = inferred.get("updated_at")
        payload.setdefault("updated_at", datetime.utcnow().isoformat() + "Z")
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
    by_uid, by_ref = _build_clause_lookup(clauses)

    for field in ("clause_uids", "related_clause_uids", "clause_uid"):
        for uid in _as_clause_ref_list(risk.get(field)):
            clause = by_uid.get(uid)
            if clause is not None:
                return clause
            chosen = _select_clause_candidate(by_ref.get(uid) or [], risk=risk)
            if chosen is not None:
                return chosen

    for field in ("clause_ids", "related_clause_ids", "display_clause_ids", "clause_id", "display_clause_id"):
        for ref in _as_clause_ref_list(risk.get(field)):
            chosen = _select_clause_candidate(by_ref.get(ref) or [], risk=risk)
            if chosen is not None:
                return chosen
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


def _parse_rewrite_payload(payload: dict[str, Any] | None) -> tuple[str, str, str] | None:
    if not isinstance(payload, dict):
        return None
    if "revised_text" not in payload:
        return None
    revised_raw = payload.get("revised_text")
    if revised_raw is None:
        return None
    revised_text = str(revised_raw).strip()
    rationale = str(payload.get("rationale") or "").strip()
    edit_type = str(payload.get("edit_type") or "").strip()
    return revised_text, rationale, edit_type


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

    structured_payload = _parse_rewrite_payload(structured_dict)
    if structured_payload is not None:
        return structured_payload

    outputs_payload = _parse_rewrite_payload(outputs)
    if outputs_payload is not None:
        return outputs_payload

    text_payload = outputs.get("text")
    if not isinstance(text_payload, str):
        raise HTTPException(status_code=500, detail="rewrite workflow outputs 缺少 revised_text（structured_output/revised_text/text 均未提供）")
    cleaned = strip_markdown_json(text_payload)
    parsed = _load_json_with_repair(cleaned)
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=500, detail="rewrite workflow text 不是 JSON 对象")
    parsed_payload = _parse_rewrite_payload(parsed)
    if parsed_payload is None:
        raise HTTPException(status_code=500, detail="rewrite workflow 返回 revised_text 为空")
    return parsed_payload


def _build_ai_comment_text(
    *,
    target_text: str,
    revised_text: str,
) -> str:
    before = str(target_text or "").strip()
    after = str(revised_text or "").strip()
    if before and not after:
        before_piece = _short_text(before, 120) or "原文片段"
        suffix = "" if before_piece[-1:] in "。！？!?" else "。"
        return f"删除“{before_piece}”{suffix}"

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
    before_changed_core = _aggregate_overlap_fragment(before_changed)
    after_changed_core = _aggregate_overlap_fragment(after_changed)
    use_full_text = len(before_changed_core) < 4 or len(after_changed_core) < 4

    before_piece = _short_text(before if use_full_text else (before_changed or before), 120) or "原文片段"
    after_piece = _short_text(after if use_full_text else (after_changed or after), 120) or "修改后片段"
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


def _build_clause_lookup(clauses: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_uid: dict[str, dict[str, Any]] = {}
    by_ref: dict[str, list[dict[str, Any]]] = {}

    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        uid = str(clause.get("clause_uid") or "").strip()
        if uid:
            by_uid[uid] = clause
            uid_bucket = by_ref.setdefault(uid, [])
            if clause not in uid_bucket:
                uid_bucket.append(clause)
        for field in ("clause_id", "display_clause_id", "local_clause_id", "source_clause_id"):
            for ref in _as_clause_ref_list(clause.get(field)):
                bucket = by_ref.setdefault(ref, [])
                if clause not in bucket:
                    bucket.append(clause)

    return by_uid, by_ref



def _risk_clause_match_texts(risk: dict[str, Any] | None) -> list[str]:
    if not isinstance(risk, dict):
        return []
    texts: list[str] = []
    seen: set[str] = set()
    for field in ("target_text", "anchor_text", "evidence_text", "clause_text", "issue"):
        value = re.sub(r"\s+", " ", str(risk.get(field) or "")).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        texts.append(value)
    return texts



def _select_clause_candidate(candidates: list[dict[str, Any]], risk: dict[str, Any] | None = None) -> dict[str, Any] | None:
    unique: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for clause in candidates or []:
        if not isinstance(clause, dict):
            continue
        uid = str(clause.get("clause_uid") or "").strip()
        key = uid or str(id(clause))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(clause)

    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]

    texts = _risk_clause_match_texts(risk)
    if texts:
        narrowed: list[dict[str, Any]] = []
        narrowed_seen: set[str] = set()
        for clause in unique:
            clause_text = re.sub(r"\s+", " ", str(clause.get("source_excerpt") or clause.get("clause_text") or "")).strip()
            if not clause_text:
                continue
            if not any(text and (text in clause_text or clause_text in text) for text in texts):
                continue
            uid = str(clause.get("clause_uid") or "").strip()
            key = uid or str(id(clause))
            if key in narrowed_seen:
                continue
            narrowed_seen.add(key)
            narrowed.append(clause)
        if len(narrowed) == 1:
            return narrowed[0]

    return None



def _build_clause_uid_alias_map(clauses: list[dict[str, Any]]) -> dict[str, str]:
    by_uid, by_ref = _build_clause_lookup(clauses)
    alias: dict[str, str] = {uid: uid for uid in by_uid}
    for ref, candidates in by_ref.items():
        candidate_uids = {
            str(clause.get("clause_uid") or "").strip()
            for clause in candidates
            if str(clause.get("clause_uid") or "").strip()
        }
        if len(candidate_uids) == 1:
            alias.setdefault(ref, next(iter(candidate_uids)))
    return alias



def _collect_risk_clause_keys(risk: dict[str, Any], clause_alias_map: dict[str, str] | None = None) -> set[str]:
    alias_map = clause_alias_map or {}
    keys: set[str] = set()

    for field in ("clause_uids", "related_clause_uids", "clause_uid"):
        for uid in _as_clause_ref_list(risk.get(field)):
            keys.add(alias_map.get(uid) or uid)

    has_canonical_uid = bool(keys)
    for field in ("clause_ids", "related_clause_ids", "display_clause_ids", "clause_id", "display_clause_id"):
        for ref in _as_clause_ref_list(risk.get(field)):
            resolved = alias_map.get(ref)
            if resolved:
                keys.add(resolved)
            elif not has_canonical_uid:
                keys.add(ref)

    return keys


def _resolve_aggregate_context(run_dir: Path | None, item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return item
    if run_dir is None:
        return item
    if not str(item.get("aggregate_id") or "").strip():
        return item

    group = _load_ai_aggregation_group(run_dir, item)
    if not isinstance(group, dict):
        return item

    context = _clone_jsonable(group)
    for field in ("status", "ai_rewrite", "ai_rewrite_decision", "accepted_patch", "locator"):
        if field in item:
            context[field] = _clone_jsonable(item[field])
    return context



def _extract_normative_citation(item: dict[str, Any]) -> str:
    normative_basis = item.get("normative_basis")
    if not isinstance(normative_basis, dict):
        return ""
    return str(normative_basis.get("citation_text") or "").strip()



def _strip_redundant_basis_citation(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False

    basis = str(item.get("basis") or "").strip()
    citation = _extract_normative_citation(item)
    if not basis or not citation:
        return False

    if not basis.endswith(citation):
        return False

    prefix = re.sub(r"[；;，,、\s]+$", "", basis[: -len(citation)]).strip()
    if not prefix:
        return False

    changed = False
    if prefix != basis:
        item["basis"] = prefix
        changed = True

    if str(item.get("basis_citation") or "").strip() != citation:
        item["basis_citation"] = citation
        changed = True

    return changed



def _sanitize_reviewed_display_payload(payload: dict[str, Any], clauses: list[dict[str, Any]] | None = None) -> bool:
    risk_items = (((payload or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        return False

    alias_map = build_clause_alias_map(clauses or [])
    if not alias_map:
        return False

    changed = False
    for item in risk_items:
        if not isinstance(item, dict):
            continue
        if _strip_redundant_basis_citation(item):
            changed = True
        for field in ("issue", "basis_summary", "basis", "factual_basis", "reasoning_basis", "suggestion_basis"):
            raw = str(item.get(field) or "").strip()
            if not raw:
                continue
            cleaned = humanize_clause_refs(raw, alias_map)
            if cleaned != raw:
                item[field] = cleaned
                changed = True

        for nested_key in ("ai_rewrite", "ai_apply", "accepted_patch"):
            nested = item.get(nested_key)
            if not isinstance(nested, dict):
                continue
            raw_comment = str(nested.get("comment_text") or "").strip()
            if not raw_comment:
                continue
            cleaned_comment = humanize_clause_refs(raw_comment, alias_map)
            if cleaned_comment != raw_comment:
                nested["comment_text"] = cleaned_comment
                changed = True

        if _refresh_accepted_patch_for_item(item):
            changed = True
    return changed


def _sanitize_reviewed_ai_payload(payload: dict[str, Any], run_dir: Path | None = None) -> bool:
    risk_items = (((payload or {}).get("risk_result") or {}).get("risk_items") or [])
    if not isinstance(risk_items, list):
        return False

    changed = False
    for item in risk_items:
        if not isinstance(item, dict):
            continue
        aggregate_context = _resolve_aggregate_context(run_dir, item)
        preserve_full_clause = _use_full_clause_target(aggregate_context)
        fallback_target = _normalize_target_text(
            str(
                aggregate_context.get("target_text")
                or item.get("target_text")
                or item.get("evidence_text")
                or item.get("anchor_text")
                or ""
            ),
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
                target_for_comment, next_revised = _finalize_aggregate_patch_pair(aggregate_context, ai_payload, revised_text)
                if target_for_comment and str(ai_payload.get("target_text") or "").strip() != target_for_comment:
                    ai_payload["target_text"] = target_for_comment
                    changed = True
                if str(ai_payload.get("revised_text") or "").strip() != next_revised:
                    ai_payload["revised_text"] = next_revised
                    changed = True
                revised_text = next_revised
            else:
                target_for_comment = str(ai_payload.get("target_text") or "").strip() or fallback_target
            next_comment = _build_ai_comment_text(target_text=target_for_comment, revised_text=revised_text)
            if str(ai_payload.get("comment_text") or "").strip() != next_comment:
                ai_payload["comment_text"] = next_comment
                changed = True
        if _refresh_accepted_patch_for_item(item):
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


def _pick_suggestion_insert_text(risk: dict[str, Any]) -> str:
    return _first_non_empty_text(
        [
            risk.get("suggestion"),
            risk.get("suggestion_optimized"),
            risk.get("suggestion_minimal"),
            risk.get("basis"),
        ]
    )



def _build_suggest_insert_comment_text(risk: dict[str, Any]) -> str:
    issue = str(risk.get("issue") or risk.get("risk_label") or risk.get("title") or "").strip() or "—"
    basis = str(risk.get("basis_summary") or risk.get("basis") or "").strip() or "—"
    suggestion = _pick_suggestion_insert_text(risk) or "—"
    return "\n".join(
        [
            f"【问题】{issue}",
            f"【依据】{basis}",
            f"【建议插入】：{suggestion}",
        ]
    )



def _build_suggest_insert_patch(risk: dict[str, Any]) -> dict[str, Any] | None:
    suggestion_text = _pick_suggestion_insert_text(risk)
    if not suggestion_text:
        return None

    patch: dict[str, Any] = {
        "kind": "suggest_insert",
        "export_mode": "comment_only",
        "suggestion_text": suggestion_text,
        "comment_text": _build_suggest_insert_comment_text(risk),
        "created_at": _iso_now(),
    }

    locator = risk.get("locator") if isinstance(risk.get("locator"), dict) else {}
    locator_matched_text = str(locator.get("matched_text") or "").strip()
    locator_resolved_target_text = str(risk.get("locator_resolved_target_text") or "").strip()
    before_text = locator_resolved_target_text or locator_matched_text or _extract_target_text(risk)
    if before_text:
        patch["before_text"] = before_text
    return patch


def _build_ai_rewrite_patch(risk: dict[str, Any]) -> dict[str, Any] | None:
    ai_rewrite = risk.get("ai_rewrite") if isinstance(risk.get("ai_rewrite"), dict) else {}
    if str(ai_rewrite.get("state") or "").strip().lower() != "succeeded":
        return None

    before_text = str(ai_rewrite.get("target_text") or "").strip()
    revised_text = str(ai_rewrite.get("revised_text") or "")
    if not before_text and not revised_text:
        return None

    comment_text = str(ai_rewrite.get("comment_text") or "").strip()
    if not comment_text:
        comment_text = _build_ai_comment_text(target_text=before_text, revised_text=revised_text)

    created_at = str(ai_rewrite.get("created_at") or "").strip() or _iso_now()
    return {
        "kind": "ai_rewrite",
        "export_mode": "document_patch",
        "before_text": before_text,
        "after_text": revised_text,
        "comment_text": comment_text,
        "created_at": created_at,
    }


def _refresh_accepted_patch_for_item(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False

    changed = False
    status = str(item.get("status") or "").strip().lower()
    is_accepted = _is_accepted_risk_status(status)
    ai_rewrite = item.get("ai_rewrite") if isinstance(item.get("ai_rewrite"), dict) else {}
    ai_state = str(ai_rewrite.get("state") or "").strip().lower()
    current_patch = item.get("accepted_patch") if isinstance(item.get("accepted_patch"), dict) else None

    if is_accepted and ai_state == "succeeded":
        if str(item.get("ai_rewrite_decision") or "").strip().lower() != "accepted":
            item["ai_rewrite_decision"] = "accepted"
            changed = True
        next_patch = _build_ai_rewrite_patch(item)
        if next_patch is not None and next_patch != current_patch:
            item["accepted_patch"] = next_patch
            changed = True
        return changed

    if is_accepted:
        next_patch = _build_suggest_insert_patch(item)
        if next_patch is not None:
            if next_patch != current_patch:
                item["accepted_patch"] = next_patch
                changed = True
        elif current_patch is not None:
            item.pop("accepted_patch", None)
            changed = True
        return changed

    if current_patch is not None:
        item.pop("accepted_patch", None)
        changed = True
    return changed


def _has_other_accepted_risk_in_same_clause(
    target_item: dict[str, Any],
    risk_items: list[dict[str, Any]],
    clauses: list[dict[str, Any]] | None = None,
) -> bool:
    if not isinstance(target_item, dict) or not isinstance(risk_items, list):
        return False
    alias_map = _build_clause_uid_alias_map(clauses or []) if clauses else {}
    target_keys = _collect_risk_clause_keys(target_item, alias_map)
    if not target_keys:
        return False
    for item in risk_items:
        if not isinstance(item, dict) or item is target_item:
            continue
        if not _is_accepted_risk_status(item.get("status")):
            continue
        if _collect_risk_clause_keys(item, alias_map) & target_keys:
            return True
    return False


def _find_clause_by_key(clause_key: str, clauses: list[dict[str, Any]], alias_map: dict[str, str] | None = None) -> dict[str, Any] | None:
    raw_key = str(clause_key or "").strip()
    if not raw_key:
        return None
    alias = alias_map or _build_clause_uid_alias_map(clauses)
    normalized_key = alias.get(raw_key) or raw_key
    by_uid, by_ref = _build_clause_lookup(clauses)
    clause = by_uid.get(normalized_key)
    if clause is not None:
        return clause
    return _select_clause_candidate(by_ref.get(normalized_key) or [])


def _select_group_representative_risk(anchored_risks: list[dict[str, Any]], multi_risks: list[dict[str, Any]]) -> dict[str, Any]:
    if anchored_risks:
        return anchored_risks[0]
    if multi_risks:
        return multi_risks[0]
    return {}


def _aggregate_group_source_types(anchored_risks: list[dict[str, Any]], multi_risks: list[dict[str, Any]]) -> list[str]:
    source_types: list[str] = []
    if anchored_risks:
        source_types.append("anchored")
    if multi_risks:
        source_types.append("multi_clause")
    return source_types


def _aggregate_group_type(anchored_risks: list[dict[str, Any]], multi_risks: list[dict[str, Any]]) -> str:
    if anchored_risks and multi_risks:
        return "mixed_clause_risks"
    if anchored_risks:
        return "anchored_only"
    if multi_risks:
        return "multi_clause_only"
    return "unknown"


def _is_effective_aggregate_group(group: dict[str, Any] | None) -> bool:
    if not isinstance(group, dict):
        return False
    source_ids = {
        str(item or "").strip()
        for item in (group.get("source_risk_ids") or [])
        if str(item or "").strip()
    }
    if len(source_ids) >= 2:
        return True
    anchored_count = len(group.get("anchored_risks") or []) if isinstance(group.get("anchored_risks"), list) else 0
    multi_count = len(group.get("multi_clause_risks") or []) if isinstance(group.get("multi_clause_risks"), list) else 0
    return (anchored_count + multi_count) >= 2


def _select_aggregate_target_text(
    host_risk: dict[str, Any],
    multi_risks: list[dict[str, Any]],
    clause_source: str,
) -> tuple[str, str]:
    clause_text = str(clause_source or "").strip()
    is_mixed = _risk_source_type(host_risk) == "anchored" and bool(multi_risks)
    if clause_text and is_mixed:
        mixed_target = _select_mixed_aggregate_primary_target(
            {
                "aggregate_type": "mixed_clause_risks",
                "clause_text": clause_text,
                "anchored_risk": host_risk,
                "anchored_risks": [host_risk],
            },
            str(host_risk.get("target_text") or host_risk.get("evidence_text") or host_risk.get("main_text") or host_risk.get("anchor_text") or "").strip(),
        )
        if mixed_target:
            if mixed_target != clause_text:
                return mixed_target, "anchored_primary_sentence"
            return mixed_target, "host_clause_text"
    if clause_text:
        return clause_text, "host_clause_text"

    host_source_type = _risk_source_type(host_risk)
    host_prefix = host_source_type if host_source_type in {"anchored", "multi_clause"} else "host"
    host_candidates = [
        (f"{host_prefix}.target_text", str(host_risk.get("target_text") or "").strip()),
        (f"{host_prefix}.evidence_text", str(host_risk.get("evidence_text") or "").strip()),
        (f"{host_prefix}.anchor_text", str(host_risk.get("anchor_text") or "").strip()),
        (f"{host_prefix}.main_text", str(host_risk.get("main_text") or "").strip()),
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
        total_risks = len(anchored_risks) + len(multi_risks)
        if total_risks < 2:
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
            "aggregate_type": _aggregate_group_type(anchored_risks, multi_risks),
            "aggregate_source_types": _aggregate_group_source_types(anchored_risks, multi_risks),
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
            item["aggregate_type"] = str(group.get("aggregate_type") or _aggregate_group_type([], []))
            item["aggregate_source_types"] = list(group.get("aggregate_source_types") or [])
            item["aggregate_member_risk_ids"] = list(group.get("source_risk_ids") or [])
            item["aggregate_anchored_risk_ids"] = list(group.get("anchored_risk_ids") or [])
            item["aggregate_multi_clause_risk_ids"] = list(group.get("multi_clause_risk_ids") or [])
            item["source_risk_ids"] = list(group.get("source_risk_ids") or [])
            item["target_text"] = str(group.get("target_text") or item.get("target_text") or "")
            item["target_text_source"] = str(group.get("target_text_source") or "")
            item["host_clause_uid"] = str(group.get("host_clause_uid") or "")
            item["host_clause_id"] = str(group.get("host_clause_id") or "")
            item["risk_source_type"] = "aggregated"
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
        str(risk.get("main_text") or "").strip(),
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

    if _is_effective_aggregate_group(aggregate_group):
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

    guided_context = _resolve_suggestion_guided_patch_context(risk, merged_clauses)
    if guided_context is not None:
        target_text = str(guided_context.get("target_text") or target_text or "").strip()
        clause_source = str(guided_context.get("clause_text") or clause_source or "").strip()

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
    is_aggregate = _is_effective_aggregate_group(aggregate_group)
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
    else:
        normalized_target = re.sub(r"\s+", " ", str(rewrite_target_text or "").strip())
        normalized_revised = re.sub(r"\s+", " ", str(rewrite_revised_text or "").strip())
        if normalized_target and normalized_target == normalized_revised:
            merged_clauses = _safe_json(run_dir / "merged_clauses.json")
            if isinstance(merged_clauses, list):
                guided_context = _resolve_suggestion_guided_patch_context(risk, merged_clauses)
                if guided_context is not None:
                    rewrite_target_text = str(guided_context.get("target_text") or rewrite_target_text or "").strip()
                    rewrite_revised_text = str(guided_context.get("revised_text") or "").strip()
                    if rewrite_revised_text and rewrite_revised_text != rewrite_target_text:
                        rationale = (
                            str(rationale or "").strip()
                            or "原始 AI 改写未产生实际变更，已根据建议中的显式替换关系自动定位可修改片段并生成修订文本。"
                        )
                    else:
                        return {
                            "state": "failed",
                            "target_text": rewrite_target_text,
                            "revised_text": "",
                            "comment_text": "AI 改写未产生实际变更，且未能根据建议定位到可替换片段。",
                            "rationale": str(rationale or "").strip(),
                            "edit_type": edit_type or "replace",
                            "workflow_kind": "default",
                            "created_at": _iso_now(),
                        }
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
    _sync_ai_aggregation_file(run_dir=run_dir, validated=validated, reviewed=reviewed)
    _sanitize_reviewed_ai_payload(reviewed, run_dir=run_dir)
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
    if isinstance(clauses, list):
        if _sanitize_reviewed_display_payload(validated, clauses):
            _persist_reviewed_payload(run_dir, validated)
    meta = _read_meta(run_id)
    can_export_reviewed_docx = _can_export_reviewed_docx(run_id)
    return {
        "run_id": run_id,
        "status": meta.get("status"),
        "file_name": meta.get("file_name"),
        "review_side": meta.get("review_side"),
        "contract_type_hint": meta.get("contract_type_hint"),
        "analysis_scope": meta.get("analysis_scope"),
        "analysis_scope_label": meta.get("analysis_scope_label"),
        "merged_clauses": clauses,
        "risk_result_validated": validated,
        "risk_result_ai_aggregated": _safe_json(_aggregation_file_path(run_dir)),
        "download_ready": can_export_reviewed_docx,
        "download_url": f"/api/reviews/{run_id}/download" if can_export_reviewed_docx else None,
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


def _can_export_reviewed_docx(run_id: str) -> bool:
    run_dir = RUN_ROOT / run_id
    return _resolve_document_path(run_id) is not None and (run_dir / "merged_clauses.json").exists()


def _to_history_item(meta: dict[str, Any]) -> dict[str, Any]:
    run_id = str(meta.get("run_id") or "")
    run_dir = RUN_ROOT / run_id
    document_path = _resolve_document_path(run_id)
    return {
        "run_id": run_id,
        "file_name": meta.get("file_name"),
        "status": meta.get("status") or "running",
        "review_side": meta.get("review_side"),
        "contract_type_hint": meta.get("contract_type_hint"),
        "analysis_scope": meta.get("analysis_scope"),
        "analysis_scope_label": meta.get("analysis_scope_label"),
        "updated_at": meta.get("updated_at") or (_latest_mtime_iso(run_dir) if run_dir.exists() else None),
        "step": meta.get("step"),
        "warning": meta.get("warning"),
        "error": meta.get("error"),
        "download_ready": _can_export_reviewed_docx(run_id),
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


def _run_pipeline(*, run_id: str, file_path: Path, file_name: str, review_side: str, contract_type_hint: str, analysis_scope: str) -> None:
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
    env["ANALYSIS_SCOPE"] = analysis_scope

    _write_meta(
        run_id,
        {
            "status": "running",
            "file_name": file_name,
            "review_side": review_side,
            "contract_type_hint": contract_type_hint,
            "analysis_scope": analysis_scope,
            "analysis_scope_label": analysis_scope_label(analysis_scope),
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
    analysis_scope = normalize_analysis_scope(getattr(settings, "analysis_scope", "full_detail"))
    return {
        "review_side": normalized_review_side,
        "contract_type_hint": settings.contract_type_hint,
        "analysis_scope": analysis_scope,
        "analysis_scope_label": analysis_scope_label(analysis_scope),
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/reviews")
async def create_review(
    file: UploadFile = File(...),
    review_side: str = Form(settings.review_side),
    contract_type_hint: str = Form("service_agreement"),
    analysis_scope: str = Form(getattr(settings, "analysis_scope", "full_detail")),
) -> dict[str, Any]:
    normalized_review_side = _normalize_review_side(review_side or settings.review_side)
    normalized_analysis_scope = normalize_analysis_scope(analysis_scope or getattr(settings, "analysis_scope", "full_detail"))
    suffix = Path(file.filename or "contract.docx").suffix.lower()
    if suffix != ".docx":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "UNSUPPORTED_FILE_TYPE",
                "title": "文件格式不支持",
                "user_message": "请上传 .docx 格式的合同文件后再试。",
                "detail": f"目前仅支持 .docx 文件，收到：{suffix or 'unknown'}",
            },
        )

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
            "analysis_scope": normalized_analysis_scope,
            "analysis_scope_label": analysis_scope_label(normalized_analysis_scope),
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
            analysis_scope=normalized_analysis_scope,
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
    merged_clauses = _safe_json(merged_path)
    if isinstance(merged_clauses, list):
        _sanitize_reviewed_display_payload(reviewed_payload, merged_clauses)
    reviewed_path = run_dir / "risk_result_reviewed.json"
    reviewed_path.write_text(json.dumps(reviewed_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    locator_stdout = ""
    locator_stderr = ""
    try:
        locator_report = enrich_reviewed_risks_with_locators(run_id, run_root=RUN_ROOT)
        locator_stdout = json.dumps(locator_report, ensure_ascii=False, indent=2)
    except Exception as exc:
        locator_stderr = str(exc)

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
        "accepted,ai_applied",
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
            "[risk_locator]",
            locator_stdout,
            "[risk_comments]",
            comment_proc.stdout or "",
        ]
    )
    stderr = "\n".join(
        [
            "[ai_patch]",
            patch_proc.stderr or "",
            "[risk_locator]",
            locator_stderr,
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
        clauses = _load_run_clauses(run_dir)
        target["status"] = "ai_applied" if _has_other_accepted_risk_in_same_clause(target, risk_items, clauses) else "accepted"
        ai_rewrite = target.get("ai_rewrite") if isinstance(target.get("ai_rewrite"), dict) else {}
        if str(ai_rewrite.get("state") or "").strip().lower() == "succeeded":
            target["ai_rewrite_decision"] = "accepted"
            ai_patch = _build_ai_rewrite_patch(target)
            if ai_patch:
                target["accepted_patch"] = ai_patch
            else:
                target.pop("accepted_patch", None)
        else:
            suggest_insert_patch = _build_suggest_insert_patch(target)
            if suggest_insert_patch:
                target["accepted_patch"] = suggest_insert_patch
            else:
                target.pop("accepted_patch", None)
    elif status == "rejected":
        target["ai_rewrite_decision"] = "rejected"
        target.pop("accepted_patch", None)
    elif status == "pending":
        if isinstance(target.get("ai_rewrite"), dict):
            target["ai_rewrite_decision"] = "proposed"
        target.pop("accepted_patch", None)

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
            ai_patch = _build_ai_rewrite_patch(item)
            if ai_patch:
                item["accepted_patch"] = ai_patch
            else:
                item.pop("accepted_patch", None)
        else:
            suggest_insert_patch = _build_suggest_insert_patch(item)
            if suggest_insert_patch:
                item["accepted_patch"] = suggest_insert_patch
            else:
                item.pop("accepted_patch", None)
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
    if _is_accepted_risk_status(target.get("status")):
        target["ai_rewrite_decision"] = "accepted"
        _refresh_accepted_patch_for_item(target)
    else:
        target["ai_rewrite_decision"] = "proposed"
        target.pop("accepted_patch", None)

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
                if _is_accepted_risk_status(risk.get("status")):
                    risk["ai_rewrite_decision"] = "accepted"
                    _refresh_accepted_patch_for_item(risk)
                else:
                    risk["ai_rewrite_decision"] = "proposed"
                    risk.pop("accepted_patch", None)
                created += 1
            except Exception as exc:
                failed += 1
                failure_target = _extract_target_text(risk)
                aggregate_group = _load_ai_aggregation_group(run_dir, risk)
                if _is_effective_aggregate_group(aggregate_group):
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
    is_aggregate = str(ai_rewrite.get("workflow_kind") or "").strip().lower() == "aggregate" or bool(str(target.get("aggregate_id") or "").strip())
    aggregate_context = _resolve_aggregate_context(run_dir, target) if is_aggregate else target
    preserve_full_clause = _use_full_clause_target(aggregate_context)
    existing_target = _normalize_target_text(
        str(ai_rewrite.get("target_text") or ""),
        preserve_full_clause=preserve_full_clause,
    )
    submitted_target = _normalize_target_text(
        str(body.target_text or ""),
        preserve_full_clause=preserve_full_clause,
    )
    if submitted_target:
        ai_rewrite["target_text"] = submitted_target
    if is_aggregate:
        current_target = submitted_target or existing_target
    else:
        current_target = submitted_target or existing_target or _extract_target_text(target)
    if revised_text:
        ai_rewrite["revised_text"] = revised_text
    if is_aggregate:
        current_target, normalized_revised = _finalize_aggregate_patch_pair(
            aggregate_context,
            ai_rewrite,
            str(ai_rewrite.get("revised_text") or revised_text or ""),
        )
        ai_rewrite["revised_text"] = normalized_revised
    if current_target:
        ai_rewrite["target_text"] = current_target
    ai_rewrite["comment_text"] = _build_ai_comment_text(
        target_text=str(ai_rewrite.get("target_text") or current_target or ""),
        revised_text=str(ai_rewrite.get("revised_text") or ""),
    )
    target["status"] = "ai_applied"
    target["ai_rewrite_decision"] = "accepted"
    ai_patch = _build_ai_rewrite_patch(target)
    if ai_patch:
        target["accepted_patch"] = ai_patch
    else:
        target.pop("accepted_patch", None)

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

    workflow_kind = str(ai_rewrite.get("workflow_kind") or "").strip().lower()
    is_aggregate = workflow_kind == "aggregate" or bool(str(target.get("aggregate_id") or "").strip())
    aggregate_context = _resolve_aggregate_context(run_dir, target) if is_aggregate else target
    preserve_full_clause = _use_full_clause_target(aggregate_context)
    current_target = _normalize_target_text(
        str(ai_rewrite.get("target_text") or ""),
        preserve_full_clause=preserve_full_clause,
    )
    ai_rewrite["revised_text"] = revised_text
    if is_aggregate:
        resolved_target, normalized_revised = _finalize_aggregate_patch_pair(aggregate_context, ai_rewrite, revised_text)
        ai_rewrite["revised_text"] = normalized_revised
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
    target.pop("accepted_patch", None)

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
    target.pop("accepted_patch", None)
    target["ai_rewrite_decision"] = "rejected"
    target["status"] = "rejected"

    _persist_reviewed_payload(run_dir, reviewed)
    return {"ok": True, "item": target}
