"""ATS source adapters. Each exposes ``fetch(company, client) -> list[Job]``."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from jobhunt.schema import ATSName, Company, Job
from jobhunt.sources import ashby, greenhouse, lever, workday

Fetcher = Callable[[Company, httpx.Client], list[Job]]

FETCHERS: dict[ATSName, Fetcher] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}


def fetch_company(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool] = lambda job: True,
) -> list[Job]:
    """Dispatch to the right ATS adapter for this company.

    ``wants_body`` matters only where descriptions cost a request each (Workday): those postings
    get a description only if it returns True. Other sources always include descriptions.
    """
    if company.ats == "workday":
        return workday.fetch(company, client, wants_body)
    return FETCHERS[company.ats](company, client)
