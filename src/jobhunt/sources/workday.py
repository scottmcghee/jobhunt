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

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text

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


def _list(company: Company, client: httpx.Client) -> list[dict]:
    postings: list[dict] = []
    total: int | None = None
    offset = 0
    while True:
        query = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        resp = client.post(f"{_api_url(company)}/jobs", json=query)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("jobPostings") or []
        if total is None:
            total = min(int(data.get("total") or 0), MAX_POSTINGS)
        postings += page
        offset += PAGE_SIZE
        if not page or offset >= total:
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
) -> list[Job]:
    jobs: dict[str, Job] = {}
    for posting in _list(company, client):
        job = normalize(company, posting)
        if job.external_id in jobs:  # postings can shift between pages
            continue
        if wants_body(job) and (info := _detail(company, client, posting["externalPath"])):
            job = normalize(company, posting, info)
        jobs[job.external_id] = job
    log.info("workday %s: %d jobs", company.slug, len(jobs))
    return list(jobs.values())
