"""SAP SuccessFactors Career Site Builder sites (e.g. jobs.ball.com, careers.aflac.com).

Career Site Builder has no public JSON API, and robots.txt disallows the ``/services/`` paths its
feeds and search use. What it allows, and every site serves, is a sitemap on the site's host:

    GET https://{host}/robots.txt      read first; the sitemap and job pages must be allowed
    GET https://{host}/sitemap.xml     either an RSS feed of every posting, descriptions included,
                                       or a plain sitemap of job page URLs
    GET https://{host}[/{brand}]/job/{title-and-place}/{id}/   one posting's page (HTML)

A board is ``ats: successfactors`` with ``slug:`` the site's host, e.g. ``careers.aflac.com``.

From an RSS feed, every posting comes in one request. From a plain sitemap, each posting is only
a URL, whose path holds the title and the place (``Richmond-Senior-Manager-VA-23230``); which
pages get fetched follows the URL-word rules in ``_sitemap`` (``Sr_`` is how these URLs write
"Sr."). The page's schema.org microdata gives the real title, location, date posted and
description.

Not every site the survey sees SuccessFactors on is Career Site Builder (careers.netapp.com is
another platform in front of it). Its sitemap lists no job URLs in this shape, so ``fetch``
warns about a board with no postings.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources import _sitemap
from jobhunt.sources._html import to_text

log = logging.getLogger(__name__)

_JOB_PATH = re.compile(r"/(?:[\w-]+/)?job/([^/]+)/(\d+)/?")
_GOOGLE_NS = "{http://base.google.com/ns/1.0}"
_ITEMPROP_OPEN = re.compile(r'<(\w+)\b[^>]*\bitemprop="(title|description)"[^>]*>', re.I)
_META = re.compile(r'<meta\s+itemprop="(\w+)"\s+content="([^"]*)"', re.I)
_ADDRESS = ("addressLocality", "addressRegion", "addressCountry", "postalCode")


def _url(company: Company, path: str) -> str:
    return f"https://{company.slug.lower()}{path}"


def _job_id(url: str) -> tuple[str, str] | None:
    """A job URL's title-and-place text and its id, or None if it isn't a job page."""
    m = _JOB_PATH.fullmatch(urlsplit(url).path)
    if not m:
        return None
    return unquote(m.group(1)).replace("-", " ").strip(), m.group(2)


def _from_feed(company: Company, root: ElementTree.Element) -> list[Job]:
    jobs: dict[str, Job] = {}
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        found = _job_id(link)
        if found is None:
            continue
        location = (item.findtext(f"{_GOOGLE_NS}location") or "").strip()
        title = (item.findtext("title") or "").strip()
        if location and title.endswith(f"({location})"):  # "Title (City, ST, US, 12345)"
            title = title[: -len(location) - 2].strip()
        jobs.setdefault(found[1], Job(
            source="successfactors",
            company=company.name,
            company_slug=company.slug,
            external_id=found[1],
            title=title,
            location=location,
            remote=_sitemap.remote(location, title),
            url=link,
            body=to_text(item.findtext("description")),
        ))
    return list(jobs.values())


def _itemprop(page: str, name: str) -> list[str]:
    """The inner HTML of each element with ``itemprop="<name>"`` (nested tags and all)."""
    found = []
    for m in _ITEMPROP_OPEN.finditer(page):
        if m.group(2).lower() != name:
            continue
        tag, depth, pos = m.group(1).lower(), 1, m.end()
        for t in re.finditer(rf"<(/?){tag}\b[^>]*>", page[pos:], re.I):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                found.append(page[pos : pos + t.start()])
                break
    return found


def _posted(text: str) -> str | None:
    """'Mon Sep 14 00:00:00 UTC 2026', as the pages write it, in ISO 8601."""
    try:
        when = datetime.strptime(text.strip(), "%a %b %d %H:%M:%S UTC %Y")
    except ValueError:
        return None
    return when.replace(tzinfo=UTC).isoformat()


def _from_page(listed: Job, page: str) -> Job:
    titles = _itemprop(page, "title")
    bodies = _itemprop(page, "description")
    if not titles or not bodies:
        raise ValueError("no posting on the page")
    title = " ".join(to_text(titles[0]).split())
    metas: dict[str, str] = {}
    for key, value in _META.findall(page):
        metas.setdefault(key, html.unescape(value).strip())
    location = ", ".join(metas[k] for k in _ADDRESS if metas.get(k)) or metas.get(
        "streetAddress", ""
    )
    location = location or _sitemap.place_from_url(listed.title, title)
    return listed.model_copy(update={
        "title": title,
        "location": location,
        "remote": _sitemap.remote(location, title),
        "body": "\n\n".join(t for t in map(to_text, bodies) if t),
        "posted_at": _posted(metas.get("datePosted", "")),
    })


def fetch(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool] = lambda job: True,
    max_pages: int | None = None,
    pool: Executor | None = None,
) -> list[Job]:
    """Every posting in the site's sitemap. ``max_pages`` doesn't apply: the sitemap is one page.

    With ``pool``, job pages are fetched concurrently.
    """
    rules = _sitemap.robots(client, company.slug.lower())
    if not rules.can_fetch(_sitemap.agent(client), _url(company, "/sitemap.xml")):
        log.warning("successfactors %s: robots.txt disallows /sitemap.xml", company.slug)
        return []
    resp = _sitemap.get_on_host(client, _url(company, "/sitemap.xml"), company.slug.lower(), rules)
    root = _sitemap.parse_xml(resp.content, "the sitemap")
    if root.tag == "rss":
        jobs = _from_feed(company, root)
    elif (ns := root.tag.removesuffix("urlset")) in _sitemap.SITEMAP_NS:
        found: dict[str, Job] = {}
        for loc in root.iter(f"{ns}loc"):
            url = (loc.text or "").strip()
            if _sitemap.on_host(url, company.slug.lower()) and (words_id := _job_id(url)):
                found.setdefault(words_id[1], _sitemap.listed(company, url, *words_id))
        jobs = _sitemap.fetch_pages(company, client, found, wants_body, rules, _from_page, pool)
    else:
        raise ValueError(f"the sitemap is neither an RSS feed nor a urlset ({root.tag})")
    if not jobs:
        log.warning(
            "successfactors %s: 0 postings — is it a Career Site Builder site?", company.slug
        )
    log.info("successfactors %s: %d jobs", company.slug, len(jobs))
    return jobs
