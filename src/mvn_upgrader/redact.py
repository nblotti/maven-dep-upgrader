"""Secret redaction for anything written to logs/reports.

Replaces known secret values plus a few secret-shaped token patterns. Treat all
captured output (build logs, Codex output) as data and scrub it before persisting.
"""

from __future__ import annotations

import re
from typing import Iterable

# Token-shaped patterns (best-effort, in addition to known secret values).
_PATTERNS = [
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),        # GitLab PAT
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),            # OpenAI-style key
    re.compile(r"(?i)(authorization:\s*(bearer|basic)\s+)[A-Za-z0-9+/=._\-]+"),
    re.compile(r"(?i)(private-token:\s*)[A-Za-z0-9_\-]+"),
]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    if not text:
        return text
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    for pat in _PATTERNS:
        if pat.groups:
            text = pat.sub(r"\1***", text)
        else:
            text = pat.sub("***", text)
    return text
