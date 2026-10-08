"""USAJOBS, the US federal government's jobs site, through its documented search API.

Docs: https://developer.usajobs.gov/api-reference/get-api-search

    GET https://data.usajobs.gov/api/Search?Keyword=...&LocationName=...&Radius=...
        &ResultsPerPage=500&Page=N&Fields=Full
    headers: Host: data.usajobs.gov, User-Agent: <the email the key was requested with>,
             Authorization-Key: <the key>

The API needs a free key (settings ``usajobs.api_key`` and ``usajobs.email``); without both, a
USAJOBS board is skipped with a warning. Federal postings run to the tens of thousands, so
``fetch`` searches once per term, like the other search sources; descriptions come in the
results. A board is ``ats: usajobs`` with ``slug:`` where to search: a place USAJOBS knows
(``Seattle, Washington``), optionally with a radius in miles (``Seattle, Washington/50``), or
``remote`` for remote jobs anywhere. A place's search includes its remote jobs too.

Each posting's company is its agency (``OrganizationName``), and its id is the control number
(``MatchedObjectId``, the number in its link); one announcement (``PositionID``) can have several.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from concurrent.futures import Executor
from datetime import UTC, datetime

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._rate import RATE
from jobhunt.sources._search import terms

log = logging.getLogger(__name__)

BASE = "https://data.usajobs.gov/api/Search"
PAGE_SIZE = 500  # the most the API returns at once
MAX_PER_TERM = 2000  # default per-term cap (fetch.max_per_term); the API stops at 10,000
RATE_CAP = 1.0  # requests a second: a handful of searches needs no more
_RADIUS = re.compile(r"(.+)/(\d+)")


def _where(company: Company) -> dict[str, str]:
    """The query parameters for the board's slug: a place (and radius), or remote."""
    if company.slug.lower() == "remote":
        return {"RemoteIndicator": "True"}
    if m := _RADIUS.fullmatch(company.slug):
        return {"LocationName": m.group(1).strip(), "Radius": m.group(2)}
    return {"LocationName": company.slug}


def _posted(value: object) -> str | None:
    """'2026-09-30T00:00:00.0000' (no zone given) as ISO 8601 in UTC, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value[:19]).replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


def normalize(company: Company, item: dict) -> Job:
    d = item.get("MatchedObjectDescriptor") or {}
    details = (d.get("UserArea") or {}).get("Details") or {}
    duties = details.get("MajorDuties") or []
    parts = [
        to_text(details.get("JobSummary")),
        "\n".join(to_text(x) for x in duties if isinstance(x, str)),
        to_text(d.get("QualificationSummary")),
    ]
    location = d.get("PositionLocationDisplay") or ""
    cid = str(item["MatchedObjectId"])
    url = str(d.get("PositionURI") or f"https://www.usajobs.gov/job/{cid}").replace(":443/", "/")
    remote = details.get("RemoteIndicator") is True or "remote" in location.lower()
    return Job(
        source="usajobs",
        company=d.get("OrganizationName") or company.name,
        company_slug=company.slug,
        external_id=cid,
        title=d.get("PositionTitle", ""),
        location=location,
        remote=True if remote else None,
        url=url,
        body="\n\n".join(p for p in parts if p),
        posted_at=_posted(d.get("PublicationStartDate")),
    )


def _page(
    company: Company, client: httpx.Client, term: str, page: int, auth: tuple[str, str]
) -> tuple[list[dict], int, int]:
    key, email = auth
    resp = client.get(
        BASE,
        params={
            "Keyword": term, **_where(company), "ResultsPerPage": PAGE_SIZE, "Page": page,
            "Fields": "Full",
        },
        headers={"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key},
        extensions={RATE: RATE_CAP},
        follow_redirects=False,  # the key and email go to data.usajobs.gov only
    )
    resp.raise_for_status()
    result = resp.json().get("SearchResult") or {}
    items = [i for i in result.get("SearchResultItems") or [] if isinstance(i, dict)]
    try:
        pages = int((result.get("UserArea") or {}).get("NumberOfPages") or 1)
    except ValueError:
        pages = 1
    try:
        total = int(result.get("SearchResultCountAll") or 0)
    except ValueError:
        total = 0
    return items, pages, total


def fetch(
    company: Company,
    client: httpx.Client,
    search: Iterable[str] = (),
    max_pages: int | None = None,
    wants_body: Callable[[Job], bool] | None = None,  # descriptions come in the results
    pool: Executor | None = None,  # a few searches; nothing to spread
    max_per_term: int | None = None,
    auth: tuple[str, str] | None = None,
) -> list[Job]:
    """Every posting any of the search terms finds where the board's slug says."""
    if not auth or not all(auth):
        log.warning(
            "usajobs %s: skipped; set usajobs.api_key and usajobs.email in settings", company.slug
        )
        return []
    cap = max_per_term or MAX_PER_TERM
    found: dict[str, Job] = {}
    for term in terms(search, source="usajobs"):
        page, pages, seen, total = 1, 1, 0, 0
        while page <= pages and seen < cap and (max_pages is None or page <= max_pages):
            items, pages, total = _page(company, client, term, page, auth)
            for item in items[: cap - seen]:
                if item.get("MatchedObjectId"):
                    job = normalize(company, item)
                    found.setdefault(job.external_id, job)
            seen += len(items)
            if not items:
                break
            page += 1
        if seen >= cap and total > cap:
            log.warning(
                "usajobs %s: %r has %d hits; kept the first %d", company.slug, term, total, cap
            )
    log.info("usajobs %s: %d jobs", company.slug, len(found))
    return list(found.values())
