"""BambooHR careers-site API.

Endpoints (public, no auth; the ones each tenant's careers page calls):

    GET https://{tenant}.bamboohr.com/careers/list
    GET https://{tenant}.bamboohr.com/careers/{id}/detail

The listing has no descriptions, so a description costs one request per posting. ``fetch`` takes
a ``wants_body`` check and pays that cost only for postings that pass it, as for Workday.

An unknown tenant isn't a 404: it redirects (302) to www.bamboohr.com. Requests here never follow
redirects, whatever the client's setting, so that 302 is an error from ``raise_for_status`` like any
other status (not bamboohr.com's home page read as JSON); ``cli._board_gone`` counts it toward
pruning.
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

BASE = "https://{slug}.bamboohr.com/careers"
# locationType: on-site and hybrid both mean "not remote" here, as for SmartRecruiters.
_REMOTE_BY_LOCATION_TYPE = {"0": False, "1": True, "2": False}


def _is_remote(raw: dict) -> bool | None:
    remote = _REMOTE_BY_LOCATION_TYPE.get(str(raw.get("locationType")))
    if remote is not None:
        return remote
    return True if raw.get("isRemote") else None


def _location(raw: dict) -> str:
    """The office's city and state, else the remote region (atsLocation), else "Remote"."""
    office = raw.get("location") or {}
    region = raw.get("atsLocation") or {}
    parts = [office.get("city"), office.get("state")]
    if not any(parts):
        state = region.get("state") or region.get("province")
        parts = [region.get("city"), state, region.get("country")]
    text = ", ".join(p.strip() for p in parts if p and p.strip())
    return text or ("Remote" if _is_remote(raw) else "")


def _body(opening: dict) -> str:
    body = to_text(opening.get("description"))
    if pay := (opening.get("compensation") or "").strip():
        body = f"{body}\n\nCompensation: {pay}" if body else f"Compensation: {pay}"
    return body


def normalize(company: Company, raw: dict, opening: dict | None = None) -> Job:
    """Build a Job from a listing entry, plus its detail record when we fetched one."""
    return Job(
        source="bamboohr",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=raw.get("jobOpeningName", ""),
        location=_location(raw),
        remote=_is_remote(raw),
        url=f"{BASE.format(slug=company.slug)}/{raw['id']}",
        body=_body(opening) if opening else "",
        posted_at=(opening or {}).get("datePosted"),
    )


def _detail(company: Company, client: httpx.Client, posting_id: str) -> dict | None:
    try:
        url = f"{BASE.format(slug=company.slug)}/{posting_id}/detail"
        resp = client.get(url, follow_redirects=False)
        resp.raise_for_status()
        return (resp.json().get("result") or {}).get("jobOpening") or None
    except (httpx.HTTPError, ValueError) as e:
        error = " ".join(str(e).split())  # httpx's messages can span lines
        log.warning("bamboohr %s: no description for %s (%s)", company.slug, posting_id, error)
        return None


def fetch(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool] = lambda job: True,
    max_pages: int | None = None,  # the listing is one request; accepted for a common signature
    pool: Executor | None = None,
) -> list[Job]:
    """All postings; with ``pool``, descriptions are fetched concurrently."""
    resp = client.get(f"{BASE.format(slug=company.slug)}/list", follow_redirects=False)
    resp.raise_for_status()
    postings = with_ids(company, resp.json().get("result") or [])
    jobs = [normalize(company, raw) for raw in postings]
    wanted = [i for i, job in enumerate(jobs) if wants_body(job)]
    run = pool.map if pool is not None else map
    details = run(lambda i: _detail(company, client, jobs[i].external_id), wanted)
    for i, opening in zip(wanted, details, strict=True):
        if opening:
            jobs[i] = normalize(company, postings[i], opening)
    log.info("bamboohr %s: %d jobs", company.slug, len(jobs))
    return jobs
