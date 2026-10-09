"""Find new job boards: from Common Crawl's URLs on known platforms, and from careers hosts.

    python -m jobhunt.discover [--platforms P ...] [--crawls N] [--no-crawl]
                               [--hosts FILE ...] [--webgraph] [--limit N] [--refresh]
                               [--check] [-o OUT]

Two routes, one output (``data/discovered.yaml``, entries to review and paste into
``config/companies.yaml``; boards already there are left out):

- **Known platforms.** Board URLs on Workday, Greenhouse, Lever, Ashby, SmartRecruiters,
  Workable, BambooHR, Eightfold, Gem and Rippling follow patterns, and Common Crawl's index keeps
  each platform's URLs together, so ``commoncrawl.urls`` finds every one a crawl saw for about
  100 MB of index (cached after the first run) plus some tens of MB of index blocks per crawl.
  ``slugs.board_from_url`` turns them into boards (new Workday datacenters included).
  Oracle is opt-in (``--platforms oracle``): its prefix is all of oraclecloud.com.
- **Careers hosts.** A company's own careers site (``careers.acme.com``) says nothing in its URL, so
  each host in ``--hosts`` files (plain hosts, URLs, or lines grepped from a Common Crawl
  ``cluster.idx``), and with ``--webgraph`` each careers host in Common Crawl's latest web graph
  (``commoncrawl.webgraph_hosts``: first label in ``discover.webgraph_labels``, top-level domain
  generic or in ``discover.webgraph_country_tlds``; see settings.yaml), gets the S&P 500 survey's
  fingerprinting (``fingerprint.survey_site``): its page is read for the platform it runs and the
  board it points at, politely and within robots.txt. Classic iCIMS portals
  (``careers-<company>.icims.com``) are skipped: their robots.txt disallows everything. So are hosts
  under ``EXCLUDED_DOMAINS`` (Meta's, whose terms forbid automated collection, and Google's),
  without a request. Results are cached per host in ``data/discovery/hosts.json``, so a rerun only
  visits new hosts. A host that failed for now (no answer, or 5xx or 429) is tried again on a later
  run, a day or more after its last try, up to three tries in all (``discover`` in settings.yaml
  changes both); ``--refresh`` visits every host again. ``discover.survey_workers`` hosts are
  surveyed at once, each one request at a time; ``--limit N`` surveys at most N this run, least
  recently tried first, and leaves the rest for the next.

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
import threading
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

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
# Careers hosts never surveyed: a registrable domain (and its subdomains) -> why. A policy, not a
# tunable (CLAUDE.md's off-limits list).
_META = "Meta's terms forbid automated collection without written permission"
_GOOGLE = "Google's robots.txt disallows its job pages"
EXCLUDED_DOMAINS = {
    "facebook.com": _META, "meta.com": _META, "metacareers.com": _META,
    "instagram.com": _META, "whatsapp.com": _META, "oculus.com": _META,
    "fb.com": _META, "workplace.com": _META, "messenger.com": _META,
    "threads.net": _META, "threads.com": _META,
    "google.com": _GOOGLE, "youtube.com": _GOOGLE,
}


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


def excluded(host: str) -> str | None:
    """Why the host must not be surveyed, if it is or is under one of ``EXCLUDED_DOMAINS``."""
    labels = host.lower().rstrip(".").split(".")
    for n in range(len(labels) - 1):
        if reason := EXCLUDED_DOMAINS.get(".".join(labels[n:])):
            return reason
    return None


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
    no response, or an answer of 5xx or 429. Its wait between requests ends early once ``stop``
    is set, and raises ``throttle.Stopped``."""

    def __init__(self, client: httpx.Client, delay: float, stop: threading.Event):
        super().__init__(client, 0)  # the wait is ours, so a stop cuts it short
        self.pause, self.stop = delay, stop
        self.transient = 0

    def _fetch(
        self, url: str, follow_redirects: bool, json: dict | None = None
    ) -> httpx.Response | None:
        """Redirects are followed by hand, so a hop to a host under ``EXCLUDED_DOMAINS`` (a
        robots.txt redirect, say) is never sent: it gets no answer, which is not a failure."""
        for _ in range(fingerprint.MAX_REDIRECTS + 1):
            if excluded(urlsplit(url).hostname or ""):
                self.skipped.append(url)
                return None
            if self.stop.wait(self.pause):
                raise throttle.Stopped
            resp = super()._fetch(url, False, json)
            if resp is None or resp.status_code >= 500 or resp.status_code == 429:
                self.transient += 1
            if not follow_redirects or resp is None or not resp.is_redirect:
                return resp
            if "location" not in resp.headers:
                return resp
            try:
                url = urljoin(url, resp.headers["location"])
            except ValueError:  # a malformed Location: it is the answer
                return resp
        self.errors.append(f"{url}: too many redirects")
        self.transient += 1  # as when httpx followed them and gave up
        return None

    def allowed(self, url: str) -> bool:
        """A host under ``EXCLUDED_DOMAINS`` (one a site redirects to) gets no request at all,
        not even for its robots.txt."""
        if excluded(urlsplit(url).hostname or ""):
            return False
        return super().allowed(url)


def _due(
    entry: dict | None, refresh: bool, now: datetime, rules: settings.DiscoverSettings
) -> str:
    """What to do with a host this run: ``survey``, ``cached``, ``waiting`` or ``given up``."""
    if entry is None or refresh:
        return "survey"
    if "failures" not in entry:
        return "cached"
    if entry["failures"] >= rules.max_attempts:
        return "given up"
    try:
        last = datetime.fromisoformat(entry["failed_at"])
    except (KeyError, TypeError, ValueError):
        return "survey"
    wait = timedelta(hours=rules.retry_after_hours)
    return "survey" if now - last >= wait else "waiting"


def _survey_one(
    client: httpx.Client, host: str, delay: float, now: datetime | None, stop: threading.Event
) -> tuple[fingerprint.Site, _Polite, str]:
    """Survey one careers host: its requests go one at a time, ``delay`` apart."""
    polite = _Polite(client, delay, stop)
    home = f"https://{host}/"
    site = fingerprint.survey_site(polite, home, name_from_host(host), urls=[home])
    return site, polite, (now or datetime.now(UTC)).isoformat()


def _entry(
    entry: dict | None, site: fingerprint.Site, polite: _Polite, tried_at: str, progress: str
) -> dict | None:
    """The host's new cache entry after a survey (``entry`` is its old one)."""
    if polite.transient and not site.boards:
        log.warning("%s: unreachable (%s)", progress, ", ".join(polite.errors) or "5xx or 429")
        if entry is not None and "failures" not in entry:  # refresh: keep its cached boards
            return {**entry, "refresh_failed_at": tried_at}  # and when it was tried, for --limit
        return {
            "failed_at": tried_at,
            "failures": (entry or {}).get("failures", 0) + 1,
            "errors": polite.errors,
        }
    log.info("%s: %s", progress, ", ".join(site.platforms) or "no platform found")
    return {
        "surveyed_at": tried_at,
        "platforms": site.platforms,
        "pages": site.pages,
        "boards": [b.model_dump(exclude_defaults=True) for b in site.boards],
    }


def survey_hosts(
    hosts: Iterable[str],
    client: httpx.Client,
    cache_path: Path,
    delay: float = 1.0,
    refresh: bool = False,
    now: datetime | None = None,
    rules: settings.DiscoverSettings | None = None,
    workers: int = 1,
    limit: int | None = None,
) -> list[Company]:
    """The boards each careers host's site points at; hosts surveyed before come from the cache.

    The cache is saved every ``rules.save_every_seconds``, at the end, and on an interrupt (which
    stops the requests under way first), so an interrupted run keeps what it found. A host that
    found no boards and had a request fail or answer 5xx or 429 (robots.txt included) is cached as a
    failure: it's tried again ``rules.retry_after_hours`` or more later, ``rules.max_attempts``
    times in all (``rules`` defaults to the settings' defaults). With ``refresh`` every host is
    surveyed again, and one that fails keeps its cached boards. Hosts due a survey go least recently
    tried first, so a ``limit`` works through them all over several runs. Hosts under
    ``EXCLUDED_DOMAINS`` are skipped without a request. Each host is stamped with the time it was
    tried (``now``, if given, stands in for the clock).

    ``workers`` hosts are surveyed at once, each with its own requests one at a time. ``limit``
    surveys at most that many hosts this run; the rest are left for a later one.
    """
    rules = rules or settings.DiscoverSettings()
    cache = _load_cache(cache_path)
    hosts = list(hosts)
    if forbidden := [host for host in hosts if excluded(host)]:
        log.info("skipped %d hosts whose owners forbid it (EXCLUDED_DOMAINS)", len(forbidden))
        hosts = [host for host in hosts if not excluded(host)]
    start = now or datetime.now(UTC)
    plan = {host: _due(cache.get(host), refresh, start, rules) for host in hosts}
    todo = [host for host in hosts if plan[host] == "survey"]
    todo.sort(key=lambda host: _last_tried(cache.get(host)))  # stable: input order breaks ties
    later = todo[limit:] if limit is not None else []
    todo = todo[: len(todo) - len(later)]
    counts = Counter(plan.values())
    log.info(
        "%d careers hosts: %d to survey, %d cached, %d failed for now (tried again after %g h), "
        "%d given up after %d tries (--refresh tries them all), %d left for a later run",
        len(hosts), len(todo), counts["cached"], counts["waiting"], rules.retry_after_hours,
        counts["given up"], rules.max_attempts, len(later),
    )
    stop = threading.Event()
    handled: set = set()
    before = dict(cache)  # entries as they were, so an interrupt can't count a host twice
    saved = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(_survey_one, client, host, delay, now, stop): host for host in todo}
        try:
            for done, future in enumerate(as_completed(futures), 1):
                host = futures[future]
                progress = f"[{done}/{len(todo)}] {host}"
                if (entry := _entry(before.get(host), *future.result(), progress)) is not None:
                    cache[host] = entry
                handled.add(future)  # only once stored, so an interrupt before here still keeps it
                if time.monotonic() - saved >= rules.save_every_seconds:
                    _save(cache_path, cache)
                    saved = time.monotonic()
        except BaseException:  # Ctrl-C: stop the requests under way, keep the hosts done
            stop.set()
            transport = getattr(client, "_transport", None)
            if isinstance(transport, throttle.ThrottledTransport):
                transport.stop()
            pool.shutdown(wait=True, cancel_futures=True)
            for future, host in futures.items():
                if future in handled or future.cancelled() or future.exception() is not None:
                    continue  # a host stopped midway is neither surveyed nor a failure
                progress = f"[interrupted] {host}"
                if (entry := _entry(before.get(host), *future.result(), progress)) is not None:
                    cache[host] = entry
            _save(cache_path, cache)
            raise
    boards: list[Company] = []
    for host in hosts:
        for raw in (cache.get(host) or {}).get("boards") or []:
            try:
                boards.append(Company.model_validate(raw))
            except ValueError:  # a cached board this version no longer accepts
                continue
    _save(cache_path, cache)
    return boards


def _save(path: Path, cache: dict[str, dict]) -> None:
    storage._write_atomic(path, json.dumps(cache, indent=1, sort_keys=True))


def _last_tried(entry: dict | None) -> str:
    """When the host was last tried, as an ISO string; "" (first) if never."""
    entry = entry or {}
    return max(str(entry.get(k) or "") for k in ("surveyed_at", "failed_at", "refresh_failed_at"))


def _new(boards: Iterable[Company], known: Iterable[Company]) -> list[Company]:
    """``boards`` minus ``known`` and repeats (compared as ``slugs.discover`` does), sorted."""
    seen = {slugs._dedupe_key(c) for c in known}
    found = []
    for board in boards:
        if (key := slugs._dedupe_key(board)) not in seen:
            seen.add(key)
            found.append(board)
    return sorted(found, key=lambda c: (c.ats, c.slug.lower()))


def _webgraph_hosts(
    client: httpx.Client, rules: settings.DiscoverSettings, cache_dir: Path
) -> list[str]:
    """Careers hosts from the latest web graph, under a generic or listed top-level domain."""
    release = commoncrawl.latest_graph(client)
    found = commoncrawl.webgraph_hosts(client, release, rules.webgraph_labels, cache_dir)
    hosts = [
        h for h in found
        if commoncrawl.generic_or_listed_tld(
            h, rules.webgraph_country_tlds, rules.webgraph_generic_tlds
        )
    ]
    log.info("web graph %s: %d careers hosts (%d under other top-level domains left out)",
             release, len(hosts), len(found) - len(hosts))
    return hosts


def _host_client(fetch: settings.FetchSettings, workers: int = 1) -> httpx.Client:
    """One request at a time per host, ``workers`` hosts at once; no retries (a guessed host
    often doesn't exist)."""
    transport = throttle.ThrottledTransport(
        start=1,
        ceiling=1,
        max_in_flight=workers,
        max_retries=fetch.max_retries,
        max_retry_after=fetch.max_retry_after,
        transient_retries=0,
        connect_retries=0,
        max_rate=fetch.max_rate,  # platforms many careers hosts redirect to, like Workable
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
    parser.add_argument("--webgraph", action="store_true",
                        help="careers hosts from Common Crawl's latest web graph "
                             "(discover.webgraph_labels and webgraph_country_tlds in settings)")
    parser.add_argument("--limit", type=_at_least_one, metavar="N",
                        help="survey at most N careers hosts this run; the rest wait for the next")
    parser.add_argument("--refresh", action="store_true", help="survey cached hosts again")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between requests to careers sites (default 1)")
    parser.add_argument("--check", action="store_true",
                        help="fetch the first page of each new board; drop those with no postings")
    parser.add_argument("--companies", type=Path, help="existing companies.yaml to skip")
    parser.add_argument("--data-dir", type=Path, default=storage.DEFAULT_DATA_DIR)
    parser.add_argument("-o", "--out", type=Path, help="default: <data-dir>/discovered.yaml")
    args = parser.parse_args(argv)

    if args.no_crawl and not args.hosts and not args.webgraph:
        log.error("nothing to do: --no-crawl and no --hosts or --webgraph")
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
        lines: list[str] = []
        for path in args.hosts:
            lines += path.read_text().splitlines()
        if args.webgraph:
            lines += _webgraph_hosts(client, tunables.discover, args.data_dir / "commoncrawl")
        if lines:
            hosts, skipped = read_hosts(lines)
            if skipped:
                log.info("skipped %d classic iCIMS hosts (robots.txt disallows them)", len(skipped))
            workers = tunables.discover.survey_workers
            with _host_client(fetch, workers) as host_client:
                cache = args.data_dir / "discovery" / "hosts.json"
                found += survey_hosts(
                    hosts, host_client, cache, args.delay, args.refresh,
                    rules=tunables.discover, workers=workers, limit=args.limit,
                )
        found = _new(found, known)
        if args.check:
            out.write_text(slugs.render(found))
            log.info("%d new boards -> %s (unchecked until the check finishes)", len(found), out)
            try:
                checked = slugs.check(
                    found, client, tunables.slugs.check_workers, tunables.slugs.check_progress_every
                )
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
