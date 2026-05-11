from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

DEFAULT_ANALYSIS_SCOPE = "full_detail"

_ANALYSIS_SCOPE_ALIASES: dict[str, str] = {
    "": DEFAULT_ANALYSIS_SCOPE,
    "all": "full_detail",
    "all_detail": "full_detail",
    "all_detailed": "full_detail",
    "detail": "full_detail",
    "detailed": "full_detail",
    "full": "full_detail",
    "full_detail": "full_detail",
    "full_detailed": "full_detail",
    "high": "high_risk_only",
    "high_only": "high_risk_only",
    "high_risk": "high_risk_only",
    "high_risk_only": "high_risk_only",
    "only_high": "high_risk_only",
}


def _is_high_risk(item: dict[str, Any]) -> bool:
    return str(item.get("risk_level") or "").strip().lower() == "high"


_ANALYSIS_SCOPE_FILTERS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "full_detail": lambda _item: True,
    "high_risk_only": _is_high_risk,
}


_ANALYSIS_SCOPE_LABELS: dict[str, str] = {
    "full_detail": "深度审查",
    "high_risk_only": "仅高风险",
}


def normalize_analysis_scope(value: str | None, default: str = DEFAULT_ANALYSIS_SCOPE) -> str:
    raw = str(value or "").strip().lower()
    normalized_default = _ANALYSIS_SCOPE_ALIASES.get(str(default or "").strip().lower(), DEFAULT_ANALYSIS_SCOPE)
    return _ANALYSIS_SCOPE_ALIASES.get(raw, normalized_default)


def analysis_scope_label(value: str | None) -> str:
    scope = normalize_analysis_scope(value)
    return _ANALYSIS_SCOPE_LABELS.get(scope, _ANALYSIS_SCOPE_LABELS[DEFAULT_ANALYSIS_SCOPE])


def apply_analysis_scope(payload: dict[str, Any], scope: str | None) -> dict[str, Any]:
    normalized_scope = normalize_analysis_scope(scope)
    predicate = _ANALYSIS_SCOPE_FILTERS.get(normalized_scope, _ANALYSIS_SCOPE_FILTERS[DEFAULT_ANALYSIS_SCOPE])

    cloned = deepcopy(payload) if isinstance(payload, dict) else {"risk_items": []}
    risk_items = cloned.get("risk_items") if isinstance(cloned, dict) else []
    if not isinstance(risk_items, list):
        return cloned

    filtered_items = [item for item in risk_items if isinstance(item, dict) and predicate(item)]
    cloned["risk_items"] = filtered_items
    cloned["analysis_scope"] = normalized_scope
    cloned["analysis_scope_summary"] = {
        "scope": normalized_scope,
        "label": analysis_scope_label(normalized_scope),
        "input_count": len(risk_items),
        "output_count": len(filtered_items),
    }
    return cloned
