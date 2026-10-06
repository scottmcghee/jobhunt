"""Workable careers-page widget API.

Endpoint (public, no auth; the one each account's careers page and embed widget call):

    GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true

One request returns every published job with its description, so there are no detail requests
and no paging. An unknown account is a 404, so dead boards are pruned like any other.
"""

from __future__ import annotations

import logging

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids

log = logging.getLogger(__name__)

BASE = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


def _place(city: str | None, region: str | None, country: str | None) -> str:
    return ", ".join(part for part in (city, region, country) if part)


def _location(raw: dict) -> str:
    """Every listed location, e.g. "Austin, Texas, United States; Bellevue, Washington, ...". """
    listed = raw.get("locations") or []
    places = [_place(loc.get("city"), loc.get("region"), loc.get("country")) for loc in listed]
    if not any(places):
        places = [_place(raw.get("city"), raw.get("state"), raw.get("country"))]
    return "; ".join(dict.fromkeys(p for p in places if p))


def _is_remote(raw: dict, location: str) -> bool | None:
    if raw.get("telecommuting"):
        return True
    # telecommuting is false for on-site and hybrid alike, so false says nothing more
    return True if "remote" in f"{location} {raw.get('title', '')}".lower() else None


def normalize(company: Company, raw: dict) -> Job:
    location = _location(raw)
    return Job(
        source="workable",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["shortcode"]),
        title=raw.get("title", ""),
        location=location,
        remote=_is_remote(raw, location),
        url=raw.get("url") or f"https://apply.workable.com/{company.slug}/j/{raw['shortcode']}",
        body=to_text(raw.get("description")),
        posted_at=raw.get("published_on") or raw.get("created_at"),
    )


def fetch(company: Company, client: httpx.Client) -> list[Job]:
    resp = client.get(BASE.format(slug=company.slug), params={"details": "true"})
    resp.raise_for_status()
    postings = with_ids(company, resp.json().get("jobs") or [], field="shortcode")
    jobs = [normalize(company, raw) for raw in postings]
    log.info("workable %s: %d jobs", company.slug, len(jobs))
    return jobs
