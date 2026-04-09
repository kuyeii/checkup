from __future__ import annotations

from pathlib import Path
from typing import Any

from .parse_outputs import parse_clause_payload


def load_existing_clause_batch(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    return parse_clause_payload(path.read_text(encoding="utf-8"))
