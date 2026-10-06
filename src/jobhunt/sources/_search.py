"""Search terms for sources that search instead of listing every posting (Amazon, Eightfold)."""

from __future__ import annotations

import logging
from collections.abc import Iterable

log = logging.getLogger(__name__)


def terms(search: Iterable[str], source: str) -> list[str]:
    """Cleaned, unique search terms; a trailing filter wildcard ("recruit*") is dropped.

    No terms at all means one unfiltered search ("").
    """
    raw = list(dict.fromkeys(t.strip().lower() for t in search))
    for term in raw:
        if term.endswith("*") and (stem := term.rstrip("*").strip()):
            # these searches match whole words, so "recruit" won't find "Recruiter"
            log.warning("%s can't search by prefix; searching %r only for %r", source, stem, term)
    cleaned = [t.rstrip("*").strip() for t in raw]
    return list(dict.fromkeys(t for t in cleaned if t)) or [""]
