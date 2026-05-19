"""Input sanitization helpers."""

from __future__ import annotations

import html
import re
from typing import Optional

_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_text(value: Optional[str], *, max_length: int = 32_000) -> str:
    """Strip HTML/script tags and entity-escape remaining text for safe storage/display.

    Rejects NUL bytes; truncates to ``max_length`` after normalization.
    """
    if value is None:
        return ""
    s = str(value).replace("\x00", "")
    s = _TAG_RE.sub("", s)
    s = html.escape(s, quote=True)
    if len(s) > max_length:
        s = s[:max_length]
    return s.strip()
