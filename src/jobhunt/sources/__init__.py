"""ATS source adapters. Each exposes ``fetch(company, client) -> list[Job]``."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from concurrent.futures import Executor

import httpx

from jobhunt.schema import ATSName, Company, Job
from jobhunt.sources import (
    amazon,
    apple,
    ashby,
    bamboohr,
    eightfold,
    greenhouse,
    lever,
    oracle,
    paradox,
    phenom,
    radancy,
    smartrecruiters,
    successfactors,
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
}


def fetch_company(
    company: Company,
    client: httpx.Client,
    wants_body: BodyCheck = lambda job: True,
    max_pages: int | None = None,
    pool: Executor | None = None,
    search: Sequence[str] = (),
    max_per_term: int | None = None,
) -> list[Job]:
    """Dispatch to the right ATS adapter for this company.

    ``wants_body`` matters only where descriptions cost a request each (Workday, SmartRecruiters,
    BambooHR, Eightfold, Oracle, Apple, Phenom, Radancy, Paradox, and SuccessFactors sites without
    a feed):
    those postings get a description only if it returns True. Other sources always include
    descriptions.

    ``max_pages`` stops paged listings (Workday, SmartRecruiters, and each search of a search
    source) early; the others are one request.

    ``search`` matters only for sites too big to list (Amazon, Eightfold, Oracle, Apple, Phenom):
    they search per term instead (``fetch`` passes the title filter's target-level words). Others
    ignore it. ``max_per_term`` caps how many postings one term may bring in (None: the source's
    own default; fetch passes fetch.max_per_term).
    With ``pool``, Workday, SmartRecruiters, BambooHR, Eightfold, Oracle, Apple, Phenom, Radancy,
    Paradox and SuccessFactors fetch later pages and descriptions concurrently on it (BambooHR,
    Oracle, Apple, Phenom, Radancy, Paradox and SuccessFactors only descriptions).
    """
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
    "api.smartrecruiters.com": "smartrecruiters",
    "apply.workable.com": "workable",
    "www.amazon.jobs": "amazon",
    "jobs.apple.com": "apple",
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
    if company.ats in ("eightfold", "successfactors", "radancy", "paradox"):  # a site's own host
        return company.slug.lower()
    if company.ats in ("oracle", "phenom"):  # each tenant has its own host (and maybe sites)
        return company.slug.partition("/")[0].lower()
    return company.ats


def request_group(url: httpx.URL) -> str:
    """The rate-limit group of one request URL. Agrees with ``rate_group`` for every source."""
    host = url.host.lower()
    if m := _WORKDAY_HOST.fullmatch(host):
        return f"workday:{m.group(1)}"
    if _BAMBOOHR_HOST.fullmatch(host):
        return "bamboohr"
    if _EIGHTFOLD_HOST.fullmatch(host):
        return "eightfold"
    return _API_HOSTS.get(host, host)
