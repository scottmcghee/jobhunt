"""ATS source adapters. Each exposes ``fetch(company, client) -> list[Job]``.

A source whose successful fetch leaves postings out (a cap, an empty page before the reported
total, a sitemap skipped or ruled out by robots.txt) says so with ``_partial.note``, so
``listing_is_complete`` doesn't take the postings it missed as closed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from concurrent.futures import Executor

import httpx

from jobhunt.schema import ATSName, Company, Job
from jobhunt.sources import (
    _partial,
    amazon,
    apple,
    ashby,
    bamboohr,
    eightfold,
    gem,
    greenhouse,
    icims_careers,
    lever,
    oracle,
    paradox,
    phenom,
    radancy,
    rippling,
    smartrecruiters,
    successfactors,
    usajobs,
    workable,
    workday,
)

Fetcher = Callable[[Company, httpx.Client], list[Job]]
BodyCheck = Callable[[Job], bool]

FETCHERS: dict[ATSName, Fetcher] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workable": workable.fetch,
    "icims_careers": icims_careers.fetch,
    "gem": gem.fetch,
}

# Sources whose listings lack descriptions, so each description costs a request.
PagedFetcher = Callable[[Company, httpx.Client, BodyCheck, int | None, Executor | None], list[Job]]
ON_DEMAND_FETCHERS: dict[ATSName, PagedFetcher] = {
    "workday": workday.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "bamboohr": bamboohr.fetch,
    "successfactors": successfactors.fetch,
    "radancy": radancy.fetch,
    "paradox": paradox.fetch,
    "rippling": rippling.fetch,
}


# Single-company sites too big to list in full: they run one search per term instead.
SearchFetcher = Callable[
    [Company, httpx.Client, Sequence[str], int | None, BodyCheck, Executor | None, int | None],
    list[Job],
]
SEARCH_FETCHERS: dict[ATSName, SearchFetcher] = {
    "amazon": amazon.fetch,
    "eightfold": eightfold.fetch,
    "oracle": oracle.fetch,
    "apple": apple.fetch,
    "phenom": phenom.fetch,
    "usajobs": usajobs.fetch,  # also needs ``usajobs_auth``; see fetch_company
}


def listing_is_complete(company: Company, jobs: Sequence[Job]) -> bool:
    """Whether a successful fetch of the board listed every posting it has, so a recorded job it
    left out has closed. Call it once per fetch: it consumes the board's note.

    Not for search sources (a job a search didn't return may still be open), nor for a fetch the
    source noted as partial (``_partial.note``: a cap, an empty page before the reported total, a
    sitemap skipped or ruled out by robots.txt), nor for an empty listing, which may be a glitch
    (SmartRecruiters answers an unknown board with no postings): an emptied board's jobs close
    only when the board is pruned, or by hand.
    """
    partial = _partial.take(company.key)
    return company.ats not in SEARCH_FETCHERS and partial is None and bool(jobs)


def fetch_company(
    company: Company,
    client: httpx.Client,
    wants_body: BodyCheck = lambda job: True,
    max_pages: int | None = None,
    pool: Executor | None = None,
    search: Sequence[str] = (),
    max_per_term: int | None = None,
    usajobs_auth: tuple[str, str] | None = None,
) -> list[Job]:
    """Dispatch to the right ATS adapter for this company.

    ``wants_body`` matters only where descriptions cost a request each (Workday, SmartRecruiters,
    BambooHR, Eightfold, Oracle, Apple, Phenom, Radancy, Paradox, Rippling, and SuccessFactors
    sites without a feed):
    those postings get a description only if it returns True. Other sources always include
    descriptions.

    ``max_pages`` stops paged listings (Workday, SmartRecruiters, and each search of a search
    source) early. iCIMS Career Sites is paged too but ignores it: it reads every page, up to its
    own 100-page guard. The others are one request.

    ``search`` matters only for sites too big to list (Amazon, Eightfold, Oracle, Apple, Phenom,
    USAJOBS): they search per term instead (``fetch`` passes the title filter's target-level
    words). Others ignore it. ``max_per_term`` caps how many postings one term may bring in (None:
    the source's own default; fetch passes fetch.max_per_term).
    ``usajobs_auth`` is the (key, email) pair, handed to USAJOBS boards only; without it they are
    skipped with a warning.
    With ``pool``, Workday, SmartRecruiters, BambooHR, Eightfold, Oracle, Apple, Phenom, Radancy,
    Paradox, Rippling and SuccessFactors fetch later pages and descriptions concurrently on it
    (BambooHR, Oracle, Apple, Phenom, Radancy, Paradox, Rippling and SuccessFactors only
    descriptions).
    """
    _partial.take(company.key)  # a note from an earlier fetch that failed isn't this one's
    if company.ats == "usajobs":  # the one source with credentials (key, email); no one else's
        return usajobs.fetch(
            company, client, search, max_pages, wants_body, pool, max_per_term, auth=usajobs_auth
        )
    if company.ats in SEARCH_FETCHERS:
        fetcher = SEARCH_FETCHERS[company.ats]
        return fetcher(company, client, search, max_pages, wants_body, pool, max_per_term)
    if company.ats in ON_DEMAND_FETCHERS:
        return ON_DEMAND_FETCHERS[company.ats](company, client, wants_body, max_pages, pool)
    return FETCHERS[company.ats](company, client)


# Requests are rate-limited per group: one per Workday datacenter (its tenants share
# infrastructure), and one per API host for the other sources (every board is on that host;
# BambooHR and Eightfold give each tenant its own subdomain, but each is one service, so one
# group: 20 *.eightfold.ai boards fetched side by side were answered 405).
_API_HOSTS: dict[str, str] = {
    "boards-api.greenhouse.io": "greenhouse",
    "api.lever.co": "lever",
    "api.ashbyhq.com": "ashby",
    "api.gem.com": "gem",
    "api.rippling.com": "rippling",
    "api.smartrecruiters.com": "smartrecruiters",
    "apply.workable.com": "workable",
    "www.amazon.jobs": "amazon",
    "jobs.apple.com": "apple",
    "data.usajobs.gov": "usajobs",
}
_WORKDAY_HOST = re.compile(r"[a-z0-9-]+\.(wd\d+)\.myworkdayjobs\.com")
_BAMBOOHR_HOST = re.compile(r"[a-z0-9-]+\.bamboohr\.com")
_EIGHTFOLD_HOST = re.compile(r"[a-z0-9-]+\.eightfold\.ai")


def rate_group(company: Company) -> str:
    """The rate-limit group a board's requests belong to, e.g. ``workday:wd5`` or ``lever``."""
    if company.ats == "workday":
        return f"workday:{company.datacenter}"
    if company.ats == "eightfold" and _EIGHTFOLD_HOST.fullmatch(company.slug.lower()):
        return "eightfold"
    if company.ats in ("eightfold", "successfactors", "radancy", "paradox", "icims_careers"):
        # a site's own host
        return company.slug.lower()
    if company.ats in ("oracle", "phenom"):  # each tenant has its own host (and maybe sites)
        return company.slug.partition("/")[0].lower()
    return company.ats


def request_group(url: httpx.URL) -> str:
    """The rate-limit group of one request URL. Agrees with ``rate_group`` for every source,
    except a SuccessFactors board that moved to SAP's hosting: past robots.txt and the first
    sitemap hop, its requests fall in the SAP host's group (``<site>.jobs.hr.cloud.sap``)."""
    host = url.host.lower()
    if m := _WORKDAY_HOST.fullmatch(host):
        return f"workday:{m.group(1)}"
    if _BAMBOOHR_HOST.fullmatch(host):
        return "bamboohr"
    if _EIGHTFOLD_HOST.fullmatch(host):
        return "eightfold"
    return _API_HOSTS.get(host, host)
