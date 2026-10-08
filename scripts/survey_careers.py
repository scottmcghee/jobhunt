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
import time  # noqa: F401 - tests patch survey.time.sleep, which Polite uses
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from jobhunt import config, settings, slugs, throttle
from jobhunt.fingerprint import (  # noqa: F401 - names the survey's callers and tests use
    MAX_PAGE,
    PLATFORMS,
    SITEMAP_SOURCES,
    Polite,
    candidate_urls,
    career_links,
    icims_careers_boards,
    phenom_board,
    phenom_boards,
    platforms,
    sitemap_boards,
    survey_site,
)
from jobhunt.schema import Company

_Polite = Polite  # the name the survey's tests know

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
MAX_ATTEMPTS = 3  # surveys of a company that stays unreachable
# Companies this survey leaves alone, and why.
EXCLUDED = {
    "META": "Meta's terms forbid automated collection",
    "GOOGL": "Google's robots.txt disallows its job pages",
    "GOOG": "Google's robots.txt disallows its job pages",
}


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
    found = survey_site(polite, site, company.name)
    result.pages, result.platforms, result.boards = found.pages, found.platforms, found.boards
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
