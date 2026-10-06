"""Eightfold careers sites, the platform behind Microsoft, Nvidia, Eaton, PayPal and others.

Endpoints (public, no auth; the ones each careers site calls; every Eightfold robots.txt seen
allows /careers and /api/pcsx):

    GET https://{host}/careers                       the careers page; it names the domain
    GET https://{host}/api/pcsx/search?domain=...&query=...&start=N[&location=...]
    GET https://{host}/api/pcsx/position_details?position_id=...&domain=...

A board is ``ats: eightfold`` with ``slug:`` the careers site's host, e.g. ``eaton.eightfold.ai``
or ``apply.careers.microsoft.com``, and optionally ``location:`` (e.g. ``United States``) to limit
its searches to one place. The API needs the company's ``domain`` (``eaton.com``), which the
careers page embeds; ``fetch`` reads it from there, one request per board per run.

These sites post thousands of roles, so ``fetch`` searches once per term, 10 results a page (the
API's fixed size), and asks for a description only for postings that pass ``wants_body``.
An unknown domain on a real host is a 404; an unknown ``*.eightfold.ai`` host doesn't resolve.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Iterable
from concurrent.futures import Executor
from datetime import UTC, datetime

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids
from jobhunt.sources._search import terms

log = logging.getLogger(__name__)

PAGE_SIZE = 10  # fixed by the API; a larger num is ignored
MAX_PER_TERM = 500  # 50 requests; a runaway guard for broad terms
_DOMAIN = re.compile(r'"domain"\s*:\s*"([a-z0-9][a-z0-9.-]*\.[a-z]{2,})"', re.I)
_REMOTE = {"remote": True, "onsite": False, "hybrid": False}


def _domain(page: str) -> str | None:
    """The company domain a careers page embeds, as entity-escaped JSON or plain."""
    m = _DOMAIN.search(html.unescape(page))
    return m.group(1).lower() if m else None


def _remote(raw: dict) -> bool | None:
    return _REMOTE.get((raw.get("workLocationOption") or "").lower())


def _posted(ts: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), UTC).isoformat()  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def normalize(company: Company, raw: dict, detail: dict | None = None) -> Job:
    locations = [str(loc) for loc in raw.get("locations") or [] if loc]
    path = raw.get("positionUrl") or f"/careers/job/{raw['id']}"
    return Job(
        source="eightfold",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=raw.get("name", ""),
        location="; ".join(dict.fromkeys(locations)),
        remote=_remote(raw),
        url=f"https://{company.slug.lower()}{path}",
        body=to_text((detail or {}).get("jobDescription")),
        posted_at=_posted(raw.get("postedTs")),
    )


def _careers_domain(company: Company, client: httpx.Client) -> str:
    resp = client.get(f"https://{company.slug.lower()}/careers")
    resp.raise_for_status()
    if domain := _domain(resp.text):
        return domain
    raise ValueError(f"no Eightfold domain on https://{company.slug}/careers")


def _page(
    company: Company, client: httpx.Client, domain: str, term: str, start: int
) -> tuple[list[dict], int]:
    params = {"domain": domain, "query": term, "start": start}
    if company.location:
        params["location"] = company.location
    resp = client.get(f"https://{company.slug.lower()}/api/pcsx/search", params=params)
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    return data.get("positions") or [], int(data.get("count") or 0)


def _detail(company: Company, client: httpx.Client, domain: str, posting_id: str) -> dict | None:
    try:
        resp = client.get(
            f"https://{company.slug.lower()}/api/pcsx/position_details",
            params={"position_id": posting_id, "domain": domain, "hl": "en"},
        )
        resp.raise_for_status()
        return resp.json().get("data") or None
    except (httpx.HTTPError, ValueError) as e:
        error = " ".join(str(e).split())  # httpx's messages can span lines
        log.warning("eightfold %s: no description for %s (%s)", company.slug, posting_id, error)
        return None


def fetch(
    company: Company,
    client: httpx.Client,
    search: Iterable[str] = (),
    max_pages: int | None = None,
    wants_body: Callable[[Job], bool] = lambda job: True,
    pool: Executor | None = None,
) -> list[Job]:
    """Every posting any search term finds; descriptions for those ``wants_body`` accepts."""
    domain = _careers_domain(company, client)
    found: dict[str, tuple[dict, Job]] = {}
    for term in terms(search, source="eightfold"):
        start = pages = 0
        while start < MAX_PER_TERM and (max_pages is None or pages < max_pages):
            positions, count = _page(company, client, domain, term, start)
            pages += 1
            for raw in with_ids(company, positions):
                job = normalize(company, raw)
                found.setdefault(job.external_id, (raw, job))
            start += len(positions)
            if not positions or start >= count:
                break
        else:  # the loop's own condition stopped it: the cap, or max_pages
            if start >= MAX_PER_TERM:
                log.warning(
                    "eightfold %s: %r has %d hits; kept the first %d",
                    company.slug, term, count, MAX_PER_TERM,
                )
    wanted = [(raw, job) for raw, job in found.values() if wants_body(job)]
    run = pool.map if pool is not None else map
    details = run(lambda pair: _detail(company, client, domain, pair[1].external_id), wanted)
    for (raw, job), detail in zip(wanted, details, strict=True):
        if detail:
            found[job.external_id] = (raw, normalize(company, raw, detail))
    log.info("eightfold %s: %d jobs", company.slug, len(found))
    return [job for _, job in found.values()]
