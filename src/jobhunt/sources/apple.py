"""Apple's own careers site (jobs.apple.com). Not an ATS, and not a JSON API.

Pages (public; jobs.apple.com serves no robots.txt): the search and job pages are rendered on the
server, and each embeds its data as ``window.__staticRouterHydrationData = JSON.parse("...")``.

    GET https://jobs.apple.com/en-us/search?location={slug}&search={term}&page=N   20 a page
    GET https://jobs.apple.com/en-us/details/{id}/{title-slug}                     one posting

A board is ``ats: apple`` with ``slug:`` the location filter from a search URL, e.g.
``united-states-USA``. Apple lists thousands of roles, so ``fetch`` searches once per term (a
multi-word term is sent as a "quoted phrase": unquoted, Apple matches any of its words, and
"head of" finds 4,000+ postings) and fetches a job page only for postings that pass
``wants_body``. The body is the job page's summary, description, responsibilities, and minimum
and preferred qualifications.

A mistyped location filter finds nothing rather than a 404, so ``fetch`` warns about a board with
no postings instead.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from concurrent.futures import Executor

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._postings import with_ids
from jobhunt.sources._search import terms

log = logging.getLogger(__name__)

SITE = "https://jobs.apple.com/en-us"
PAGE_SIZE = 20  # fixed by the site
MAX_PER_TERM = 400  # 20 pages; a runaway guard for broad terms
_DATA = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(("(?:[^"\\]|\\.)*")\)', re.S
)
_SECTIONS = (
    ("", "jobSummary"),
    ("", "description"),
    ("", "responsibilities"),
    ("Minimum qualifications", "minimumQualifications"),
    ("Preferred qualifications", "preferredQualifications"),
)


def _loader(page: str) -> dict:
    """The page's embedded data (its ``loaderData``)."""
    m = _DATA.search(page)
    if not m:
        raise ValueError("no job data on the page")
    return json.loads(json.loads(m.group(1))).get("loaderData") or {}


def _query(term: str) -> str:
    return f'"{term}"' if " " in term else term


def _location(raw: dict) -> str:
    places = []
    for loc in raw.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        city = loc.get("city") or loc.get("name")
        parts = [city, loc.get("stateProvince"), loc.get("countryName")]
        places.append(", ".join(dict.fromkeys(p for p in parts if p)))
    return "; ".join(dict.fromkeys(p for p in places if p))


def _body(detail: dict) -> str:
    parts = []
    for label, key in _SECTIONS:
        if text := (detail.get(key) or "").strip():
            parts.append(f"{label}:\n{text}" if label else text)
    return "\n\n".join(parts)


def normalize(company: Company, raw: dict, detail: dict | None = None) -> Job:
    return Job(
        source="apple",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=raw.get("postingTitle") or "",
        location=_location(raw),
        remote=True if raw.get("homeOffice") is True else None,
        url=_url(raw),
        body=_body(detail) if detail else "",
        posted_at=raw.get("postDateInGMT"),
    )


def _url(raw: dict) -> str:
    return f"{SITE}/details/{raw['id']}/{raw.get('transformedPostingTitle') or 'job'}"


def _page(company: Company, client: httpx.Client, term: str, page: int) -> tuple[list[dict], int]:
    params = {"location": company.slug, "search": _query(term), "page": page}
    resp = client.get(f"{SITE}/search", params=params)
    resp.raise_for_status()
    search = _loader(resp.text).get("search") or {}
    return search.get("searchResults") or [], int(search.get("totalRecords") or 0)


def _detail(company: Company, client: httpx.Client, raw: dict) -> dict | None:
    try:
        resp = client.get(_url(raw))
        resp.raise_for_status()
        return (_loader(resp.text).get("jobDetails") or {}).get("jobsData") or None
    except (httpx.HTTPError, ValueError) as e:
        error = " ".join(str(e).split())  # httpx's messages can span lines
        log.warning("apple %s: no description for %s (%s)", company.slug, raw["id"], error)
        return None


def fetch(
    company: Company,
    client: httpx.Client,
    search: Iterable[str] = (),
    max_pages: int | None = None,
    wants_body: Callable[[Job], bool] = lambda job: True,
    pool: Executor | None = None,
    max_per_term: int | None = None,
) -> list[Job]:
    """Every posting any search term finds; descriptions for those ``wants_body`` accepts."""
    cap = max_per_term or MAX_PER_TERM
    found: dict[str, tuple[dict, Job]] = {}
    for term in terms(search, source="apple"):
        seen = pages = 0
        while seen < cap and (max_pages is None or pages < max_pages):
            rows, total = _page(company, client, term, pages + 1)
            pages += 1
            for raw in with_ids(company, rows):
                job = normalize(company, raw)
                found.setdefault(job.external_id, (raw, job))
            seen += len(rows)
            if not rows or seen >= total:
                break
        else:  # the loop's own condition stopped it: the cap, or max_pages
            if seen >= cap:
                log.warning(
                    "apple %s: %r has %d hits; kept the first %d",
                    company.slug, term, total, cap,
                )
    if not found:
        log.warning(
            "apple %s: 0 postings — check the location filter (e.g. united-states-USA)",
            company.slug,
        )
    wanted = [(raw, job) for raw, job in found.values() if wants_body(job)]
    run = pool.map if pool is not None else map
    details = run(lambda pair: _detail(company, client, pair[0]), wanted)
    for (raw, job), detail in zip(wanted, details, strict=True):
        if detail:
            found[job.external_id] = (raw, normalize(company, raw, detail))
    log.info("apple %s: %d jobs", company.slug, len(found))
    return [job for _, job in found.values()]
