"""Paradox careers sites (e.g. jobs.adp.com, careers.fedex.com).

Paradox is best known for its "Olivia" chat assistant, which many careers sites embed beside
another ATS; those boards belong to that ATS. Some companies' careers sites are Paradox's own.
Their sitemaps and job pages are allowed by robots.txt:

    GET https://{host}/robots.txt   read first
    GET {each Sitemap: robots.txt names, else https://{host}/sitemap.xml}, and any sitemaps an
        index lists (FedEx splits its jobs into 30)
    GET https://{host}[/{lang}]/jobs/{id}/{title}/   one posting (ADP, GM, Verizon), or
    GET https://{host}[/{lang}]/{title}/job/{id}      (FedEx); its JSON-LD JobPosting has the
                                                      details

A board is ``ats: paradox`` with ``slug:`` the site's host. Sites list each posting once per
language (``/es/trabajos/...``); the English (or language-free) URL is kept. The URL's title
words stand in for the title until the page is fetched (see ``_sitemap``).

Many Paradox sites front Workday (a posting's apply link says so): add that board instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import Executor
from urllib.parse import unquote, urlsplit

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources import _sitemap

_LANG = r"(?:(?P<lang>[a-z]{2}(?:-[a-z0-9]{2,3})?)/)?"
_ID = r"(?P<id>[\w-]*\d[\w-]*)"  # an id has a digit: /jobs/saved-jobs/ isn't a posting
_JOB_PATHS = (
    # the word for "jobs" in each language the sites serve
    re.compile(
        rf"/{_LANG}(?:jobs|trabajos|emplois|emploi|empregos|cargos|vagas|stellen|offerte)/{_ID}"
        r"/(?P<title>[^/]+)/?",
        re.I,
    ),
    re.compile(rf"/{_LANG}(?P<title>[^/]+)/job/{_ID}/?", re.I),  # FedEx
)


def job_url(url: str) -> tuple[str, str, int] | None:
    """A job URL's title words, its id, and a rank (English or no language first)."""
    path = urlsplit(url).path
    for pattern in _JOB_PATHS:
        if m := pattern.fullmatch(path):
            words = " ".join(unquote(m.group("title")).replace("-", " ").split())
            rank = 0 if (m.group("lang") or "en").split("-")[0].lower() == "en" else 1
            return words, m.group("id"), rank
    return None


def fetch(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool] = lambda job: True,
    max_pages: int | None = None,
    pool: Executor | None = None,
) -> list[Job]:
    """Every posting in the site's sitemaps. ``max_pages`` doesn't apply."""
    return _sitemap.fetch_job_pages(company, client, job_url, wants_body, pool)
