"""Radancy (TalentBrew) careers sites (e.g. jobs.intuit.com, careers.blackrock.com).

Radancy builds careers sites in front of an ATS (Workday, Avature, SuccessFactors, ...). Its
search (``/search-jobs/``) is disallowed by robots.txt; its sitemap and job pages are allowed:

    GET https://{host}/robots.txt   read first
    GET {each Sitemap: robots.txt names, else https://{host}/sitemap.xml}
    GET https://{host}[/{lang}]/job/{city}/{title}/{org}/{id}   one posting; its JSON-LD
                                                                  JobPosting has the details

A board is ``ats: radancy`` with ``slug:`` the site's host. The URL's city and title words stand
in for the title until the page is fetched (see ``_sitemap``); only postings whose title could
pass ``wants_body`` cost a page.

When the ATS behind the site is one jobhunt reads (a posting's apply link says which), add that
board instead: both list the same jobs, under different keys.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import Executor
from urllib.parse import unquote, urlsplit

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources import _sitemap

_JOB_PATH = re.compile(
    r"/(?:(?P<lang>[a-z]{2}(?:-[a-z]{2})?)/)?job/(?P<city>[^/]+)/(?P<title>[^/]+)/\d+/(?P<id>\d+)/?",
    re.I,
)


def job_url(url: str) -> tuple[str, str, int] | None:
    """A job URL's city-and-title words, its id, and a rank (English or no language first)."""
    m = _JOB_PATH.fullmatch(urlsplit(url).path)
    if not m:
        return None
    words = " ".join(unquote(m.group(g)).replace("-", " ") for g in ("city", "title"))
    rank = 0 if (m.group("lang") or "en").split("-")[0].lower() == "en" else 1
    return " ".join(words.split()), m.group("id"), rank


def fetch(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool] = lambda job: True,
    max_pages: int | None = None,
    pool: Executor | None = None,
) -> list[Job]:
    """Every posting in the site's sitemaps. ``max_pages`` doesn't apply."""
    return _sitemap.fetch_job_pages(company, client, job_url, wants_body, pool)
