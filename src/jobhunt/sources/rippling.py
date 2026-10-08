"""Rippling ATS job boards (ats.rippling.com/<slug>).

The board pages load their postings from Rippling's public board API:

    GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs           every posting
    GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}    one posting

The listing has titles, locations and links but no descriptions, so a description costs one
request; ``fetch`` asks only for postings that pass ``wants_body``. A board is ``ats: rippling``
with ``slug:`` the name in ``ats.rippling.com/<slug>``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, datetime

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids

log = logging.getLogger(__name__)

BASE = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"


def _label(value: object) -> str:
    return str(value.get("label") or "") if isinstance(value, dict) else ""


def _posted(value: object) -> str | None:
    """'2026-01-27T15:24:57.958000-08:00' as ISO 8601 in UTC, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC).isoformat()
    except ValueError:
        return None


def normalize(
    company: Company, posting: dict, detail: dict | None = None, places: list[str] | None = None
) -> Job:
    """A Job from a listing entry (``places``: its locations, from every entry for it), plus its
    detail record when we fetched one."""
    title = str(posting.get("name") or "").strip()
    location = "; ".join(places) if places else _label(posting.get("workLocation"))
    body, posted_at = "", None
    if detail:
        if listed := [p for p in detail.get("workLocations") or [] if isinstance(p, str)]:
            location = "; ".join(listed)
        description = detail.get("description") or {}
        if isinstance(description, dict):  # role first: the company blurb is on every posting
            parts = (to_text(description.get(k)) for k in ("role", "company"))
            body = "\n\n".join(p for p in parts if p)
        posted_at = _posted(detail.get("createdOn"))
    uuid = str(posting["uuid"])
    return Job(
        source="rippling",
        company=company.name,
        company_slug=company.slug,
        external_id=uuid,
        title=title,
        location=location,
        remote=True if "remote" in f"{location} {title}".lower() else None,
        url=posting.get("url") or f"https://ats.rippling.com/{company.slug}/jobs/{uuid}",
        body=body,
        posted_at=posted_at,
    )


def _detail(company: Company, client: httpx.Client, uuid: str) -> dict | None:
    try:
        resp = client.get(f"{BASE.format(slug=company.slug)}/{uuid}")
        resp.raise_for_status()
        detail = resp.json()
        return detail if isinstance(detail, dict) else None
    except (httpx.HTTPError, ValueError) as e:
        error = " ".join(str(e).split())
        log.warning("rippling %s: no description for %s (%s)", company.slug, uuid, error)
        return None


def fetch(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool] = lambda job: True,
    max_pages: int | None = None,  # unused: the listing is one request
    pool: Executor | None = None,
) -> list[Job]:
    """Every posting; descriptions for those that pass ``wants_body``, on ``pool`` if given."""
    resp = client.get(BASE.format(slug=company.slug))
    resp.raise_for_status()
    listing = resp.json()
    # a posting is listed once for each of its locations
    places: dict[str, list[str]] = {}
    first: dict[str, dict] = {}
    for p in with_ids(company, listing if isinstance(listing, list) else [], "uuid"):
        uuid = str(p["uuid"])
        first.setdefault(uuid, p)
        labels = places.setdefault(uuid, [])
        if (label := _label(p.get("workLocation"))) and label not in labels:
            labels.append(label)
    jobs = {u: (p, normalize(company, p, places=places[u])) for u, p in first.items()}
    wanted = [p for p, job in jobs.values() if wants_body(job)]
    run = pool.map if pool is not None else map
    details = run(lambda p: _detail(company, client, str(p["uuid"])), wanted)
    for posting, detail in zip(wanted, details, strict=True):
        if detail:
            uuid = str(posting["uuid"])
            jobs[uuid] = (posting, normalize(company, posting, detail, places[uuid]))
    log.info("rippling %s: %d jobs", company.slug, len(jobs))
    return [job for _, job in jobs.values()]
