from __future__ import annotations

from typing import Any

from .parse_outputs import parse_clause_payload



def merge_clause_batches(batches: list[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for batch in batches:
        if batch is None:
            continue
        clauses = parse_clause_payload(batch)
        merged.extend(clauses)
    return merged
