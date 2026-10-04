"""Guard shared by the ATS adapters against malformed listings."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from jobhunt.schema import Company

log = logging.getLogger(__name__)


def with_ids(company: Company, postings: Iterable[dict], field: str = "id") -> list[dict]:
    """The postings that have ``field``. A posting without its ID can't be deduped or linked."""
    kept = []
    for posting in postings:
        if posting.get(field):
            kept.append(posting)
        else:
            log.warning("%s %s: skipped a posting with no %s", company.ats, company.slug, field)
    return kept
