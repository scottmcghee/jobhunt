"""ATS source adapters. Each exposes ``fetch(company, client) -> list[Job]``."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from jobhunt.schema import ATSName, Company, Job
from jobhunt.sources import ashby, greenhouse, lever, smartrecruiters, workday

Fetcher = Callable[[Company, httpx.Client], list[Job]]
BodyCheck = Callable[[Job], bool]

FETCHERS: dict[ATSName, Fetcher] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}

# Sources whose listings lack descriptions, so each description costs a request.
PagedFetcher = Callable[[Company, httpx.Client, BodyCheck, int | None], list[Job]]
ON_DEMAND_FETCHERS: dict[ATSName, PagedFetcher] = {
    "workday": workday.fetch,
    "smartrecruiters": smartrecruiters.fetch,
}


def fetch_company(
    company: Company,
    client: httpx.Client,
    wants_body: BodyCheck = lambda job: True,
    max_pages: int | None = None,
) -> list[Job]:
    """Dispatch to the right ATS adapter for this company.

    ``wants_body`` matters only where descriptions cost a request each (Workday, SmartRecruiters):
    those postings get a description only if it returns True. Other sources always include
    descriptions.

    ``max_pages`` stops paged listings (Workday, SmartRecruiters) early; the others are one request.
    """
    if company.ats in ON_DEMAND_FETCHERS:
        return ON_DEMAND_FETCHERS[company.ats](company, client, wants_body, max_pages)
    return FETCHERS[company.ats](company, client)
