"""SAP SuccessFactors Career Site Builder sites (e.g. jobs.ball.com, careers.aflac.com).

Career Site Builder has no public JSON API, and robots.txt disallows the ``/services/`` paths its
feeds and search use. What it allows, and every site serves, is a sitemap on the site's host:

    GET https://{host}/robots.txt      read first; the sitemap and job pages must be allowed
    GET https://{host}/sitemap.xml     either an RSS feed of every posting, descriptions included,
                                       or a plain sitemap of job page URLs
    GET https://{host}[/{brand}]/job/{title-and-place}/{id}/   one posting's page (HTML)

A board is ``ats: successfactors`` with ``slug:`` the site's host, e.g. ``careers.aflac.com``.

From an RSS feed, every posting comes in one request. From a plain sitemap, each posting is only
a URL, whose path holds the title and the place (``Richmond-Senior-Manager-VA-23230``). The
title is one run of those words, so a page is fetched if ``wants_body`` passes any run of them,
with "_" read as "." (``Sr_`` is how the URLs write "Sr.") and words the URL joined by dropping a
"/" (``ManagerDirector``, ``VPDirector``, ``SVPGM``) split apart. That fetches some pages whose
title then fails the filter. It can still skip one if the URL joined two lowercase words, or
joined all-caps words more than once in a title or into one longer than 8 letters. The page's
schema.org microdata gives the real title, location, date posted and description. The others
keep what their URL says.

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
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text

log = logging.getLogger(__name__)

_JOB_PATH = re.compile(r"/(?:[\w-]+/)?job/([^/]+)/(\d+)/?")
# Career Site Builder writes Google's old sitemap namespace; others the standard one
_SITEMAP_NS = (
    "{http://www.google.com/schemas/sitemap/0.9}",
    "{http://www.sitemaps.org/schemas/sitemap/0.9}",
)
_GOOGLE_NS = "{http://base.google.com/ns/1.0}"
_ITEMPROP_OPEN = re.compile(r'<(\w+)\b[^>]*\bitemprop="(title|description)"[^>]*>', re.I)
_META = re.compile(r'<meta\s+itemprop="(\w+)"\s+content="([^"]*)"', re.I)
_ADDRESS = ("addressLocality", "addressRegion", "addressCountry", "postalCode")
# "ManagerDirector", "VPDirector": the URL dropped a "/"
_JOINED = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CAPS = re.compile(r"\b[A-Z]{2,8}\b")  # "SVPGM" may be "SVP/GM": each two-way split is a reading
_TOKEN = re.compile(r"[^\W_]+")
_EDGES = re.compile(r"^[\W_]+|[^\w.]+$")  # keeps the end of "D.C." and "U.S."


def _url(company: Company, path: str) -> str:
    return f"https://{company.slug.lower()}{path}"


def _robots(company: Company, client: httpx.Client) -> RobotFileParser:
    """The site's robots.txt rules, per RFC 9309: redirects are followed, a 4xx allows everything,
    a 5xx nothing."""
    rules = RobotFileParser()
    resp = client.get(_url(company, "/robots.txt"), follow_redirects=True)
    if resp.status_code >= 500:
        rules.disallow_all = True
    elif resp.status_code >= 400:
        rules.allow_all = True
    else:
        rules.parse(resp.text.splitlines())
    return rules


def _job_id(url: str) -> tuple[str, str] | None:
    """A job URL's title-and-place text and its id, or None if it isn't a job page."""
    m = _JOB_PATH.fullmatch(urlsplit(url).path)
    if not m:
        return None
    return unquote(m.group(1)).replace("-", " ").strip(), m.group(2)


def _remote(*texts: str) -> bool | None:
    return True if any("remote" in t.lower() for t in texts) else None


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
            remote=_remote(location, title),
            url=link,
            body=to_text(item.findtext("description")),
        ))
    return list(jobs.values())


def _listed(company: Company, url: str, words: str, job_id: str) -> Job:
    """A posting known only by its URL: the URL's words stand in for the title."""
    return Job(
        source="successfactors",
        company=company.name,
        company_slug=company.slug,
        external_id=job_id,
        title=words,
        remote=_remote(words),
        url=url,
    )


def _wanted(job: Job, wants_body: Callable[[Job], bool]) -> bool:
    """Whether ``wants_body`` passes any run of a listed posting's URL words, as the title.

    The real title is one of those runs, read with "_" as "." and joined words split: at a
    capital after a lowercase letter or before one, or anywhere in one all-caps word.
    """
    dotted = job.title.replace("_", ".")
    split = _JOINED.sub(" ", dotted)
    readings = [job.title, dotted, split]
    for m in _CAPS.finditer(split):
        for k in range(1, len(m.group())):
            readings.append(f"{split[: m.start() + k]} {split[m.start() + k :]}")
    tried: set[str] = set()
    for reading in dict.fromkeys(readings):
        words = reading.split()
        for n in range(len(words), 0, -1):
            for i in range(len(words) - n + 1):
                span = " ".join(words[i : i + n])
                if span in tried:
                    continue
                tried.add(span)
                if wants_body(job.model_copy(update={"title": span})):
                    return True
    return False


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


def _place_from_url(words: str, title: str) -> str:
    """The place words around the title in a job URL's words, matched word by word.

    'Richmond Senior Manager Marketing VA 23230' with title 'Senior Manager - Marketing' ->
    'Richmond, VA 23230'.
    If the title isn't there, the URL's words, place and all. Either way "_" reads as ".".
    """
    words = words.replace("_", ".")
    tokens = list(_TOKEN.finditer(words))
    have = [t.group().lower() for t in tokens]
    want = [t.lower() for t in _TOKEN.findall(title)]
    for i in range(len(have) - len(want) + 1):
        if want and have[i : i + len(want)] == want:
            before = words[: tokens[i].start()]
            after = words[tokens[i + len(want) - 1].end() :]
            return ", ".join(p for p in (_EDGES.sub("", before), _EDGES.sub("", after)) if p)
    return words


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
    location = location or _place_from_url(listed.title, title)
    return listed.model_copy(update={
        "title": title,
        "location": location,
        "remote": _remote(location, title),
        "body": "\n\n".join(t for t in map(to_text, bodies) if t),
        "posted_at": _posted(metas.get("datePosted", "")),
    })


def _page(company: Company, client: httpx.Client, job: Job) -> Job | None:
    try:
        resp = client.get(job.url, follow_redirects=True)  # some sites redirect to an internal id
        resp.raise_for_status()
        return _from_page(job, resp.text)
    except (httpx.HTTPError, ValueError) as e:
        error = " ".join(str(e).split())  # httpx's messages can span lines
        log.warning(
            "successfactors %s: no description for %s (%s)", company.slug, job.external_id, error
        )
        return None


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
    robots = _robots(company, client)
    agent = str(client.headers.get("user-agent", "*"))
    if not robots.can_fetch(agent, _url(company, "/sitemap.xml")):
        log.warning("successfactors %s: robots.txt disallows /sitemap.xml", company.slug)
        return []
    resp = client.get(_url(company, "/sitemap.xml"))
    resp.raise_for_status()
    try:
        root = ElementTree.fromstring(resp.content)
    except ElementTree.ParseError as e:
        raise ValueError(f"the sitemap isn't XML ({e})") from None
    if root.tag == "rss":
        jobs = _from_feed(company, root)
    elif (ns := root.tag.removesuffix("urlset")) in _SITEMAP_NS:
        listed: dict[str, Job] = {}
        for loc in root.iter(f"{ns}loc"):
            url = (loc.text or "").strip()
            if (found := _job_id(url)) is not None:
                listed.setdefault(found[1], _listed(company, url, *found))
        wanted = [job for job in listed.values() if _wanted(job, wants_body)]
        allowed = [job for job in wanted if robots.can_fetch(agent, job.url)]
        if len(allowed) < len(wanted):
            log.warning(
                "successfactors %s: robots.txt disallows %d job pages; kept them without "
                "descriptions", company.slug, len(wanted) - len(allowed),
            )
        run = pool.map if pool is not None else map
        for page in run(lambda job: _page(company, client, job), allowed):
            if page is not None:
                listed[page.external_id] = page
        jobs = list(listed.values())
    else:
        raise ValueError(f"the sitemap is neither an RSS feed nor a urlset ({root.tag})")
    if not jobs:
        log.warning(
            "successfactors %s: 0 postings — is it a Career Site Builder site?", company.slug
        )
    log.info("successfactors %s: %d jobs", company.slug, len(jobs))
    return jobs
