from __future__ import annotations

import re


def clean_contract_text(text: str) -> str:
    cleaned = text or ""
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\u3000", " ")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    parts = re.split(r"（以下无正文）|\(以下无正文\)|以下无正文", cleaned, maxsplit=1)
    cleaned = parts[0].strip()
    return cleaned
