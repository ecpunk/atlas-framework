"""Secrets-only content scanner for KB writes.

mcp_server.py loads this file (next to it in tools/) and calls
scan_text(content, profile="secrets", source_label=...) before every KB write;
any hit refuses the write. KB docs are committed (and often pushed), so
credential material must never land in them.

Only high-confidence, low-false-positive live-secret formats are matched. The
module fails closed: if it is missing, mcp_server.py refuses every KB write.

Same API as the larger leak gate it was cut from: scan_text() returns a list of
Hit(path, line, why, snippet); unknown profiles raise ValueError.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PROFILE_NAMES = ("secrets",)

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("discord webhook url", re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[\w-]{20,}")),
    ("anthropic api key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai api key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
]


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    why: str
    snippet: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.why} :: {self.snippet.strip()[:160]}"


def scan_text(text: str, profile: str = "secrets", extra: list[str] | None = None,
              source_label: str = "<submitted>") -> list[Hit]:
    """Scan one in-memory string for secret-shaped content. `extra` adds
    case-insensitive literal terms to forbid."""
    if profile not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {profile!r}; choose from {PROFILE_NAMES}")
    lit_res = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in (extra or []) if t]
    hits: list[Hit] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        for term, rx in lit_res:
            if rx.search(raw):
                hits.append(Hit(source_label, lineno, f"forbidden term '{term}'", raw))
        for why, rx in _SECRET_PATTERNS:
            if rx.search(raw):
                hits.append(Hit(source_label, lineno, why, raw))
    return hits
