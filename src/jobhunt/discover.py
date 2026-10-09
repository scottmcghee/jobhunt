"""Find new job boards: from Common Crawl's URLs on known platforms, and from careers hosts.

    python -m jobhunt.discover [--platforms P ...] [--crawls N] [--no-crawl]
                               [--hosts FILE ...] [--refresh] [--check] [-o OUT]

Two routes, one output (``data/discovered.yaml``, entries to review and paste into
``config/companies.yaml``; boards already there are left out):

- **Known platforms.** Board URLs on Workday, Greenhouse, Lever, Ashby, SmartRecruiters,
  Workable, BambooHR, Eightfold, Gem and Rippling follow patterns, and Common Crawl's index keeps
  each platform's URLs together, so ``commoncrawl.urls`` finds every one a crawl saw for about
  100 MB of index (cached after the first run) plus some tens of MB of index blocks per crawl.
  ``slugs.board_from_url`` turns them into boards (new Workday datacenters included).
  Oracle is opt-in (``--platforms oracle``): its prefix is all of oraclecloud.com.
- **Careers hosts.** A company's own careers site (``careers.acme.com``) says nothing in its URL,
  so each host in ``--hosts`` files (plain hosts, URLs, or lines grepped from a Common Crawl
  ``cluster.idx``) gets the S&P 500 survey's fingerprinting (``fingerprint.survey_site``): its
  page is read for the platform it runs and the board it points at, politely and within
  robots.txt. Classic iCIMS portals (``careers-<company>.icims.com``) are skipped: their robots.txt
  disallows everything. Results are cached per host in ``data/discovery/hosts.json``, so a rerun
  only visits new hosts. A host that failed for now (no answer, or 5xx or 429) is tried again on a
  later run, a day or more after its last try, up to three tries in all; ``--refresh`` visits
  every host again.

``--check`` fetches the first page of each new board and drops those with no open postings, as
``python -m jobhunt.slugs --check`` does; the unchecked list is written first, so an interrupted
check leaves it in place. A board found on a careers host is named after the host
(``careers.acme.com`` -> ``acme``); fix the name when you paste it.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from jobhunt import commoncrawl, config, fingerprint, settings, slugs, storage, throttle
from jobhunt.schema import Company

log = logging.getLogger("jobhunt.discover")

# Each platform's board hosts, for its Common Crawl prefix: (host, its subdomains too).
PLATFORM_HOSTS: dict[str, list[tuple[str, bool]]] = {
    "workday": [("myworkdayjobs.com", True)],
    "greenhouse": [("greenhouse.io", True)],
    "lever": [("jobs.lever.co", False)],
    "ashby": [("jobs.ashbyhq.com", False)],
    "smartrecruiters": [
        ("jobs.smartrecruiters.com", False), ("careers.smartrecruiters.com", False)
    ],
    "workable": [("workable.com", True)],
    "bamboohr": [("bamboohr.com", True)],
    "eightfold": [("eightfold.ai", True)],
    "gem": [("jobs.gem.com", False)],
    "rippling": [("ats.rippling.com", False)],
    "oracle": [("oraclecloud.com", True)],
}
DEFAULT_PLATFORMS = [p for p in PLATFORM_HOSTS if p != "oracle"]
_CLASSIC_ICIMS = re.compile(r"[a-z0-9-]+\.icims\.com")
_SURT_HOST = re.compile(r"([a-z0-9-]+(?:,[a-z0-9-]+)+)(?::\d+)?\)")
_HOST = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
_PREFIXES = ("www.", "careers.", "jobs.", "career.", "job.")
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}  # gamma.co.uk
MAX_ATTEMPTS = 3  # surveys of a careers host that stays unreachable, like the S&P 500 survey's
RETRY_AFTER = timedelta(days=1)  # between two surveys of a host that failed for now


def prefixes(platforms: Iterable[str]) -> list[str]:
    """The SURT prefixes of the platforms' board hosts, each once, in order."""
    found = [commoncrawl.surt_prefix(h, sub) for p in platforms for h, sub in PLATFORM_HOSTS[p]]
    return list(dict.fromkeys(found))


def crawl_boards(
    client: httpx.Client,
    platforms: Sequence[str],
    crawls: int,
    cache_dir: Path,
    known: Iterable[Company] = (),
) -> list[Company]:
    """New boards from the URLs the latest ``crawls`` crawls saw on the platforms' hosts."""
    wanted = prefixes(platforms)
    ids = commoncrawl.latest_crawls(client, crawls)
    urls = itertools.chain.from_iterable(
        commoncrawl.urls(client, crawl, wanted, cache_dir) for crawl in ids
    )
    boards = slugs.discover(urls, known)
    log.info("common crawl %s: %d new boards", ", ".join(ids), len(boards))
    return boards


def _host(line: str) -> str | None:
    """The host a line names: a plain host, a URL, or a Common Crawl index line (SURT)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return (urlsplit(line.split()[0]).hostname or "").lower() or None
    if m := _SURT_HOST.match(line.lower()):
        return ".".join(reversed(m.group(1).split(",")))
    token = line.split()[0].lower().strip("/")
    return token if _HOST.fullmatch(token) else None


def read_hosts(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    """The careers hosts in ``lines``, each once, and the classic iCIMS hosts left out."""
    hosts: dict[str, None] = {}
    skipped: dict[str, None] = {}
    for line in lines:
        if host := _host(line):
            (skipped if _CLASSIC_ICIMS.fullmatch(host) else hosts)[host] = None
    return list(hosts), list(skipped)


def name_from_host(host: str) -> str:
    """A placeholder company name: ``careers.acme.com`` -> ``acme``."""
    labels = host.lower().split(".")
    while len(labels) > 2 and f"{labels[0]}." in _PREFIXES:
        labels = labels[1:]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else labels[0]


def _load_cache(path: Path) -> dict[str, dict]:
    try:
        cache = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        log.warning("%s: not valid JSON; starting a new one", path)
        cache = {}
    return cache if isinstance(cache, dict) else {}


class _Polite(fingerprint.Polite):
    """``fingerprint.Polite`` that also counts failures that mean "try later": a request that got
    no response, or an answer of 5xx or 429."""

    def __init__(self, client: httpx.Client, delay: float):
        super().__init__(client, delay)
        self.transient = 0

    def _fetch(
        self, url: str, follow_redirects: bool, json: dict | None = None
    ) -> httpx.Response | None:
        resp = super()._fetch(url, follow_redirects, json)
        if resp is None or resp.status_code >= 500 or resp.status_code == 429:
            self.transient += 1
        return resp


def _due(entry: dict | None, refresh: bool, now: datetime) -> str:
    """What to do with a host this run: ``survey``, ``cached``, ``waiting`` or ``given up``."""
    if entry is None or refresh:
        return "survey"
    if "failures" not in entry:
        return "cached"
    if entry["failures"] >= MAX_ATTEMPTS:
        return "given up"
    try:
        last = datetime.fromisoformat(entry["failed_at"])
    except (KeyError, TypeError, ValueError):
        return "survey"
    return "survey" if now - last >= RETRY_AFTER else "waiting"


def survey_hosts(
    hosts: Iterable[str],
    client: httpx.Client,
    cache_path: Path,
    delay: float = 1.0,
    refresh: bool = False,
    now: datetime | None = None,
) -> list[Company]:
    """The boards each careers host's site points at; hosts surveyed before come from the cache.

    The cache is saved after every host, so an interrupted run keeps what it found. A host that
    found no boards and had a request fail or answer 5xx or 429 (robots.txt included) is cached as
    a failure: it's tried again a day or more later, ``MAX_ATTEMPTS`` times in all. With
    ``refresh`` every host is surveyed again, and one that fails keeps its cached boards.
    Each host is stamped with the time it was tried (``now``, if given, stands in for the clock).
    """
    cache = _load_cache(cache_path)
    hosts = list(hosts)
    start = now or datetime.now(UTC)
    plan = {host: _due(cache.get(host), refresh, start) for host in hosts}
    counts = Counter(plan.values())
    log.info(
        "%d careers hosts: %d to survey, %d cached, %d failed for now (tried again after a day), "
        "%d given up after %d tries (--refresh tries them all)",
        len(hosts), counts["survey"], counts["cached"], counts["waiting"], counts["given up"],
        MAX_ATTEMPTS,
    )
    boards: list[Company] = []
    surveyed = 0
    for host in hosts:
        entry = cache.get(host)
        if plan[host] == "survey":
            surveyed += 1
            progress = f"[{surveyed}/{counts['survey']}] {host}"
            polite = _Polite(client, delay)
            home = f"https://{host}/"
            site = fingerprint.survey_site(polite, home, name_from_host(host), urls=[home])
            tried_at = (now or datetime.now(UTC)).isoformat()
            if polite.transient and not site.boards:
                errors = ", ".join(polite.errors) or "5xx or 429"
                log.warning("%s: unreachable (%s)", progress, errors)
                if entry is None or "failures" in entry:  # else (refresh): keep its cached boards
                    entry = {
                        "failed_at": tried_at,
                        "failures": (entry or {}).get("failures", 0) + 1,
                        "errors": polite.errors,
                    }
            else:
                entry = {
                    "surveyed_at": tried_at,
                    "platforms": site.platforms,
                    "pages": site.pages,
                    "boards": [b.model_dump(exclude_defaults=True) for b in site.boards],
                }
                log.info("%s: %s", progress, ", ".join(entry["platforms"]) or "no platform found")
            cache[host] = entry
            storage._write_atomic(cache_path, json.dumps(cache, indent=1, sort_keys=True))
        for raw in (entry or {}).get("boards") or []:
            try:
                boards.append(Company.model_validate(raw))
            except ValueError:  # a cached board this version no longer accepts
                continue
    storage._write_atomic(cache_path, json.dumps(cache, indent=1, sort_keys=True))
    return boards


def _new(boards: Iterable[Company], known: Iterable[Company]) -> list[Company]:
    """``boards`` minus ``known`` and repeats (compared as ``slugs.discover`` does), sorted."""
    seen = {slugs._dedupe_key(c) for c in known}
    found = []
    for board in boards:
        if (key := slugs._dedupe_key(board)) not in seen:
            seen.add(key)
            found.append(board)
    return sorted(found, key=lambda c: (c.ats, c.slug.lower()))


def _host_client(fetch: settings.FetchSettings) -> httpx.Client:
    """One request at a time, like the survey; no retries (a guessed host often doesn't exist)."""
    transport = throttle.ThrottledTransport(
        start=1,
        ceiling=1,
        max_in_flight=1,
        max_retries=fetch.max_retries,
        max_retry_after=fetch.max_retry_after,
        transient_retries=0,
        connect_retries=0,
    )
    return httpx.Client(
        transport=transport,
        timeout=fetch.timeout,
        headers={"User-Agent": fetch.user_agent},
        cookies=throttle.no_cookies(),
    )


def _at_least_one(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {number}")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobhunt.discover", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platforms", nargs="+", choices=list(PLATFORM_HOSTS),
                        default=DEFAULT_PLATFORMS, metavar="P",
                        help=f"platforms to find in Common Crawl (default: all but oracle; "
                             f"choices: {', '.join(PLATFORM_HOSTS)})")
    parser.add_argument("--crawls", type=_at_least_one, default=1, metavar="N",
                        help="how many of the latest crawls to read (default 1)")
    parser.add_argument("--no-crawl", action="store_true", help="skip Common Crawl")
    parser.add_argument("--hosts", nargs="+", type=Path, default=[], metavar="FILE",
                        help="files of careers hosts, URLs or cluster.idx lines to fingerprint")
    parser.add_argument("--refresh", action="store_true", help="survey cached hosts again")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between requests to careers sites (default 1)")
    parser.add_argument("--check", action="store_true",
                        help="fetch the first page of each new board; drop those with no postings")
    parser.add_argument("--companies", type=Path, help="existing companies.yaml to skip")
    parser.add_argument("--data-dir", type=Path, default=storage.DEFAULT_DATA_DIR)
    parser.add_argument("-o", "--out", type=Path, help="default: <data-dir>/discovered.yaml")
    args = parser.parse_args(argv)

    if args.no_crawl and not args.hosts:
        log.error("nothing to do: --no-crawl and no --hosts")
        return 2
    if missing := [p for p in args.hosts if not p.is_file()]:
        log.error("no such hosts file: %s", ", ".join(map(str, missing)))
        return 2
    try:
        tunables = settings.load()
    except settings.SettingsError as e:
        log.error("%s", e)
        return 2
    known = config.load_companies(args.companies)
    out = args.out or args.data_dir / "discovered.yaml"
    try:
        return _discover(args, tunables, known, out)
    except KeyboardInterrupt:
        log.error("interrupted: nothing written (careers hosts surveyed so far are cached)")
        return 130


def _discover(
    args: argparse.Namespace, tunables: settings.Settings, known: list[Company], out: Path
) -> int:
    fetch = tunables.fetch
    found: list[Company] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with slugs._client(fetch) as client:
        if not args.no_crawl:
            cache_dir = args.data_dir / "commoncrawl"
            found += crawl_boards(client, args.platforms, args.crawls, cache_dir, known)
        if args.hosts:
            lines = itertools.chain.from_iterable(p.read_text().splitlines() for p in args.hosts)
            hosts, skipped = read_hosts(lines)
            if skipped:
                log.info("skipped %d classic iCIMS hosts (robots.txt disallows them)", len(skipped))
            with _host_client(fetch) as host_client:
                cache = args.data_dir / "discovery" / "hosts.json"
                found += survey_hosts(hosts, host_client, cache, args.delay, args.refresh)
        found = _new(found, known)
        if args.check:
            out.write_text(slugs.render(found))
            log.info("%d new boards -> %s (unchecked until the check finishes)", len(found), out)
            try:
                checked = slugs.check(found, client, tunables.slugs.check_workers)
            except KeyboardInterrupt:
                log.error("interrupted: the unchecked boards are in %s", out)
                return 130
            log.info("checked %d boards: %d dropped", len(found), len(found) - len(checked))
            found = checked
    out.write_text(slugs.render(found))
    log.info("%d new boards -> %s", len(found), out)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    log.setLevel(logging.INFO)
    slugs.log.setLevel(logging.INFO)  # --check's progress and the boards it drops
    raise SystemExit(main())
