"""Ashby public job board API.

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
"""

from __future__ import annotations

import logging

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text

log = logging.getLogger(__name__)

BASE = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def normalize(company: Company, raw: dict) -> Job:
    location = raw.get("location", "") or ""
    secondary = [s.get("location", "") for s in raw.get("secondaryLocations", []) or []]
    all_locs = ", ".join(x for x in [location, *secondary] if x)
    return Job(
        source="ashby",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=raw.get("title", ""),
        location=all_locs,
        remote=bool(raw.get("isRemote")) if "isRemote" in raw else None,
        url=raw.get("jobUrl") or raw.get("applyUrl", ""),
        body=to_text(raw.get("descriptionHtml")) or (raw.get("descriptionPlain") or ""),
        posted_at=raw.get("publishedAt"),
    )


def fetch(company: Company, client: httpx.Client) -> list[Job]:
    url = BASE.format(slug=company.slug)
    resp = client.get(url, params={"includeCompensation": "true"})
    resp.raise_for_status()
    data = resp.json()
    jobs = [normalize(company, j) for j in data.get("jobs", []) if j.get("isListed", True)]
    log.info("ashby %s: %d jobs", company.slug, len(jobs))
    return jobs
