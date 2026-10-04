"""Greenhouse Job Board API.

Docs: https://developers.greenhouse.io/job-board.html
Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
"""

from __future__ import annotations

import logging

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids

log = logging.getLogger(__name__)

BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def _is_remote(raw: dict) -> bool | None:
    loc = (raw.get("location") or {}).get("name", "") or ""
    text = f"{loc} {raw.get('title', '')}".lower()
    if "remote" in text:
        return True
    return None


def normalize(company: Company, raw: dict) -> Job:
    return Job(
        source="greenhouse",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=raw.get("title", ""),
        location=(raw.get("location") or {}).get("name", "") or "",
        remote=_is_remote(raw),
        url=raw.get("absolute_url", ""),
        body=to_text(raw.get("content")),
        posted_at=raw.get("updated_at") or raw.get("first_published"),
    )


def fetch(company: Company, client: httpx.Client) -> list[Job]:
    url = BASE.format(slug=company.slug)
    resp = client.get(url, params={"content": "true"})
    resp.raise_for_status()
    data = resp.json()
    jobs = [normalize(company, j) for j in with_ids(company, data.get("jobs", []))]
    log.info("greenhouse %s: %d jobs", company.slug, len(jobs))
    return jobs
