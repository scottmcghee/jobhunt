"""Survey the S&P 500's careers sites: which hiring platform each company uses, and its boards.

    python scripts/survey_careers.py [OUT_DIR] [--companies PATH] [--delay 1.0] [--limit N]
                                     [--only TICKER ...]

For each constituent (Wikipedia's list), it looks up the company's website (Wikidata), then tries
a few likely careers pages: www.<host>/careers, <host>/careers, careers.<host>, jobs.<host>. If
none of them names a hiring platform, it follows up to two careers links from the homepage. Every
page it reads is checked for platform fingerprints (Workday, Eightfold, Oracle, iCIMS,
SuccessFactors, ...), and every URL in it goes through ``jobhunt.slugs.board_from_url``, so a
board on a source jobhunt supports becomes a companies.yaml entry under the company's real name.

It is polite: one request at a time, ``--delay`` seconds apart, and it honours each host's
robots.txt. Results are saved after every company, so an interrupted run picks up where it
stopped. Outputs, in OUT_DIR (default data/sp500):

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

from jobhunt import config, settings, slugs
from jobhunt.schema import Company

DEFAULT_OUT = Path("data/sp500")
WIKI_URL = "https://en.wikipedia.org/w/index.php"
SPARQL_URL = "https://query.wikidata.org/sparql"
# Constituents still in the index (no end date), with their website and ticker.
SPARQL = """SELECT ?cLabel ?site ?tick WHERE {
  ?c p:P361 ?st . ?st ps:P361 wd:Q242345 . FILTER NOT EXISTS { ?st pq:P582 ?end }
  OPTIONAL { ?c wdt:P856 ?site } OPTIONAL { ?c p:P414 ?ex . ?ex pq:P249 ?tick }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }"""
MAX_PAGE = 2_000_000  # characters of a page to scan

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


@dataclass(frozen=True)
class Constituent:
    ticker: str
    name: str
    sector: str


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


# ------------------------------------------------------------------ inputs


def _link_text(cell: str) -> str:
    """[[target|label]] -> label, [[target]] -> target."""
    return re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", cell).strip()


def parse_constituents(wikitext: str) -> list[Constituent]:
    """The rows of the first table on the 'List of S&P 500 companies' page."""
    start = wikitext.find("{|")
    table = wikitext[start : wikitext.find("|}", start)]
    rows = []
    for row in table.split("\n|-")[1:]:
        lines = [line for line in row.strip().splitlines() if line.strip()]
        if not lines or lines[0].startswith("!"):
            continue
        cells = [c.strip() for c in " ".join(lines).lstrip("|").split("||")]
        if len(cells) < 3:
            continue
        m = re.search(r"\{\{[^|}]*\|([^|}]+)\}\}", cells[0])
        ticker = (m.group(1) if m else cells[0]).strip()
        rows.append(Constituent(ticker, _link_text(cells[1]), _link_text(cells[2])))
    return rows


def parse_sites(sparql: dict) -> dict[str, str]:
    """Ticker -> official website, from the Wikidata query's JSON."""
    sites: dict[str, str] = {}
    for row in sparql.get("results", {}).get("bindings", []):
        tick, site = row.get("tick", {}).get("value"), row.get("site", {}).get("value")
        if tick and site:
            sites.setdefault(tick, site)
    return sites


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
        url = urljoin(base, href.strip())
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or url in links:
            continue
        if _CAREERS.search(parts.path) or (parts.hostname or "").startswith(("jobs.", "careers.")):
            links.append(url)
            if len(links) == limit:
                break
    return links


class _Polite:
    """One request at a time, ``delay`` seconds apart, within each host's robots.txt."""

    def __init__(self, client: httpx.Client, delay: float):
        self.client, self.delay = client, delay
        self.robots: dict[str, RobotFileParser | None] = {}

    def get(self, url: str) -> httpx.Response | None:
        time.sleep(self.delay)
        try:
            return self.client.get(url)
        except httpx.HTTPError:
            return None

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            resp = self.get(f"{origin}/robots.txt")
            rules = None
            if resp is not None and resp.status_code == 200:
                rules = RobotFileParser()
                rules.parse(resp.text.splitlines())
            self.robots[origin] = rules  # no robots.txt: everything is allowed
        rules = self.robots[origin]
        agent = str(self.client.headers.get("user-agent", "*"))
        return rules is None or rules.can_fetch(agent, url)


def _read(page: httpx.Response, company: Constituent) -> tuple[set[str], list[Company]]:
    text = page.text[:MAX_PAGE]
    found = platforms(f"{text} {page.url}")
    boards = []
    for url in [str(page.url), *_URL.findall(text)]:
        if board := slugs.board_from_url(url):
            boards.append(board.model_copy(update={"name": company.name}))
    return found, boards


def survey_company(
    company: Constituent, site: str, client: httpx.Client, delay: float = 1.0
) -> Result:
    polite = _Polite(client, delay)
    result = Result(company.ticker, company.name, company.sector, site, [], [], [])

    def visit(url: str) -> bool:
        if not polite.allowed(url):
            result.skipped_by_robots.append(url)
            return False
        page = polite.get(url)
        if page is None or page.status_code >= 400:
            return False
        found, boards = _read(page, company)
        if not (found or boards):
            return False
        result.pages.append(str(page.url))
        result.platforms = sorted(found)
        seen: set[str] = set()
        for board in boards:
            if (key := board.key.lower()) not in seen:
                seen.add(key)
                result.boards.append(board)
        return True

    if any(visit(url) for url in candidate_urls(site)):
        return result
    if polite.allowed(site) and (home := polite.get(site)) is not None and home.status_code < 400:
        for url in career_links(home.text[:MAX_PAGE], str(home.url)):
            if visit(url):
                break
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
        where = ", ".join(r.platforms) or ("no website found" if r.site is None else "none found")
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
    done = {r.ticker for r in results}
    headers = {"User-Agent": fetch.user_agent}
    with httpx.Client(headers=headers, timeout=fetch.timeout, follow_redirects=True) as client:
        constituents = fetch_constituents(client)
        sites = fetch_sites(client)
        todo = [c for c in constituents if c.ticker not in done]
        if args.only:
            todo = [c for c in todo if c.ticker in set(args.only)]
        for company in todo[: args.limit]:
            site = sites.get(company.ticker) or sites.get(company.ticker.replace(".", ""))
            if site:
                result = survey_company(company, site, client, args.delay)
            else:
                result = Result(company.ticker, company.name, company.sector, None, [], [], [])
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
