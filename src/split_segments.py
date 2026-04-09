from __future__ import annotations

import re
from typing import Any


HeadingStyle = str


CHINESE_HEADING = re.compile(r"(?m)^[一二三四五六七八九十百]+、.*$")
ARTICLE_HEADING = re.compile(r"(?m)^第[一二三四五六七八九十百0-9]+条.*$")
ARABIC_HEADING = re.compile(r"(?m)^[0-9]+、.*$")



def detect_heading_style(text: str) -> HeadingStyle | None:
    chinese = list(CHINESE_HEADING.finditer(text))
    article = list(ARTICLE_HEADING.finditer(text))
    arabic = list(ARABIC_HEADING.finditer(text))

    if len(chinese) >= 3:
        return "chinese"
    if len(article) >= 3:
        return "article"
    if len(arabic) >= 3:
        return "arabic"
    return None



def get_heading_pattern(style: HeadingStyle | None) -> re.Pattern[str] | None:
    if style == "chinese":
        return CHINESE_HEADING
    if style == "article":
        return ARTICLE_HEADING
    if style == "arabic":
        return ARABIC_HEADING
    return None



def split_into_segments(text: str) -> dict[str, Any]:
    style = detect_heading_style(text)
    pattern = get_heading_pattern(style)

    if pattern is None:
        return {
            "heading_style": "fallback_fulltext",
            "segment_count": 1,
            "segments": [
                {
                    "segment_id": "segment_1",
                    "segment_title": "全文",
                    "segment_text": text.strip(),
                }
            ],
        }

    matches = list(pattern.finditer(text))
    segments: list[dict[str, str]] = []

    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        segment_text = text[start:end].strip()
        title_line = match.group(0).strip()
        if segment_text:
            segments.append(
                {
                    "segment_id": f"segment_{len(segments) + 1}",
                    "segment_title": title_line,
                    "segment_text": segment_text,
                }
            )

    if not segments:
        segments = [
            {
                "segment_id": "segment_1",
                "segment_title": "全文",
                "segment_text": text.strip(),
            }
        ]
        style = "fallback_fulltext"

    return {
        "heading_style": style,
        "segment_count": len(segments),
        "segments": segments,
    }
