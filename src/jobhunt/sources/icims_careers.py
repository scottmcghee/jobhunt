"""iCIMS Career Sites (formerly Jibe): the branded careers sites in front of iCIMS.

Not the classic ``careers-<company>.icims.com`` portals, whose robots.txt disallows everything.
These sites (careers.amd.com, jobs.aon.com, careers.keysight.com, ...) load their postings from
one public JSON endpoint on their own host, descriptions included:

    GET https://{host}/api/jobs?page=N&limit=100      N from 1; the reply has totalCount

A board is ``ats: icims_careers`` with ``slug:`` the site's host. A posting's page is
``https://{host}/jobs/{id}``, which the site redirects to its own path (``/careers-home/jobs/...``).
The id is the posting's ``req_id``; postings a site pulls from another iCIMS portal may have none,
and then their ``slug`` (the same number, which their page links by) stands in.

Their robots.txt allows everything but asks for a 5-second crawl delay, so each site is its own
rate group, capped at 0.2 requests a second (a ``fetch.max_rate`` entry for the host overrides it).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids
from jobhunt.sources._rate import RATE

log = logging.getLogger(__name__)

PAGE_SIZE = 100  # the most the API returns at once
MAX_PAGES = 100  # runaway guard: 10,000 postings
CRAWL_RATE = 0.2  # requests a second: robots.txt's "crawl-delay: 5"


def _posted(value: object) -> str | None:
    """'2026-07-20T20:14:00+0000' as ISO 8601 in UTC, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").astimezone(UTC).isoformat()
    except ValueError:
        return None


def normalize(company: Company, data: dict) -> Job:
    job_id = str(data["req_id"])  # the requisition id (or its stand-in), which /jobs/<id> links by
    location = data.get("full_location") or data.get("short_location") or ""
    title = data.get("title", "")
    parts = (data.get(k) for k in ("description", "responsibilities", "qualifications"))
    return Job(
        source="icims_careers",
        company=company.name,
        company_slug=company.slug,
        external_id=job_id,
        title=title,
        location=location,
        remote=True if "remote" in f"{location} {title}".lower() else None,
        url=f"https://{company.slug.lower()}/jobs/{job_id}",
        body="\n\n".join(text for p in parts if (text := to_text(p))),
        posted_at=_posted(data.get("posted_date")),
    )


def _page(company: Company, client: httpx.Client, page: int) -> tuple[list[dict], int]:
    resp = client.get(
        f"https://{company.slug.lower()}/api/jobs",
        params={"page": page, "limit": PAGE_SIZE},
        extensions={RATE: CRAWL_RATE},
    )
    resp.raise_for_status()
    body = resp.json()
    rows = [j.get("data") for j in body.get("jobs") or [] if isinstance(j, dict)]
    return [r for r in rows if isinstance(r, dict)], int(body.get("totalCount") or 0)


def fetch(company: Company, client: httpx.Client) -> list[Job]:
    """Every posting on the site, page by page until the reported total."""
    rows, total = _page(company, client, 1)
    page = 1
    while rows and len(rows) < total:
        if page == MAX_PAGES:
            log.warning(
                "icims_careers %s: kept the first %d of %d postings", company.slug, len(rows), total
            )
            break
        page += 1
        more, _ = _page(company, client, page)
        if not more:  # an empty page ends the listing, whatever the total says
            break
        rows += more
    rows = [{**r, "req_id": r.get("req_id") or r.get("slug")} for r in rows]
    jobs: dict[str, Job] = {}
    for data in with_ids(company, rows, "req_id"):
        job = normalize(company, data)
        jobs.setdefault(job.external_id, job)  # postings can shift between pages
    log.info("icims_careers %s: %d jobs", company.slug, len(jobs))
    return list(jobs.values())
