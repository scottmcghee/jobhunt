"""SmartRecruiters Posting API.

Docs: https://developers.smartrecruiters.com/docs/posting-api
Endpoints (public, no auth; the company identifier is case-insensitive):

    GET https://api.smartrecruiters.com/v1/companies/{identifier}/postings?limit=100&offset=N
    GET https://api.smartrecruiters.com/v1/companies/{identifier}/postings/{id}

The listing has no descriptions and pages 100 at a time, so a description costs one request per
posting. ``fetch`` takes a ``wants_body`` check and pays that cost only for postings that pass it.

An unknown identifier is not a 404: it returns 200 with no postings, the same as a company with no
open roles. So dead boards here are never pruned automatically; ``fetch`` warns instead.
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

BASE = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
PAGE_SIZE = 100  # the API clamps anything larger
MAX_POSTINGS = 10000  # runaway guard
SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def _is_remote(location: dict) -> bool | None:
    if location.get("remote"):
        return True
    if location.get("hybrid"):
        return False
    # remote and hybrid both default to false, so on-site and unset look the same
    return True if "remote" in (location.get("fullLocation") or "").lower() else None


def _body(detail: dict) -> str:
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    parts = [to_text((sections.get(name) or {}).get("text")) for name in SECTIONS]
    return "\n\n".join(p for p in parts if p)


def normalize(company: Company, posting: dict, detail: dict | None = None) -> Job:
    """Build a Job from a listing entry, plus its detail record when we fetched one."""
    location = posting.get("location") or {}
    return Job(
        source="smartrecruiters",
        company=company.name,
        company_slug=company.slug,
        external_id=str(posting["id"]),
        title=posting.get("name", ""),
        location=location.get("fullLocation") or "",
        remote=_is_remote(location),
        url=f"https://jobs.smartrecruiters.com/{company.slug}/{posting['id']}",
        body=_body(detail) if detail else "",
        posted_at=posting.get("releasedDate"),
    )


def _page(company: Company, client: httpx.Client, offset: int) -> tuple[list[dict], int]:
    """One listing page, and the total it reports (every page has it)."""
    resp = client.get(BASE.format(slug=company.slug), params={"limit": PAGE_SIZE, "offset": offset})
    resp.raise_for_status()
    data = resp.json()
    return data.get("content") or [], int(data.get("totalFound") or 0)


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


def _detail(company: Company, client: httpx.Client, posting_id: str) -> dict | None:
    try:
        resp = client.get(f"{BASE.format(slug=company.slug)}/{posting_id}")
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("smartrecruiters %s: no description for %s (%s)", company.slug, posting_id, e)
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
    for posting in with_ids(company, _list(company, client, max_pages, pool)):
        job = normalize(company, posting)
        listed.setdefault(job.external_id, (posting, job))  # postings can shift between pages
    wanted = [posting for posting, job in listed.values() if wants_body(job)]
    ids = [str(posting["id"]) for posting in wanted]
    run = pool.map if pool is not None else map
    details = run(lambda posting_id: _detail(company, client, posting_id), ids)
    for posting, detail in zip(wanted, details, strict=True):
        if detail:
            job = normalize(company, posting, detail)
            listed[job.external_id] = (posting, job)
    if not listed:
        log.warning("smartrecruiters %s: 0 postings — check the identifier", company.slug)
    log.info("smartrecruiters %s: %d jobs", company.slug, len(listed))
    return [job for _, job in listed.values()]
