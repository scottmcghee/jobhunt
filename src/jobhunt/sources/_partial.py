"""Boards whose last successful fetch did not list every posting they have.

A source that cuts a listing short (a cap, a page that came back empty before the reported total,
a sitemap that failed or robots.txt ruled out) notes it here by board (``Company.key``), so
``listing_is_complete`` won't take the jobs it left out as closed. ``note`` may be called from
pool threads; ``take`` pops a board's note, so each fetch's note is read once.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_partial: dict[str, str] = {}


def note(company_key: str, why: str) -> None:
    """This fetch of the board left postings out, and why."""
    with _lock:
        _partial.setdefault(company_key, why)


def take(company_key: str) -> str | None:
    """Why this fetch of the board left postings out, or None if it didn't; clears it."""
    with _lock:
        return _partial.pop(company_key, None)
