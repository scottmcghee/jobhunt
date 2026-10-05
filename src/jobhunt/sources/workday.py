"""Workday careers sites.

Workday has no documented public API. Each tenant's careers site loads its postings from JSON
endpoints on its own host, and those are what this module calls:

    POST https://{tenant}.{datacenter}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
         {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}
    GET  https://{tenant}.{datacenter}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

The listing has no descriptions and pages 20 at a time (only the first page reports the total),
so a description costs one request per posting. ``fetch`` takes a ``wants_body`` check and pays
that cost only for postings that pass it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Executor

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids

log = logging.getLogger(__name__)

PAGE_SIZE = 20  # the API rejects anything larger
MAX_POSTINGS = 5000  # runaway guard; Workday itself reports at most 2,000


def _site_url(company: Company) -> str:
    tenant, _, site = company.slug.partition("/")
    return f"https://{tenant}.{company.datacenter}.myworkdayjobs.com/{site}"


def _api_url(company: Company) -> str:
    tenant, _, site = company.slug.partition("/")
    return f"https://{tenant}.{company.datacenter}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def _is_remote(location: str) -> bool | None:
    return True if "remote" in location.lower() else None


def normalize(company: Company, posting: dict, info: dict | None = None) -> Job:
    """Build a Job from a listing entry, plus its detail record when we fetched one."""
    path = posting["externalPath"]
    location = posting.get("locationsText") or ""
    body, posted_at = "", None
    if info:
        places = [info.get("location"), *(info.get("additionalLocations") or [])]
        location = "; ".join(p for p in places if p) or location
        body = to_text(info.get("jobDescription"))
        posted_at = info.get("startDate")
    return Job(
        source="workday",
        company=company.name,
        company_slug=company.slug,
        external_id=path.rsplit("/", 1)[-1],
        title=posting.get("title", ""),
        location=location,
        remote=_is_remote(location),
        url=_site_url(company) + path,
        body=body,
        posted_at=posted_at,
    )


def _page(company: Company, client: httpx.Client, offset: int) -> tuple[list[dict], int]:
    """One listing page, and the total the API reports (only the first page has it)."""
    query = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
    resp = client.post(f"{_api_url(company)}/jobs", json=query)
    resp.raise_for_status()
    data = resp.json()
    return data.get("jobPostings") or [], int(data.get("total") or 0)


def _list(
    company: Company,
    client: httpx.Client,
    max_pages: int | None = None,
    pool: Executor | None = None,
) -> list[dict]:
    postings, total = _page(company, client, 0)
    if not postings:  # an empty page ends the listing, whatever the total says
        return postings
    offsets = range(PAGE_SIZE, min(total, MAX_POSTINGS), PAGE_SIZE)
    if max_pages is not None:
        offsets = offsets[: max_pages - 1]
    if pool is not None:  # every offset is known now, so the rest can go at once
        for page in pool.map(lambda offset: _page(company, client, offset)[0], offsets):
            if not page:  # as in the serial loop; leaving map() cancels the pages not yet started
                break
            postings += page
        return postings
    for offset in offsets:
        page, _ = _page(company, client, offset)
        if not page:
            break
        postings += page
    return postings


def _detail(company: Company, client: httpx.Client, path: str) -> dict | None:
    try:
        resp = client.get(_api_url(company) + path)
        resp.raise_for_status()
        return resp.json().get("jobPostingInfo") or None
    except (httpx.HTTPError, ValueError) as e:
        posting_id = path.rsplit("/", 1)[-1]
        log.warning("workday %s: no description for %s (%s)", company.slug, posting_id, e)
        return None


def fetch(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool] = lambda job: True,
    max_pages: int | None = None,
    pool: Executor | None = None,
) -> list[Job]:
    """All postings; with ``pool``, later pages and descriptions are fetched concurrently."""
    listed: dict[str, tuple[dict, Job]] = {}
    for posting in with_ids(company, _list(company, client, max_pages, pool), "externalPath"):
        job = normalize(company, posting)
        listed.setdefault(job.external_id, (posting, job))  # postings can shift between pages
    wanted = [posting for posting, job in listed.values() if wants_body(job)]
    paths = [posting["externalPath"] for posting in wanted]
    run = pool.map if pool is not None else map
    infos = run(lambda path: _detail(company, client, path), paths)
    for posting, info in zip(wanted, infos, strict=True):
        if info:
            job = normalize(company, posting, info)
            listed[job.external_id] = (posting, job)
    log.info("workday %s: %d jobs", company.slug, len(listed))
    return [job for _, job in listed.values()]
