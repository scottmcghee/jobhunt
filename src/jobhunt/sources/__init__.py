"""ATS source adapters. Each exposes ``fetch(company, client) -> list[Job]``."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from concurrent.futures import Executor

import httpx

from jobhunt.schema import ATSName, Company, Job
from jobhunt.sources import (
    amazon,
    ashby,
    bamboohr,
    eightfold,
    greenhouse,
    lever,
    smartrecruiters,
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
}


# Single-company sites too big to list in full: they run one search per term instead.
SearchFetcher = Callable[
    [Company, httpx.Client, Sequence[str], int | None, BodyCheck, Executor | None], list[Job]
]
SEARCH_FETCHERS: dict[ATSName, SearchFetcher] = {
    "amazon": amazon.fetch,
    "eightfold": eightfold.fetch,
}


def fetch_company(
    company: Company,
    client: httpx.Client,
    wants_body: BodyCheck = lambda job: True,
    max_pages: int | None = None,
    pool: Executor | None = None,
    search: Sequence[str] = (),
) -> list[Job]:
    """Dispatch to the right ATS adapter for this company.

    ``wants_body`` matters only where descriptions cost a request each (Workday, SmartRecruiters,
    BambooHR):
    those postings get a description only if it returns True. Other sources always include
    descriptions.

    ``max_pages`` stops paged listings (Workday, SmartRecruiters, and each search of a search
    source) early; the others are one request.

    ``search`` matters only for sites too big to list (Amazon, Eightfold): they search per term
    instead (``fetch`` passes the title filter's target-level words). Others ignore it.
    With ``pool``, Workday, SmartRecruiters and BambooHR fetch later pages and descriptions
    concurrently on it (BambooHR has only descriptions).
    """
    if company.ats in SEARCH_FETCHERS:
        return SEARCH_FETCHERS[company.ats](company, client, search, max_pages, wants_body, pool)
    if company.ats in ON_DEMAND_FETCHERS:
        return ON_DEMAND_FETCHERS[company.ats](company, client, wants_body, max_pages, pool)
    return FETCHERS[company.ats](company, client)


# Requests are rate-limited per group: one per Workday datacenter (its tenants share
# infrastructure), and one per API host for the other sources (every board is on that host;
# BambooHR gives each tenant its own subdomain, but they are one service, so one group).
_API_HOSTS: dict[str, str] = {
    "boards-api.greenhouse.io": "greenhouse",
    "api.lever.co": "lever",
    "api.ashbyhq.com": "ashby",
    "api.smartrecruiters.com": "smartrecruiters",
    "apply.workable.com": "workable",
    "www.amazon.jobs": "amazon",
}
_WORKDAY_HOST = re.compile(r"[a-z0-9-]+\.(wd\d+)\.myworkdayjobs\.com")
_BAMBOOHR_HOST = re.compile(r"[a-z0-9-]+\.bamboohr\.com")


def rate_group(company: Company) -> str:
    """The rate-limit group a board's requests belong to, e.g. ``workday:wd5`` or ``lever``."""
    if company.ats == "workday":
        return f"workday:{company.datacenter}"
    if company.ats == "eightfold":  # a careers site's own host; limits seen so far are per host
        return company.slug.lower()
    return company.ats


def request_group(url: httpx.URL) -> str:
    """The rate-limit group of one request URL. Agrees with ``rate_group`` for every source."""
    host = url.host.lower()
    if m := _WORKDAY_HOST.fullmatch(host):
        return f"workday:{m.group(1)}"
    if _BAMBOOHR_HOST.fullmatch(host):
        return "bamboohr"
    return _API_HOSTS.get(host, host)
