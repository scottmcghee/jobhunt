"""Lever Postings API.

Docs: https://github.com/lever/postings-api
Endpoint: GET https://api.lever.co/v0/postings/{slug}?mode=json
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text

log = logging.getLogger(__name__)

BASE = "https://api.lever.co/v0/postings/{slug}"


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _body(raw: dict) -> str:
    parts = [to_text(raw.get("description"))]
    for lst in raw.get("lists", []) or []:
        parts.append(lst.get("text", ""))
        parts.append(to_text(lst.get("content")))
    parts.append(to_text(raw.get("additional")))
    return "\n\n".join(p for p in parts if p)


def normalize(company: Company, raw: dict) -> Job:
    cats = raw.get("categories") or {}
    workplace = (raw.get("workplaceType") or "").lower()
    location = cats.get("location", "") or ""
    remote: bool | None
    if workplace == "remote":
        remote = True
    elif workplace in {"onsite", "on-site", "hybrid"}:
        remote = False
    else:
        remote = True if "remote" in location.lower() else None
    return Job(
        source="lever",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=raw.get("text", ""),
        location=location,
        remote=remote,
        url=raw.get("hostedUrl", ""),
        body=_body(raw),
        posted_at=_ms_to_iso(raw.get("createdAt")),
    )


def fetch(company: Company, client: httpx.Client) -> list[Job]:
    url = BASE.format(slug=company.slug)
    resp = client.get(url, params={"mode": "json"})
    resp.raise_for_status()
    data = resp.json()
    jobs = [normalize(company, j) for j in data]
    log.info("lever %s: %d jobs", company.slug, len(jobs))
    return jobs
