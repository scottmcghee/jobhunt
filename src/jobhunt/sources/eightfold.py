"""Eightfold careers sites, the platform behind Microsoft, Nvidia, Eaton, PayPal and others.

Endpoints (public, no auth; the ones each careers site calls; every Eightfold robots.txt seen
allows /careers and /api/pcsx):

    GET https://{host}/careers                       the careers page; it names the domain
    GET https://{host}/api/pcsx/search?domain=...&query=...&start=N[&location=...]
    GET https://{host}/api/pcsx/position_details?position_id=...&domain=...

Some tenants are still on Eightfold's older interface: there /api/pcsx answers 403 with JSON
``{"message": "PCSX is not enabled for this user."}`` and the same data comes from the older API,
which ``fetch`` switches to for the rest of the board's run. Any other 403 (a WAF or rate-limit
block, usually an HTML page) is raised as is:

    GET https://{host}/api/apply/v2/jobs?domain=...&query=...&start=N&num=10[&location=...]
    GET https://{host}/api/apply/v2/jobs/{id}?domain=...

A board is ``ats: eightfold`` with ``slug:`` the careers site's host, e.g. ``eaton.eightfold.ai``
or ``apply.careers.microsoft.com``, and optionally ``location:`` (e.g. ``United States``) to limit
its searches to one place. The API needs the company's ``domain`` (``eaton.com``), which the
careers page embeds; ``fetch`` reads it from there, one request per board per run.

These sites post thousands of roles, so ``fetch`` searches once per term, 10 results a page (the
API's fixed size), and asks for a description only for postings that pass ``wants_body``.
An unknown domain on a real host is a 404; an unknown ``*.eightfold.ai`` host doesn't resolve.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Iterable
from concurrent.futures import Executor
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids
from jobhunt.sources._search import terms

log = logging.getLogger(__name__)

PAGE_SIZE = 10  # fixed by the API; a larger num is ignored
MAX_PER_TERM = 500  # 50 requests; a runaway guard for broad terms
_DOMAIN = re.compile(r'"domain"\s*:\s*"([a-z0-9][a-z0-9.-]*\.[a-z]{2,})"', re.I)


def _domain(page: str) -> str | None:
    """The company domain a careers page embeds, as entity-escaped JSON or plain."""
    m = _DOMAIN.search(html.unescape(page))
    return m.group(1).lower() if m else None


def _remote(raw: dict) -> bool | None:
    option = (raw.get("workLocationOption") or "").lower()
    if option.startswith("remote"):  # remote, or Nvidia's remote_local
        return True
    return False if option in ("onsite", "hybrid") else None


def _posted(ts: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), UTC).isoformat()  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def normalize(company: Company, raw: dict, detail: dict | None = None) -> Job:
    locations = [str(loc) for loc in raw.get("locations") or [] if loc]
    path = raw.get("positionUrl") or f"/careers/job/{raw['id']}"
    return Job(
        source="eightfold",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=raw.get("name", ""),
        location="; ".join(dict.fromkeys(locations)),
        remote=_remote(raw),
        url=f"https://{company.slug.lower()}{path}",
        body=to_text((detail or {}).get("jobDescription")),
        posted_at=_posted(raw.get("postedTs")),
    )


def _careers_domain(company: Company, client: httpx.Client) -> str:
    resp = client.get(f"https://{company.slug.lower()}/careers")
    resp.raise_for_status()
    if domain := _domain(resp.text):
        return domain
    raise ValueError(f"no Eightfold domain on https://{company.slug}/careers")


def _pcsx_off(resp: httpx.Response) -> bool:
    """Whether a response is an older-interface tenant's 403 for /api/pcsx, not a block."""
    if resp.status_code != 403:
        return False
    try:
        message = resp.json().get("message")
    except (ValueError, AttributeError):  # not JSON, or not an object
        return False
    return isinstance(message, str) and "pcsx" in message.lower()


class _Board:
    """One board's API, for one run: the current one, or the older one once PCSX said it is off."""

    def __init__(self, company: Company, client: httpx.Client, domain: str):
        self.company, self.client, self.domain = company, client, domain
        self.base = f"https://{company.slug.lower()}"
        self.older = False

    def page(self, term: str, start: int) -> tuple[list[dict], int]:
        params: dict[str, str | int] = {"domain": self.domain, "query": term, "start": start}
        if self.company.location:
            params["location"] = self.company.location
        if not self.older:
            resp = self.client.get(f"{self.base}/api/pcsx/search", params=params)
            if not _pcsx_off(resp):
                resp.raise_for_status()
                data = resp.json().get("data") or {}
                return data.get("positions") or [], int(data.get("count") or 0)
            log.info("eightfold %s: /api/pcsx forbidden; using the older API", self.company.slug)
            self.older = True
        older = {**params, "num": PAGE_SIZE}
        resp = self.client.get(f"{self.base}/api/apply/v2/jobs", params=older)
        resp.raise_for_status()
        data = resp.json()
        rows = [_from_older(row) for row in data.get("positions") or []]
        return rows, int(data.get("count") or 0)

    def detail(self, posting_id: str) -> dict | None:
        try:
            if self.older:
                resp = self.client.get(
                    f"{self.base}/api/apply/v2/jobs/{posting_id}", params={"domain": self.domain}
                )
                resp.raise_for_status()
                return {"jobDescription": resp.json().get("job_description")}
            resp = self.client.get(
                f"{self.base}/api/pcsx/position_details",
                params={"position_id": posting_id, "domain": self.domain, "hl": "en"},
            )
            resp.raise_for_status()
            return resp.json().get("data") or None
        except (httpx.HTTPError, ValueError) as e:
            error = " ".join(str(e).split())  # httpx's messages can span lines
            slug = self.company.slug
            log.warning("eightfold %s: no description for %s (%s)", slug, posting_id, error)
            return None


def _from_older(row: dict) -> dict:
    """An older-API listing row, in the current API's shape (the fields ``normalize`` reads)."""
    locations = row.get("locations") or ([row["location"]] if row.get("location") else [])
    url = row.get("canonicalPositionUrl")
    return {
        "id": row.get("id"),
        "name": row.get("name") or row.get("posting_name") or "",
        "locations": locations,
        "postedTs": row.get("t_create"),
        "workLocationOption": row.get("work_location_option"),
        "positionUrl": urlsplit(url).path if url else None,
    }


def fetch(
    company: Company,
    client: httpx.Client,
    search: Iterable[str] = (),
    max_pages: int | None = None,
    wants_body: Callable[[Job], bool] = lambda job: True,
    pool: Executor | None = None,
) -> list[Job]:
    """Every posting any search term finds; descriptions for those ``wants_body`` accepts."""
    board = _Board(company, client, _careers_domain(company, client))
    found: dict[str, tuple[dict, Job]] = {}
    for term in terms(search, source="eightfold"):
        start = pages = 0
        while start < MAX_PER_TERM and (max_pages is None or pages < max_pages):
            positions, count = board.page(term, start)
            pages += 1
            for raw in with_ids(company, positions):
                job = normalize(company, raw)
                found.setdefault(job.external_id, (raw, job))
            start += len(positions)
            if not positions or start >= count:
                break
        else:  # the loop's own condition stopped it: the cap, or max_pages
            if start >= MAX_PER_TERM:
                log.warning(
                    "eightfold %s: %r has %d hits; kept the first %d",
                    company.slug, term, count, MAX_PER_TERM,
                )
    wanted = [(raw, job) for raw, job in found.values() if wants_body(job)]
    run = pool.map if pool is not None else map
    details = run(lambda pair: board.detail(pair[1].external_id), wanted)
    for (raw, job), detail in zip(wanted, details, strict=True):
        if detail:
            found[job.external_id] = (raw, normalize(company, raw, detail))
    log.info("eightfold %s: %d jobs", company.slug, len(found))
    return [job for _, job in found.values()]
