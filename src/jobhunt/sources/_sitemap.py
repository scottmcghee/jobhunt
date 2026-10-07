"""Careers sites read through their sitemaps and job pages: what robots.txt allows on sites whose
search is off-limits (SuccessFactors Career Site Builder, Radancy, Paradox).

A sitemap lists each posting only as a URL, so the URL's words (``/job/seattle/director-platform``
reads "seattle director platform") stand in for the title until the page is fetched. The title
is one run of those words, so a page is fetched if ``wants_body`` passes any run of them, with
"_" read as "." (``Sr_`` is how some URLs write "Sr.") and words the URL joined by dropping a "/"
(``ManagerDirector``, ``VPDirector``, ``SVPGM``) split apart. That fetches some pages whose title
then fails the filter. It can still skip one if the URL joined two lowercase words, or joined
all-caps words more than once in a title or into one longer than 8 letters. A posting wanted but
left without its page (robots.txt, or a failed page) gets the reading of its URL words that
passed as its title; the others keep what their URL says.

Radancy and Paradox job pages carry a schema.org ``JobPosting`` as JSON-LD, which gives the real
title, description, location and date posted (``from_posting``).
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable, Iterator
from concurrent.futures import Executor
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources._html import to_text

log = logging.getLogger(__name__)

# Career Site Builder writes Google's old sitemap namespace; others the standard one
SITEMAP_NS = (
    "{http://www.google.com/schemas/sitemap/0.9}",
    "{http://www.sitemaps.org/schemas/sitemap/0.9}",
)
MAX_SITEMAPS = 50  # sitemaps read per board, index files included (FedEx splits into 31)
MAX_REDIRECTS = 5
# "ManagerDirector", "VPDirector": the URL dropped a "/"
_JOINED = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CAPS = re.compile(r"\b[A-Z]{2,8}\b")  # "SVPGM" may be "SVP/GM": each two-way split is a reading
_TOKEN = re.compile(r"[^\W_]+")
_EDGES = re.compile(r"^[\W_]+|[^\w.]+$")  # keeps the end of "D.C." and "U.S."
# ADP writes the type attribute escaped: type="application/ld&#x2B;json"
_LD_JSON = re.compile(
    r"""<script\b[^>]*\btype\s*=\s*["']application/ld(?:\+|&#x2b;|&#43;)json["'][^>]*>(.*?)</script>""",
    re.I | re.S,
)
_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_ADDRESS = ("addressLocality", "addressRegion", "addressCountry")

# A job URL -> (its words, its id, a rank: lower wins when two URLs share an id), or None
JobUrl = Callable[[str], "tuple[str, str, int] | None"]


def robots(client: httpx.Client, host: str) -> RobotFileParser:
    """A site's robots.txt rules, per RFC 9309: redirects are followed, a 4xx allows everything,
    a 5xx nothing."""
    rules = RobotFileParser()
    resp = client.get(f"https://{host}/robots.txt", follow_redirects=True)
    if resp.status_code >= 500:
        rules.disallow_all = True
    elif resp.status_code >= 400:
        rules.allow_all = True
    else:
        rules.parse(resp.text.splitlines())
    return rules


def agent(client: httpx.Client) -> str:
    return str(client.headers.get("user-agent", "*"))


def parse_xml(content: bytes, what: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as e:
        raise ValueError(f"{what} isn't XML ({e})") from None


def on_host(url: str, host: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == host


def get_on_host(
    client: httpx.Client, url: str, host: str, rules: RobotFileParser
) -> httpx.Response:
    """GET a URL on the site's host within robots.txt, following redirects on that host only
    (careers.l3harris.com/sitemap.xml redirects to /en/sitemap.xml), each hop checked against
    robots.txt. The client's own redirect following is turned off for it."""
    for _ in range(MAX_REDIRECTS + 1):
        if not rules.can_fetch(agent(client), url):
            raise ValueError("robots.txt disallows it")
        resp = client.get(url, follow_redirects=False)
        if not (resp.is_redirect and "location" in resp.headers):
            resp.raise_for_status()
            return resp
        url = urljoin(url, resp.headers["location"])
        if not on_host(url, host):
            raise ValueError(f"it redirects to another host ({url})")
    raise ValueError("too many redirects")


def sitemap_urls(company: Company, client: httpx.Client, rules: RobotFileParser) -> list[str]:
    """Every page URL in a site's sitemaps: the ones robots.txt names on the site's host, else
    /sitemap.xml, following sitemap index files (at most ``MAX_SITEMAPS`` files in all).

    A sitemap that fails is skipped with a warning, unless no starting one (each robots.txt
    names, or /sitemap.xml) answers: then the first one's error is raised, so a 404 means the
    board is gone (an HTTPStatusError), and an answer that isn't a sitemap is a ValueError.
    """
    host = company.slug.lower()
    named = [u for u in rules.site_maps() or [] if on_host(u, host)]
    starts = list(dict.fromkeys(named)) or [f"https://{host}/sitemap.xml"]
    queue, seen, urls = list(starts), set(), []
    failed: list[Exception] = []
    answered = False
    while queue and len(seen) < MAX_SITEMAPS:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = get_on_host(client, url, host, rules)
            root = parse_xml(resp.content, "the sitemap")
            ns = next((n for n in SITEMAP_NS if root.tag.startswith(n)), None)
            if ns is None:
                raise ValueError(f"the sitemap is neither a urlset nor an index ({root.tag})")
        except (httpx.HTTPError, ValueError) as e:
            if url in starts:
                if len(starts) == 1:
                    raise
                failed.append(e)
            log.warning("%s %s: skipped sitemap %s (%s)", company.ats, company.slug, url, e)
            continue
        answered = answered or url in starts
        locs = [(loc.text or "").strip() for loc in root.iter(f"{ns}loc")]
        if root.tag == f"{ns}sitemapindex":
            queue += [u for u in locs if on_host(u, host)]
        else:
            urls += locs
    if failed and not answered:
        raise failed[0]
    if queue:
        log.warning(
            "%s %s: read %d sitemaps; skipped %d more", company.ats, company.slug, len(seen),
            len(queue),
        )
    return urls


def remote(*texts: str) -> bool | None:
    return True if any("remote" in t.lower() for t in texts) else None


def listed(company: Company, url: str, words: str, job_id: str) -> Job:
    """A posting known only by its URL: the URL's words stand in for the title."""
    return Job(
        source=company.ats,
        company=company.name,
        company_slug=company.slug,
        external_id=job_id,
        title=words,
        remote=remote(words),
        url=url,
    )


def wanted(job: Job, wants_body: Callable[[Job], bool]) -> str | None:
    """The reading of a listed posting's URL words in which ``wants_body`` passes a run of them,
    as the title, or None if it passes none.

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
                    return reading
    return None


def place_from_url(words: str, title: str) -> str:
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


def fetch_pages(
    company: Company,
    client: httpx.Client,
    found: dict[str, Job],
    wants_body: Callable[[Job], bool],
    rules: RobotFileParser,
    parse: Callable[[Job, str], Job],
    pool: Executor | None,
) -> list[Job]:
    """``found`` (postings known by URL) with the pages ``wanted`` asks for fetched and parsed.

    A page that redirects off the site's host, or to a path robots.txt disallows, is not
    followed: that posting is kept without a description.
    """
    found = dict(found)
    host = company.slug.lower()
    want = {
        job.external_id: reading
        for job in found.values()
        if (reading := wanted(job, wants_body)) is not None
    }
    allowed = [found[i] for i in want if rules.can_fetch(agent(client), found[i].url)]
    if len(allowed) < len(want):
        log.warning(
            "%s %s: robots.txt disallows %d job pages; kept them without descriptions",
            company.ats, company.slug, len(want) - len(allowed),
        )

    def page(job: Job) -> Job | None:
        try:
            resp = get_on_host(client, job.url, host, rules)  # some redirect to an internal id
            return parse(job, resp.text)
        except (httpx.HTTPError, ValueError) as e:
            error = " ".join(str(e).split())  # httpx's messages can span lines
            log.warning(
                "%s %s: no description for %s (%s)",
                company.ats, company.slug, job.external_id, error,
            )
            return None

    run = pool.map if pool is not None else map
    for job in run(page, allowed):
        if job is not None:
            found[job.external_id] = job
            del want[job.external_id]
    for job_id, reading in want.items():  # no page: the reading that passed, as the title
        found[job_id] = found[job_id].model_copy(update={"title": reading})
    return list(found.values())


# ------------------------------------------------------------------ JSON-LD job pages


def _postings(data: object) -> Iterator[dict]:
    if isinstance(data, list):
        for item in data:
            yield from _postings(item)
    elif isinstance(data, dict):
        kind = data.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield data
        yield from _postings(data.get("@graph"))


def job_postings(page: str) -> list[dict]:
    """The schema.org JobPosting objects in a page's JSON-LD blocks."""
    found = []
    for block in _LD_JSON.findall(page):
        try:
            found += _postings(json.loads(block, strict=False))  # some put raw newlines in strings
        except ValueError:
            continue
    return found


def _text(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("name")
    return " ".join(html.unescape(value).split()) if isinstance(value, str) else ""


def _place(place: object) -> str:
    if not isinstance(place, dict):
        return ""
    address = place.get("address")
    addresses = address if isinstance(address, list) else [address]  # GM: a list of them
    found = []
    for one in addresses:
        if isinstance(one, str):
            found.append(_text(one))
        elif isinstance(one, dict):
            parts = [_text(one.get(k)) for k in _ADDRESS]
            found.append(", ".join(dict.fromkeys(p for p in parts if p)))
    return "; ".join(p for p in found if p) or _text(place.get("name"))


def ld_location(posting: dict) -> str:
    places = posting.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    return "; ".join(dict.fromkeys(p for p in map(_place, places) if p))


def ld_date(value: object) -> str | None:
    """datePosted as ISO 8601: a full timestamp as given, a date zero-padded ("2026-10-7")."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _DATE.fullmatch(value):
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            pass
    m = _DATE.match(value)
    try:
        return datetime(*map(int, m.groups())).date().isoformat() if m else None
    except ValueError:  # "2026-13-40"
        return None


def from_posting(listed_job: Job, page: str) -> Job:
    """A listed posting completed from its page's JSON-LD JobPosting; ValueError if there isn't
    one with a title."""
    postings = job_postings(page)
    title = _text(postings[0].get("title")) if postings else ""
    if not title:
        raise ValueError("no JobPosting on the page")
    posting = postings[0]
    location = ld_location(posting) or place_from_url(listed_job.title, title)
    telecommute = str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    description = posting.get("description")
    return listed_job.model_copy(update={
        "title": title,
        "location": location,
        "remote": True if telecommute else remote(location, title),
        "body": to_text(description if isinstance(description, str) else ""),
        "posted_at": ld_date(posting.get("datePosted")),
    })


def fetch_job_pages(
    company: Company,
    client: httpx.Client,
    job_url: JobUrl,
    wants_body: Callable[[Job], bool],
    pool: Executor | None,
) -> list[Job]:
    """Every posting a site's sitemaps list, with JSON-LD pages for those ``wants_body`` wants."""
    rules = robots(client, company.slug.lower())
    best: dict[str, tuple[int, Job]] = {}
    for url in sitemap_urls(company, client, rules):
        if not on_host(url, company.slug.lower()) or (found := job_url(url)) is None:
            continue
        words, job_id, rank = found
        if job_id not in best or rank < best[job_id][0]:  # the same posting in another language
            best[job_id] = (rank, listed(company, url, words, job_id))
    jobs = fetch_pages(
        company, client, {i: job for i, (_, job) in best.items()}, wants_body, rules,
        from_posting, pool,
    )
    if not jobs:
        log.warning("%s %s: 0 postings — no job URLs in its sitemaps", company.ats, company.slug)
    log.info("%s %s: %d jobs", company.ats, company.slug, len(jobs))
    return jobs
