"""Amazon's own careers site (amazon.jobs). Not an ATS: Amazon runs its own job search.

Endpoint (public, no auth; the one the site's search page calls; robots.txt only disallows
the /internal pages):

    GET https://www.amazon.jobs/en/search.json?base_query=...&normalized_country_code[]=USA
        &result_limit=100&offset=N&sort=recent

Amazon lists tens of thousands of roles, so ``fetch`` searches instead of listing everything:
one query per search term (``fetch`` passes the title filter's target-level words), paged 100 at
a time, results deduped across terms. Descriptions come in the search results. A term keeps
at most its first 2,000 hits in Amazon's ``sort=recent`` order, which is roughly but not
strictly by posting date.

A board is ``ats: amazon`` with ``slug:`` an ISO 3166 alpha-3 country code (``USA``), the
country the search is limited to.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import Executor
from datetime import datetime

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids
from jobhunt.sources._search import terms

log = logging.getLogger(__name__)

BASE = "https://www.amazon.jobs/en/search.json"
SITE = "https://www.amazon.jobs"
PAGE_SIZE = 100  # the most the API returns per page
MAX_PER_TERM = 2000  # runaway guard; the API itself stops at 10,000 hits


def _posted(raw: str | None) -> str | None:
    """'October  6, 2026' -> '2026-10-06'."""
    try:
        return datetime.strptime(" ".join((raw or "").split()), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _body(raw: dict) -> str:
    parts = [to_text(raw.get("description"))]
    for label, key in (("Basic qualifications", "basic_qualifications"),
                       ("Preferred qualifications", "preferred_qualifications")):
        if text := to_text(raw.get(key)):
            parts.append(f"{label}:\n{text}")
    return "\n\n".join(p for p in parts if p)


def _locations(raw: dict) -> list[dict]:
    """The posting's ``locations`` (JSON strings, one per location); unreadable ones are skipped."""
    out = []
    for entry in raw.get("locations") or []:
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except ValueError:
                continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _remote(locations: list[dict], where: str) -> bool | None:
    kinds = {str(loc.get("type") or "").upper() for loc in locations}
    if "VIRTUAL" in kinds:
        return True
    if kinds == {"ONSITE"}:
        return False
    return True if "virtual" in where or "remote" in where else None


def normalize(company: Company, raw: dict) -> Job:
    locations = _locations(raw)
    # Many postings list several cities; the first is only in normalized_location.
    names = [loc.get("normalizedLocation") for loc in locations]
    names = list(dict.fromkeys(n for n in names if isinstance(n, str) and n))
    location = "; ".join(names) or raw.get("normalized_location") or raw.get("location") or ""
    where = f"{raw.get('location', '')} {location} {raw.get('title', '')}".lower()
    return Job(
        source="amazon",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw.get("id_icims") or raw["id"]),
        title=raw.get("title", ""),
        location=location,
        remote=_remote(locations, where),
        url=SITE + (raw.get("job_path") or ""),
        body=_body(raw),
        posted_at=_posted(raw.get("posted_date")),
    )


def _page(company: Company, client: httpx.Client, term: str, offset: int) -> tuple[list[dict], int]:
    params = {
        "base_query": term,
        "normalized_country_code[]": company.slug,
        "result_limit": PAGE_SIZE,
        "offset": offset,
        "sort": "recent",
    }
    resp = client.get(BASE, params=params)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"amazon search {term!r}: {data['error']}")
    return data.get("jobs") or [], int(data.get("hits") or 0)


def fetch(
    company: Company,
    client: httpx.Client,
    search: Iterable[str] = (),
    max_pages: int | None = None,
    wants_body: Callable[[Job], bool] | None = None,  # descriptions come in the results
    pool: Executor | None = None,  # pages are fetched in order; nothing to spread
) -> list[Job]:
    """Every posting any of the search terms finds, in the board's country."""
    found: dict[str, Job] = {}
    for term in terms(search, source="amazon"):
        offset = pages = 0
        while offset < MAX_PER_TERM and (max_pages is None or pages < max_pages):
            postings, hits = _page(company, client, term, offset)
            pages += 1
            for raw in with_ids(company, postings):
                job = normalize(company, raw)
                found.setdefault(job.external_id, job)
            offset += len(postings)
            if not postings or offset >= hits:  # an empty page ends it, whatever hits says
                break
        else:
            if offset >= MAX_PER_TERM:
                log.warning(
                    "amazon %s: %s has %d hits; kept the first %d in Amazon's 'recent' order"
                    " (not strictly by posting date)",
                    company.slug, term, hits, offset,
                )
    if not found:
        log.warning(
            "amazon %s: 0 postings — check the country code (ISO alpha-3, e.g. USA)", company.slug
        )
    log.info("amazon %s: %d jobs", company.slug, len(found))
    return list(found.values())
