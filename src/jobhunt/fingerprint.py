"""Which hiring platform a careers site runs, and the job board jobhunt should read for it.

Shared by ``scripts/survey_careers.py`` (the S&P 500 survey) and ``python -m jobhunt.discover``.
A page is checked for platform fingerprints (``PLATFORMS``); every URL in it goes through
``jobhunt.slugs.board_from_url``; and a few platforms get one or two more polite requests to find
the board (Phenom's search, an iCIMS Career Site's job API, a Radancy or Paradox sitemap). Sites
in front of another ATS give that ATS's board, so a job is never fetched twice.

Every request goes through ``Polite``: one at a time, ``delay`` seconds apart, within each host's
robots.txt, redirect by redirect (RFC 9309: a robots.txt that answers 5xx or can't be fetched
disallows everything).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from jobhunt import slugs
from jobhunt.schema import Company
from jobhunt.sources import _sitemap, paradox, phenom, radancy

MAX_PAGE = 2_000_000  # characters of a page to scan
MAX_REDIRECTS = 5
# Job sites and social networks: a homepage link to one says nothing about the company's platform.
_ELSEWHERE = ("linkedin.com", "glassdoor.com", "indeed.com", "ziprecruiter.com", "facebook.com",
              "x.com", "twitter.com", "instagram.com", "youtube.com")

# Hiring platforms and how their pages give them away. Not all are sources jobhunt supports.
PLATFORMS = {
    "workday": r"myworkdayjobs\.com|myworkdaysite\.com",
    "greenhouse": r"greenhouse\.io",
    "lever": r"jobs\.lever\.co",
    "ashby": r"ashbyhq\.com",
    "smartrecruiters": r"smartrecruiters\.com",
    "workable": r"workable\.com",
    "bamboohr": r"bamboohr\.com",
    "eightfold": r"eightfold\.ai|/api/pcsx|/api/apply/v2",
    "oracle": r"oraclecloud\.com|/hcmUI/CandidateExperience",
    "icims": r"icims\.com",
    "icims_careers": r"jibecdn\.com",  # iCIMS Career Sites (formerly Jibe)
    "jobvite": r"jobvite\.com",
    "taleo": r"taleo\.net",
    "successfactors": r"successfactors\.(com|eu)|jobs2web|rmkcdn",
    "phenom": r"phenompeople\.com|cdn\.phenompeople",
    "avature": r"avature\.net",
    "radancy": r"radancy|tbcdn\.talentbrew|talentbrew\.com",
    "brassring": r"brassring\.com|kenexa",
    "ukg": r"ultipro\.com|ukg\.net",
    "paradox": r"paradox\.ai",
}
_PLATFORMS = {name: re.compile(pattern, re.I) for name, pattern in PLATFORMS.items()}
_URL = re.compile(r"""https?://[^\s"'<>\\)]+""")
_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)""", re.I)
_CAREERS = re.compile(r"career|jobs?\b|join-?us", re.I)
_COUNTRY = re.compile(r"[a-z]{2,10}")  # a Phenom site's /us/en/ or /global/en/
_LANGUAGE = re.compile(r"[a-z]{2}")
# Career Site Builder's own scripts and styles: the page is on a SuccessFactors careers site, not
# just linking to one (careers.netapp.com mentions SuccessFactors but runs another platform)
SITEMAP_SOURCES = {"radancy": radancy.job_url, "paradox": paradox.job_url}
MAX_SURVEY_SITEMAPS = 3  # sitemaps read looking for one job URL (an index counts)
_CAREER_SITE_BUILDER = re.compile(r"/platform/(?:js/j2w|csb)\b")


def candidate_urls(site: str) -> list[str]:
    host = (urlsplit(site).hostname or "").lower().removeprefix("www.")
    return [
        f"https://www.{host}/careers",
        f"https://{host}/careers",
        f"https://careers.{host}/",
        f"https://jobs.{host}/",
    ]


def platforms(text: str) -> set[str]:
    return {name for name, pattern in _PLATFORMS.items() if pattern.search(text)}


def career_links(page: str, base: str, limit: int = 2) -> list[str]:
    """Up to ``limit`` distinct links on a page that look like careers or jobs pages."""
    links: list[str] = []
    for href in _HREF.findall(page):
        try:
            url = urljoin(base, href.strip())
            parts = urlsplit(url)
            host = parts.hostname or ""
            httpx.URL(url)
        except (ValueError, httpx.InvalidURL):  # e.g. "https://[object Object]/careers", a bad port
            continue
        if parts.scheme not in ("http", "https") or url in links:
            continue
        if any(host == d or host.endswith("." + d) for d in _ELSEWHERE):
            continue
        if _CAREERS.search(parts.path) or host.startswith(("jobs.", "careers.")):
            links.append(url)
            if len(links) == limit:
                break
    return links


class Polite:
    """One request at a time, ``delay`` seconds apart, within each host's robots.txt."""

    def __init__(self, client: httpx.Client, delay: float):
        self.client, self.delay = client, delay
        self.robots: dict[str, RobotFileParser] = {}
        self.skipped: list[str] = []  # URLs robots.txt kept us from
        self.errors: list[str] = []  # requests that got no response
        self.responses = 0

    def _fetch(
        self, url: str, follow_redirects: bool, json: dict | None = None
    ) -> httpx.Response | None:
        time.sleep(self.delay)
        try:
            if json is None:
                resp = self.client.get(url, follow_redirects=follow_redirects)
            else:
                resp = self.client.post(url, json=json, follow_redirects=follow_redirects)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:  # ValueError: a bad IDNA host
            self.errors.append(f"{url}: {type(e).__name__}")
            return None
        self.responses += 1
        return resp

    def get(self, url: str) -> httpx.Response | None:
        """GET within robots.txt, following redirects by hand so each hop is checked too."""
        for _ in range(MAX_REDIRECTS + 1):
            if not self.allowed(url):
                self.skipped.append(url)
                return None
            resp = self._fetch(url, follow_redirects=False)
            if resp is None or not resp.is_redirect or "location" not in resp.headers:
                return resp  # a 3xx with no Location goes nowhere: it is the answer
            try:
                url = urljoin(url, resp.headers["location"])
            except ValueError:  # a malformed Location, like http://[oops: it is the answer
                self.errors.append(f"{url}: bad redirect")
                return resp
        self.errors.append(f"{url}: too many redirects")
        return None

    def post(self, url: str, body: dict) -> httpx.Response | None:
        """POST within robots.txt; no redirects (an API call that redirects has moved)."""
        if not self.allowed(url):
            self.skipped.append(url)
            return None
        return self._fetch(url, follow_redirects=False, json=body)

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            resp = self._fetch(f"{origin}/robots.txt", follow_redirects=True)
            rules = RobotFileParser()
            if resp is None or resp.status_code >= 500:
                rules.disallow_all = True  # RFC 9309: unreachable means disallow everything
            elif resp.status_code >= 400:
                rules.allow_all = True  # no robots.txt (or a 401/403): everything is allowed
            else:
                rules.parse(resp.text.splitlines())
            self.robots[origin] = rules
        agent = str(self.client.headers.get("user-agent", "*"))
        return self.robots[origin].can_fetch(agent, url)


def read_page(page: httpx.Response, name: str) -> tuple[set[str], list[Company]]:
    """The platforms a page shows, and the boards its own URL and links point at."""
    text = page.text[:MAX_PAGE]
    found = platforms(f"{text} {page.url}")
    boards = []
    for url in [str(page.url), *map(slugs._trim, _URL.findall(text))]:
        if board := slugs.board_from_url(url):
            boards.append(board.model_copy(update={"name": name}))
    if "successfactors" in found and _CAREER_SITE_BUILDER.search(text):
        host = (page.url.host or "").lower()
        try:
            boards.append(Company(name=name, ats="successfactors", slug=host))
        except ValueError:  # not a host Company accepts
            pass
    return found, boards


def icims_careers_boards(polite: Polite, url: str, name: str) -> list[Company]:
    """An iCIMS Career Site's board: its host, if one posting comes back from its job API."""
    host = (urlsplit(url).hostname or "").lower()
    resp = polite.get(f"https://{host}/api/jobs?page=1&limit=1")
    if resp is None or resp.status_code >= 400:
        return []
    try:
        jobs = resp.json().get("jobs")
    except (ValueError, AttributeError):
        return []
    if not jobs:
        return []
    try:
        return [Company(name=name, ats="icims_careers", slug=host)]
    except ValueError:  # not a host Company accepts
        return []


def phenom_board(url: str, name: str) -> Company | None:
    """The Phenom board a page of the site is on: its host, and the /us/en/ in its path if any."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    locale = segments[:2]
    if len(locale) < 2 or not (_COUNTRY.fullmatch(locale[0]) and _LANGUAGE.fullmatch(locale[1])):
        locale = []
    slug = "/".join([(parts.hostname or "").lower(), *locale])
    try:
        return Company(name=name, ats="phenom", slug=slug)
    except ValueError:
        return None


def phenom_boards(polite: Polite, url: str, name: str) -> list[Company]:
    """A Phenom site's board, or the boards its postings apply on if jobhunt reads those.

    Phenom fronts an ATS (often Workday); the ATS lists everything, and fetching both would fetch
    every job twice. One search page (no keywords) shows where postings apply. A site whose
    search finds nothing (or that isn't the Phenom site, only a page linking to it) gives none.
    """
    board = phenom_board(url, name)
    if board is None or (resp := polite.post(*phenom.search_request(board, ""))) is None:
        return []
    try:
        resp.raise_for_status()
        rows, _ = phenom.search_results(resp.json())
    except (httpx.HTTPStatusError, ValueError):
        return []
    fronted: dict[str, Company] = {}
    for raw in rows:
        apply = raw.get("applyUrl") if isinstance(raw, dict) else None
        if isinstance(apply, str) and (found := slugs.board_from_url(apply)):
            fronted.setdefault(found.key.lower(), found.model_copy(update={"name": name}))
    return list(fronted.values()) or ([board] if rows else [])


def _first_job_url(polite: Polite, host: str, job_url: _sitemap.JobUrl) -> str | None:
    """The first job URL in a site's sitemaps (robots.txt's Sitemap: lines, else /sitemap.xml)."""
    origin = f"https://{host}"
    polite.allowed(origin + "/")  # reads robots.txt
    named = polite.robots[origin].site_maps() or []
    queue = [u for u in named if (urlsplit(u).hostname or "").lower() == host]
    queue = queue or [origin + "/sitemap.xml"]
    for _ in range(MAX_SURVEY_SITEMAPS):
        if not queue:
            break
        resp = polite.get(queue.pop(0))
        if resp is None or resp.status_code >= 400:
            continue
        try:
            root = _sitemap.parse_xml(resp.content, "the sitemap")
        except ValueError:
            continue
        locs = [(e.text or "").strip() for e in root.iter() if e.tag.endswith("}loc")]
        if root.tag.endswith("}sitemapindex"):
            queue += [u for u in locs if (urlsplit(u).hostname or "").lower() == host]
        elif found := next((u for u in locs if job_url(u)), None):
            return found
    return None


def sitemap_boards(polite: Polite, url: str, name: str, ats: str) -> list[Company]:
    """A Radancy or Paradox site's board, or the boards its postings apply on if jobhunt reads
    those (often Workday): one posting's page (the first in the sitemap) shows where. A site
    whose sitemaps list no job URLs (Paradox's chat widget on another platform's site) gives
    none."""
    host = (urlsplit(url).hostname or "").lower()
    try:
        board = Company(name=name, ats=ats, slug=host)
    except ValueError:
        return []
    job = _first_job_url(polite, host, SITEMAP_SOURCES[ats])
    if job is None:
        return []
    page = polite.get(job)
    fronted: dict[str, Company] = {}
    if page is not None and page.status_code < 400:
        for link in map(slugs._trim, _URL.findall(page.text[:MAX_PAGE])):
            if "apply" in link.lower() and (found := slugs.board_from_url(link)):
                fronted.setdefault(found.key.lower(), found.model_copy(update={"name": name}))
    return list(fronted.values()) or [board]



@dataclass
class Site:
    """What one careers site showed: the pages that named a platform, the platforms, the boards."""

    pages: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    boards: list[Company] = field(default_factory=list)


def visit(
    polite: Polite,
    url: str,
    name: str,
    site: Site,
    fetched: dict[str, httpx.Response | None] | None = None,
) -> bool:
    """Read one page into ``site``; True if it named a platform or a board. ``fetched``, if
    given, remembers the response for ``url``."""
    page = polite.get(url)
    if fetched is not None:
        fetched[url] = page
    if page is None or page.status_code >= 400:
        return False
    found, boards = read_page(page, name)
    if not (found or boards):
        return False
    if "phenom" in found and not boards:
        boards = phenom_boards(polite, str(page.url), name)
    if "icims_careers" in found and not boards:
        boards = icims_careers_boards(polite, str(page.url), name)
    for ats in SITEMAP_SOURCES:
        if ats in found and not boards:
            boards = sitemap_boards(polite, str(page.url), name, ats)
    site.pages.append(str(page.url))
    site.platforms = sorted(found)
    seen = {b.key.lower() for b in site.boards}
    for board in boards:
        if (key := board.key.lower()) not in seen:
            seen.add(key)
            site.boards.append(board)
    return True


def survey_site(polite: Polite, home: str, name: str, urls: list[str] | None = None) -> Site:
    """Try ``urls`` (default: the likely careers pages of ``home``'s domain) until one names a
    platform or a board; failing that, follow up to two careers links from ``home`` itself."""
    site = Site()
    fetched: dict[str, httpx.Response | None] = {}
    if not any(visit(polite, url, name, site, fetched) for url in (urls or candidate_urls(home))):
        page = fetched[home] if home in fetched else polite.get(home)  # home may be among urls
        if page is not None and page.status_code < 400:
            for url in career_links(page.text[:MAX_PAGE], str(page.url)):
                if visit(polite, url, name, site):
                    break
    return site
