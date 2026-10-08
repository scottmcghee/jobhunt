"""Gem Job Board API.

Docs: https://help.gem.com/databases/gem-help-center/the-job-board-api
Endpoint: GET https://api.gem.com/job_board/v0/{slug}/job_posts/

One request lists every published post with its description, in much the shape of Greenhouse's
Job Board API. A board is ``ats: gem`` with ``slug:`` the name in ``jobs.gem.com/<slug>``.
"""

from __future__ import annotations

import logging

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids

log = logging.getLogger(__name__)

BASE = "https://api.gem.com/job_board/v0/{slug}/job_posts/"


def _is_remote(raw: dict, location: str) -> bool | None:
    """From Gem's ``location_type`` when it says, else from the location and title."""
    kind = str(raw.get("location_type") or "").lower()
    if kind == "remote":
        return True
    if kind == "hybrid":
        return False
    return True if "remote" in f"{location} {raw.get('title', '')}".lower() else None


def normalize(company: Company, raw: dict) -> Job:
    location = (raw.get("location") or {}).get("name", "") or ""
    return Job(
        source="gem",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=raw.get("title", ""),
        location=location,
        remote=_is_remote(raw, location),
        url=raw.get("absolute_url") or f"https://jobs.gem.com/{company.slug}/{raw['id']}",
        body=to_text(raw.get("content")),
        posted_at=raw.get("first_published_at") or raw.get("updated_at"),
    )


def fetch(company: Company, client: httpx.Client) -> list[Job]:
    resp = client.get(BASE.format(slug=company.slug))
    resp.raise_for_status()
    posts = resp.json()
    posts = posts if isinstance(posts, list) else []
    jobs = [normalize(company, p) for p in with_ids(company, posts)]
    log.info("gem %s: %d jobs", company.slug, len(jobs))
    return jobs
