"""Survey the S&P 500's careers sites: which hiring platform each company uses, and its boards.

    python scripts/survey_careers.py [OUT_DIR] [--companies PATH] [--delay 1.0] [--limit N]
                                     [--only TICKER ...]

For each constituent (Wikipedia's list), it looks up the company's website (Wikidata, by the
row's Wikipedia article, else by ticker), then tries
a few likely careers pages: www.<host>/careers, <host>/careers, careers.<host>, jobs.<host>. If
none of them names a hiring platform, it follows up to two careers links from the homepage. Every
page it reads is checked for platform fingerprints (Workday, Eightfold, Oracle, iCIMS,
SuccessFactors, ...), and every URL in it goes through ``jobhunt.slugs.board_from_url``, so a
board on a source jobhunt supports becomes a companies.yaml entry under the company's real name.

It is polite: one request at a time, ``--delay`` seconds apart, and it honours each host's
robots.txt, redirect by redirect (RFC 9309: a robots.txt that answers 5xx or can't be fetched
disallows everything). Companies in EXCLUDED get no requests at all. Results are saved after every
company, so an interrupted run picks up where it stopped; companies that were unreachable are
tried again after the new ones (three attempts in all), and ``--only`` re-surveys the given
tickers. Outputs, in OUT_DIR (default data/sp500):

    survey.md                  platform counts, and one row per company
    companies.generated.yaml   boards not in companies.yaml yet; review, then paste
    results.json               the raw results (lets a run resume)
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from jobhunt import config, settings, slugs, throttle
from jobhunt.schema import Company
from jobhunt.sources import _sitemap, paradox, phenom, radancy

DEFAULT_OUT = Path("data/sp500")
WIKI_URL = "https://en.wikipedia.org/w/index.php"
WIKI_API = "https://en.wikipedia.org/w/api.php"
SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
# Constituents still in the index (no end date), with their website and ticker.
SPARQL = """SELECT ?cLabel ?site ?tick WHERE {
  ?c p:P361 ?st . ?st ps:P361 wd:Q242345 . FILTER NOT EXISTS { ?st pq:P582 ?end }
  OPTIONAL { ?c wdt:P856 ?site } OPTIONAL { ?c p:P414 ?ex . ?ex pq:P249 ?tick }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }"""
MAX_PAGE = 2_000_000  # characters of a page to scan
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 3  # surveys of a company that stays unreachable
# Companies this survey leaves alone, and why.
EXCLUDED = {
    "META": "Meta's terms forbid automated collection",
    "GOOGL": "Google's robots.txt disallows its job pages",
    "GOOG": "Google's robots.txt disallows its job pages",
}
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


@dataclass(frozen=True)
class Constituent:
    ticker: str
    name: str
    sector: str
    article: str = ""  # the row's Wikipedia link target


@dataclass
class Result:
    ticker: str
    name: str
    sector: str
    site: str | None
    pages: list[str]  # the page(s) the platform or boards were found on
    platforms: list[str]
    boards: list[Company]
    skipped_by_robots: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # "<url>: <exception type>"
    status: str = ""  # "", "unreachable" (no response at all), or "excluded: <reason>"
    attempts: int = 0  # surveys so far


# ------------------------------------------------------------------ inputs


def _link_text(cell: str) -> str:
    """[[target|label]] -> label, [[target]] -> target."""
    return re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", cell).strip()


def _link_target(cell: str) -> str:
    """[[target|label]] -> target."""
    m = re.search(r"\[\[([^|\]]*)", cell)
    return m.group(1).strip() if m else ""


def parse_constituents(wikitext: str) -> list[Constituent]:
    """The rows of the first table on the 'List of S&P 500 companies' page."""
    start = wikitext.find("{|")
    table = wikitext[start : wikitext.find("|}", start)]
    rows = []
    for row in table.split("\n|-")[1:]:
        lines = [line for line in row.strip().splitlines() if line.strip()]
        if not lines or lines[0].startswith("!"):
            continue
        cells = [c.strip(" |") for c in " ".join(lines).lstrip("|").split("||")]
        if len(cells) < 3:
            continue
        m = re.search(r"\{\{[^|}]*\|([^|}]+)\}\}", cells[0])
        ticker = (m.group(1) if m else cells[0]).strip()
        name, sector, article = _link_text(cells[1]), _link_text(cells[2]), _link_target(cells[1])
        rows.append(Constituent(ticker, name, sector, article))
    return rows


def parse_sites(sparql: dict) -> dict[str, str]:
    """Ticker -> official website, from the Wikidata query's JSON."""
    sites: dict[str, str] = {}
    for row in sparql.get("results", {}).get("bindings", []):
        tick, site = row.get("tick", {}).get("value"), row.get("site", {}).get("value")
        if tick and site:
            sites.setdefault(tick, site)
    return sites


def _title(article: str) -> str:
    """A link target as MediaWiki stores it: spaces, not underscores; first letter upper case."""
    title = " ".join(article.replace("_", " ").split())
    return title[:1].upper() + title[1:]


def parse_item_ids(query: dict, titles: Iterable[str]) -> dict[str, str]:
    """Title asked for -> Wikidata item, through Wikipedia's title normalization and redirects."""
    body = query.get("query", {})
    normalized = {n["from"]: n["to"] for n in body.get("normalized", [])}
    redirects = {r["from"]: r["to"] for r in body.get("redirects", [])}
    items = {p["title"]: p["pageprops"]["wikibase_item"]
             for p in body.get("pages", []) if "wikibase_item" in p.get("pageprops", {})}
    ids = {}
    for title in titles:
        page = normalized.get(title, title)
        if item := items.get(redirects.get(page, page)):
            ids[title] = item
    return ids


def parse_item_sites(entities: dict) -> dict[str, str]:
    """Wikidata item -> official website (the first), from a wbgetentities response."""
    sites: dict[str, str] = {}
    for item, entity in entities.get("entities", {}).items():
        values = [c.get("mainsnak", {}).get("datavalue", {}).get("value")
                  for c in entity.get("claims", {}).get("P856", [])]
        if site := next((v for v in values if v), None):
            sites[item] = site
    return sites


def fetch_title_sites(client: httpx.Client, articles: Iterable[str]) -> dict[str, str]:
    """Websites by Wikipedia article, 50 titles or items a call (the APIs' limit).

    Wikipedia resolves each title (redirects too) to its Wikidata item; Wikidata has the website.
    """
    titles = list(dict.fromkeys(_title(a) for a in articles if a))
    ids: dict[str, str] = {}
    for i in range(0, len(titles), 50):
        batch = titles[i : i + 50]
        params = {"action": "query", "titles": "|".join(batch), "redirects": "1", "format": "json",
                  "formatversion": "2", "prop": "pageprops", "ppprop": "wikibase_item"}
        resp = client.get(WIKI_API, params=params)
        resp.raise_for_status()
        ids.update(parse_item_ids(resp.json(), batch))
    items = list(dict.fromkeys(ids.values()))
    sites: dict[str, str] = {}
    for i in range(0, len(items), 50):
        params = {"action": "wbgetentities", "ids": "|".join(items[i : i + 50]), "props": "claims",
                  "format": "json"}
        resp = client.get(WIKIDATA_API, params=params)
        resp.raise_for_status()
        sites.update(parse_item_sites(resp.json()))
    return {title: sites[item] for title, item in ids.items() if item in sites}


def fetch_constituents(client: httpx.Client) -> list[Constituent]:
    params = {"title": "List_of_S&P_500_companies", "action": "raw"}
    resp = client.get(WIKI_URL, params=params)
    resp.raise_for_status()
    return parse_constituents(resp.text)


def fetch_sites(client: httpx.Client) -> dict[str, str]:
    headers = {"Accept": "application/sparql-results+json"}
    resp = client.get(SPARQL_URL, params={"query": SPARQL}, headers=headers, timeout=120)
    resp.raise_for_status()
    return parse_sites(resp.json())


# ------------------------------------------------------------------ one company


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


class _Polite:
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


def _read(page: httpx.Response, company: Constituent) -> tuple[set[str], list[Company]]:
    text = page.text[:MAX_PAGE]
    found = platforms(f"{text} {page.url}")
    boards = []
    for url in [str(page.url), *map(slugs._trim, _URL.findall(text))]:
        if board := slugs.board_from_url(url):
            boards.append(board.model_copy(update={"name": company.name}))
    if "successfactors" in found and _CAREER_SITE_BUILDER.search(text):
        host = (page.url.host or "").lower()
        try:
            boards.append(Company(name=company.name, ats="successfactors", slug=host))
        except ValueError:  # not a host Company accepts
            pass
    return found, boards


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


def phenom_boards(polite: _Polite, url: str, name: str) -> list[Company]:
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


def _first_job_url(polite: _Polite, host: str, job_url: _sitemap.JobUrl) -> str | None:
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


def sitemap_boards(polite: _Polite, url: str, name: str, ats: str) -> list[Company]:
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


def survey_company(
    company: Constituent, site: str | None, client: httpx.Client, delay: float = 1.0
) -> Result:
    result = Result(company.ticker, company.name, company.sector, site, [], [], [])
    if reason := EXCLUDED.get(company.ticker):
        result.status = f"excluded: {reason}"
        return result
    if site is None:
        return result
    polite = _Polite(client, delay)
    result.skipped_by_robots, result.errors = polite.skipped, polite.errors

    def visit(url: str) -> bool:
        page = polite.get(url)
        if page is None or page.status_code >= 400:
            return False
        found, boards = _read(page, company)
        if not (found or boards):
            return False
        if "phenom" in found and not boards:
            boards = phenom_boards(polite, str(page.url), company.name)
        for ats in SITEMAP_SOURCES:
            if ats in found and not boards:
                boards = sitemap_boards(polite, str(page.url), company.name, ats)
        result.pages.append(str(page.url))
        result.platforms = sorted(found)
        seen: set[str] = set()
        for board in boards:
            if (key := board.key.lower()) not in seen:
                seen.add(key)
                result.boards.append(board)
        return True

    if not any(visit(url) for url in candidate_urls(site)):
        if (home := polite.get(site)) is not None and home.status_code < 400:
            for url in career_links(home.text[:MAX_PAGE], str(home.url)):
                if visit(url):
                    break
    if polite.errors and not polite.responses:
        result.status = "unreachable"
    return result


# ------------------------------------------------------------------ outputs


def new_boards(results: Iterable[Result], known: Iterable[Company]) -> list[Company]:
    """Boards found that companies.yaml doesn't have, once each (Oracle: once per host)."""
    seen = {slugs._dedupe_key(c) for c in known}
    fresh = []
    for result in results:
        for board in result.boards:
            if (key := slugs._dedupe_key(board)) not in seen:
                seen.add(key)
                fresh.append(board)
    return fresh


def report(results: Sequence[Result], known: Iterable[Company]) -> str:
    known_keys = {slugs._dedupe_key(c) for c in known}
    counts = collections.Counter(p for r in results for p in r.platforms)
    lines = [
        "# S&P 500 careers survey",
        "",
        f"{len(results)} companies; {sum(bool(r.platforms) for r in results)} with a platform.",
        "",
        "| platform | companies |",
        "|---|---|",
        *(f"| {name} | {n} |" for name, n in counts.most_common()),
        "",
        "| ticker | company | platforms | boards |",
        "|---|---|---|---|",
    ]
    for r in results:
        where = ", ".join(r.platforms) or r.status
        where = where or ("no website found" if r.site is None else "none found")
        boards = "; ".join(
            f"{b.ats} {b.slug}" + (" (in config)" if slugs._dedupe_key(b) in known_keys else "")
            for b in r.boards
        )
        lines.append(f"| {r.ticker} | {r.name} | {where} | {boards} |")
    return "\n".join(lines) + "\n"


def _save(results: Sequence[Result], path: Path) -> None:
    rows = [{**asdict(r), "boards": [b.model_dump() for b in r.boards]} for r in results]
    path.write_text(json.dumps(rows, indent=1) + "\n")


def _load(path: Path) -> list[Result]:
    if not path.exists():
        return []
    rows = json.loads(path.read_text())
    return [Result(**{**r, "boards": [Company(**b) for b in r["boards"]]}) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="survey_careers", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", nargs="?", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--companies", type=Path, help="companies.yaml to compare against")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--limit", type=int, help="survey at most N more companies")
    parser.add_argument("--only", nargs="+", metavar="TICKER", help="survey just these")
    args = parser.parse_args(argv)

    try:
        fetch = settings.load().fetch
    except settings.SettingsError as e:
        print(e, file=sys.stderr)
        return 2
    known = config.load_companies(args.companies)
    args.out.mkdir(parents=True, exist_ok=True)
    saved = args.out / "results.json"
    results = _load(saved)
    tried = {r.ticker: r for r in results}
    retry = {t for t, r in tried.items() if r.status == "unreachable" and r.attempts < MAX_ATTEMPTS}
    headers = {"User-Agent": fetch.user_agent}
    # Through fetch's throttle: one request at a time, and 429s (Wikidata answers a burst of
    # batches with one) are retried after Retry-After or a backoff instead of ending the run.
    # No transient or connect retries: guesses like careers.<host> often don't resolve, and
    # retrying each would add seconds (up to fetch.timeout per attempt) per company.
    transport = throttle.ThrottledTransport(
        start=1,
        ceiling=1,
        max_in_flight=1,
        max_retries=fetch.max_retries,
        max_retry_after=fetch.max_retry_after,
        transient_retries=0,
        connect_retries=0,
    )
    with httpx.Client(
        headers=headers,
        timeout=fetch.timeout,
        follow_redirects=True,
        transport=transport,
        cookies=throttle.no_cookies(),
    ) as client:
        constituents = fetch_constituents(client)
        by_title = fetch_title_sites(client, [c.article for c in constituents])
        sites = fetch_sites(client)
        if args.only:  # these, even if done before
            todo = [c for c in constituents if c.ticker in set(args.only)]
        else:
            todo = [c for c in constituents if c.ticker not in tried]  # new ones first
            todo += [c for c in constituents if c.ticker in retry]
        for company in todo[: args.limit]:
            site = by_title.get(_title(company.article)) or sites.get(company.ticker)
            result = survey_company(company, site, client, args.delay)
            result.attempts = (tried[company.ticker].attempts if company.ticker in tried else 0) + 1
            rows = [r.ticker for r in results]
            if company.ticker in rows:
                results[rows.index(company.ticker)] = result
            else:
                results.append(result)
            _save(results, saved)
            found = ", ".join(result.platforms) or "-"
            print(f"{company.ticker:<6} {company.name[:40]:<40} {found}")
    (args.out / "survey.md").write_text(report(results, known))
    (args.out / "companies.generated.yaml").write_text(slugs.render(new_boards(results, known)))
    print(f"\n{len(results)} companies surveyed -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
