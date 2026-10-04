"""ATS source adapters. Each exposes ``fetch(company, client) -> list[Job]``."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from jobhunt.schema import ATSName, Company, Job
from jobhunt.sources import ashby, greenhouse, lever

Fetcher = Callable[[Company, httpx.Client], list[Job]]

FETCHERS: dict[ATSName, Fetcher] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}


def fetch_company(company: Company, client: httpx.Client) -> list[Job]:
    """Dispatch to the right ATS adapter for this company."""
    return FETCHERS[company.ats](company, client)
