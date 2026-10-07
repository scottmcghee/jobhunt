"""Phenom careers sites (e.g. careers.adobe.com). Not an ATS: Phenom fronts one, often Workday.

Every Phenom site answers one public JSON endpoint on its own host, the one its search page uses:

    POST https://{host}/widgets  {"ddoKey": "refineSearch", "keywords": ..., "from": N, "size": 100}
    POST https://{host}/widgets  {"ddoKey": "jobDetail", "jobId": ...}               one posting

A board is ``ats: phenom`` with ``slug:`` the site's host, country and language, as in its URLs:
``careers.adobe.com/us/en`` (some sites use ``global/en``). A site whose URLs have no country and
language (careers.davita.com) is just its host; its API then takes ``us/en``.

Big employers list thousands of roles (CVS about 19,000), so ``fetch`` searches once per term,
and asks for a description only for postings that pass ``wants_body``.

A wrong country or language finds nothing rather than a 404, so ``fetch`` warns about a board
with no postings instead.

Many Phenom sites front a Workday board (the postings' apply links say which). Fetching both
fetches every job twice, under two keys, so add only one of them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import Executor

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids
from jobhunt.sources._search import terms

log = logging.getLogger(__name__)

PAGE_SIZE = 100  # the most the site returns at once
MAX_PER_TERM = 500  # default per-term cap (fetch.max_per_term); a runaway guard for broad terms
DEFAULT_LOCALE = ("us", "en")  # what the API takes for a site with no locale in its URLs
# Cisco's search results say RemoteType; DaVita's and Adobe's postings say remote
_REMOTE_FIELDS = ("RemoteType", "remoteType", "remote")
_NOT_REMOTE = {"no", "false", "onsite", "on-site", "onsite only", "on site", "hybrid", "in office"}


def _site(company: Company) -> tuple[str, str, str]:
    host, *locale = company.slug.split("/")
    country, lang = locale or DEFAULT_LOCALE
    return host.lower(), country, lang


def _job_url(company: Company, job_id: str) -> str:
    host, *locale = company.slug.split("/")
    return "/".join([f"https://{host.lower()}", *locale, "job", job_id])


def _request(company: Company, body: dict) -> tuple[str, dict]:
    host, country, lang = _site(company)
    return f"https://{host}/widgets", {
        "lang": f"{lang}_{country}", "country": country, "deviceType": "desktop", **body
    }


def search_request(company: Company, term: str, start: int = 0) -> tuple[str, dict]:
    """The URL and JSON body of one search page (scripts/survey_careers.py sends it too)."""
    return _request(company, {
        "pageName": "search-results", "ddoKey": "refineSearch", "pageId": "page11",
        "siteType": "external", "keywords": term, "from": start, "size": PAGE_SIZE,
        "jobs": True, "counts": False, "global": True, "selected_fields": {},
    })


def search_results(data: object) -> tuple[list[dict], int]:
    """A search page's postings and total hits; ValueError if the answer isn't one."""
    search = data.get("refineSearch") if isinstance(data, dict) else None
    if not isinstance(search, dict):
        raise ValueError("no search results in the answer")
    found = search.get("data") or {}
    jobs = (found.get("jobs") or []) if isinstance(found, dict) else None
    total = search.get("totalHits") or 0
    if not isinstance(jobs, list) or not isinstance(total, int | str):
        raise ValueError("malformed search results in the answer")
    return jobs, int(total)


def _post(client: httpx.Client, request: tuple[str, dict]) -> object:
    url, body = request
    resp = client.post(url, json=body)
    resp.raise_for_status()
    return resp.json()


def _page(company: Company, client: httpx.Client, term: str, start: int) -> tuple[list[dict], int]:
    return search_results(_post(client, search_request(company, term, start)))


def _detail(company: Company, client: httpx.Client, raw: dict) -> dict | None:
    try:
        body = {"pageName": "job", "ddoKey": "jobDetail", "jobId": raw["jobId"]}
        data = _post(client, _request(company, body))
        detail = data.get("jobDetail") if isinstance(data, dict) else None
        job = ((detail or {}).get("data") or {}).get("job")
        if not isinstance(job, dict) or not job.get("description"):
            raise ValueError("no job in the answer")
        return job
    except (httpx.HTTPError, ValueError) as e:
        error = " ".join(str(e).split())  # httpx's messages can span lines
        log.warning("phenom %s: no description for %s (%s)", company.slug, raw["jobId"], error)
        return None


def _remote(raw: dict, detail: dict | None) -> bool | None:
    """A remote field if a site sets one (names vary); else True if the location says remote."""
    for source in (detail or {}, raw):
        for field in _REMOTE_FIELDS:
            value = str(source.get(field) or "").strip().lower()
            if value in ("yes", "true") or value.startswith(("remote", "fully remote")):
                return True
            if value in _NOT_REMOTE:
                return False
    return True if "remote" in _location(raw).lower() else None


def _location(raw: dict) -> str:
    places = raw.get("multi_location")
    if isinstance(places, list) and places:
        return "; ".join(dict.fromkeys(p for p in places if isinstance(p, str) and p))
    return raw.get("location") or ""


def normalize(company: Company, raw: dict, detail: dict | None = None) -> Job:
    return Job(
        source="phenom",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["jobId"]),
        title=raw.get("title") or "",
        location=_location(raw),
        remote=_remote(raw, detail),
        url=_job_url(company, str(raw["jobId"])),
        body=to_text(detail.get("description")) if detail else "",
        posted_at=raw.get("postedDate"),
    )


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
    for term in terms(search, source="phenom"):
        seen = pages = 0
        while seen < cap and (max_pages is None or pages < max_pages):
            rows, total = _page(company, client, term, seen)
            rows = rows[: cap - seen]  # the cap may end mid-page
            pages += 1
            for raw in with_ids(company, rows, field="jobId"):
                job = normalize(company, raw)
                found.setdefault(job.external_id, (raw, job))
            seen += len(rows)
            if not rows or seen >= total:
                break
        else:  # the loop's own condition stopped it: the cap, or max_pages
            if seen >= cap:
                log.warning(
                    "phenom %s: %r has %d hits; kept the first %d",
                    company.slug, term, total, seen,
                )
    if not found:
        log.warning(
            "phenom %s: 0 postings — check the slug (host/country/language, e.g. "
            "careers.example.com/us/en)",
            company.slug,
        )
    wanted = [(raw, job) for raw, job in found.values() if wants_body(job)]
    run = pool.map if pool is not None else map
    details = run(lambda pair: _detail(company, client, pair[0]), wanted)
    for (raw, job), detail in zip(wanted, details, strict=True):
        if detail:
            found[job.external_id] = (raw, normalize(company, raw, detail))
    log.info("phenom %s: %d jobs", company.slug, len(found))
    return [job for _, job in found.values()]
