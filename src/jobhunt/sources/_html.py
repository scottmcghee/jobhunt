"""Tiny HTML-to-text helper. We don't need a full parser for job bodies."""

from __future__ import annotations

import html
import re

_BLOCK_TAGS = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section|article)[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")


def to_text(fragment: str | None) -> str:
    """Convert an HTML fragment to readable plain text.

    Block-level tags become newlines, everything else is dropped, entities are
    unescaped, and whitespace is normalized. Good enough for keyword matching
    and for feeding an LLM; not meant to be a faithful rendering.
    """
    if not fragment:
        return ""
    s = fragment
    # Greenhouse (and some Lever boards) return HTML that is itself entity-escaped
    # ("&lt;p&gt;"). Unescape until stable so tags become real tags before stripping.
    for _ in range(3):
        unescaped = html.unescape(s)
        if unescaped == s:
            break
        s = unescaped
    s = _BLOCK_TAGS.sub("\n", s)
    s = _ANY_TAG.sub("", s)
    s = _WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    s = _NL.sub("\n\n", s)
    return s.strip()
