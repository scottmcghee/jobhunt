"""Oracle Recruiting Cloud careers sites (Oracle Fusion HCM "Candidate Experience").

Endpoints (public, no auth; the ones each careers site calls; the hosts serve no robots.txt):

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},keyword={term},limit=200,offset=N
    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?onlyData=true&expand=all&finder=ById;Id="{id}",siteNumber={site}

A board is ``ats: oracle`` with ``slug: {host}/{site}``, read off a careers URL like
``https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/123``. The site only shapes the job
URL: the search returns the host's postings whatever the site (even a made-up one), so keep one
board per host, or every posting is fetched, scored and written up once per board.

Keyword search matches descriptions too, so a broad term can return thousands: ``fetch`` searches
once per term, 200 a page, up to ``MAX_PER_TERM`` with a warning, and asks for a description only
for postings that pass ``wants_body``. Most postings leave the workplace type blank; that is
unknown, not on-site, so the location filter decides (it reads the body).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import Executor

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids
from jobhunt.sources._search import terms

log = logging.getLogger(__name__)

PAGE_SIZE = 200  # the most the API returns per page
MAX_PER_TERM = 1000  # 5 requests; a runaway guard for broad terms
_REMOTE = {"ORA_REMOTE": True, "ORA_ON_SITE": False, "ORA_HYBRID": False}
_BODY = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")


def _board(company: Company) -> tuple[str, str]:
    host, _, site = company.slug.partition("/")
    return host.lower(), site


def _api(company: Company) -> str:
    return f"https://{_board(company)[0]}/hcmRestApi/resources/latest"


def _location(raw: dict) -> str:
    places = [raw.get("PrimaryLocation")]
    secondary = raw.get("secondaryLocations") or []
    places += [loc.get("Name") for loc in secondary if isinstance(loc, dict)]
    return "; ".join(dict.fromkeys(p.strip() for p in places if isinstance(p, str) and p.strip()))


def _remote(raw: dict, location: str) -> bool | None:
    """The code if there is one; else True if the location or title says remote; else None.

    A blank code is unknown, not on-site: the location filter decides, and it reads the body.
    """
    if (remote := _REMOTE.get(raw.get("WorkplaceTypeCode") or "")) is not None:
        return remote
    return True if "remote" in f"{location} {raw.get('Title', '')}".lower() else None


def normalize(company: Company, raw: dict, detail: dict | None = None) -> Job:
    host, site = _board(company)
    location = _location(raw)
    body = "\n\n".join(t for t in (to_text((detail or {}).get(k)) for k in _BODY) if t)
    return Job(
        source="oracle",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["Id"]),
        title=raw.get("Title", ""),
        location=location,
        remote=_remote(raw, location),
        url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{raw['Id']}",
        body=body,
        posted_at=raw.get("PostedDate"),
    )


def _page(company: Company, client: httpx.Client, term: str, offset: int) -> tuple[list[dict], int]:
    site = _board(company)[1]
    finder = f"findReqs;siteNumber={site},keyword={term},limit={PAGE_SIZE},offset={offset}"
    params = {"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder}
    resp = client.get(f"{_api(company)}/recruitingCEJobRequisitions", params=params)
    resp.raise_for_status()
    search = (resp.json().get("items") or [{}])[0]
    return search.get("requisitionList") or [], int(search.get("TotalJobsCount") or 0)


def _detail(company: Company, client: httpx.Client, posting_id: str) -> dict | None:
    site = _board(company)[1]
    finder = f'ById;Id="{posting_id}",siteNumber={site}'
    params = {"onlyData": "true", "expand": "all", "finder": finder}
    try:
        resp = client.get(f"{_api(company)}/recruitingCEJobRequisitionDetails", params=params)
        resp.raise_for_status()
        return (resp.json().get("items") or [None])[0]
    except (httpx.HTTPError, ValueError) as e:
        error = " ".join(str(e).split())  # httpx's messages can span lines
        log.warning("oracle %s: no description for %s (%s)", company.slug, posting_id, error)
        return None


def fetch(
    company: Company,
    client: httpx.Client,
    search: Iterable[str] = (),
    max_pages: int | None = None,
    wants_body: Callable[[Job], bool] = lambda job: True,
    pool: Executor | None = None,
) -> list[Job]:
    """Every posting any search term finds; descriptions for those ``wants_body`` accepts."""
    found: dict[str, tuple[dict, Job]] = {}
    for term in terms(search, source="oracle"):
        offset = pages = 0
        while offset < MAX_PER_TERM and (max_pages is None or pages < max_pages):
            reqs, total = _page(company, client, term, offset)
            pages += 1
            for raw in with_ids(company, reqs, field="Id"):
                job = normalize(company, raw)
                found.setdefault(job.external_id, (raw, job))
            offset += len(reqs)
            if not reqs or offset >= total:
                break
        else:  # the loop's own condition stopped it: the cap, or max_pages
            if offset >= MAX_PER_TERM:
                log.warning(
                    "oracle %s: %r has %d hits; kept the first %d",
                    company.slug, term, total, MAX_PER_TERM,
                )
    wanted = [(raw, job) for raw, job in found.values() if wants_body(job)]
    run = pool.map if pool is not None else map
    details = run(lambda pair: _detail(company, client, pair[1].external_id), wanted)
    for (raw, job), detail in zip(wanted, details, strict=True):
        if detail:
            found[job.external_id] = (raw, normalize(company, raw, detail))
    log.info("oracle %s: %d jobs", company.slug, len(found))
    return [job for _, job in found.values()]
