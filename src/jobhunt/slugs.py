"""Harvest ATS board slugs from Common Crawl index files, or any other text.

    python -m jobhunt.slugs [INDEX ...] [--companies PATH] [-o OUT] [--check]

Each INDEX is any text file: every http(s) URL in it is checked for a job board. Lines grepped
from Common Crawl index files (``zgrep myworkdayjobs cdx-*.gz``), JSON records, plain URL lists,
and saved HTML all work. The output is a block of entries ready to paste under ``companies:`` in
config/companies.yaml. Slugs already listed there are left out, so the output can be regenerated
and re-pasted as more data arrives.

Without ``--check`` this runs offline. With it, the first page of each new board is fetched
through its source adapter (no descriptions), and the board is dropped if it has no open postings
or answers with a 4xx other than 429: a SmartRecruiters identifier with no postings, say, or a
Greenhouse slug that 404s. Boards that time out, are rate limited, or fail with a 5xx are kept.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import yaml

from jobhunt import config, settings
from jobhunt.schema import ATSName, Company
from jobhunt.sources import fetch_company

log = logging.getLogger("jobhunt.slugs")

DEFAULT_INDEX = Path("data/commoncrawl.txt")
DEFAULT_OUT = Path("data/companies.generated.yaml")

# boards.greenhouse.io, job-boards.greenhouse.io, and regional variants (job-boards.eu., .anz.)
_GREENHOUSE_BOARD = re.compile(r"(job-)?boards(\.[a-z]+)?\.greenhouse\.io")
_GREENHOUSE_API = "boards-api.greenhouse.io"
_BOARD_HOSTS: dict[str, ATSName] = {
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.smartrecruiters.com": "smartrecruiters",
}
_KEEPS_CASE: set[ATSName] = {"ashby", "smartrecruiters"}  # the others are case-insensitive

# <tenant>.<datacenter>.myworkdayjobs.com/[<language>/]<site>/...
_WORKDAY_HOST = re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com")
_LANGUAGE = re.compile(r"[a-z]{2}(-[a-z]{2})?", re.I)  # en-US, en-us, es

# Ends at whitespace, quotes, angle brackets, or a backslash; see _trim for trailing punctuation.
_URL = re.compile(r"""https?://[^\s"'<>\\]+""")
_CLOSERS = {")": "(", "]": "[", "}": "{"}

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]*")
_NOT_SLUGS = {"embed", "robots.txt", "llms.txt", "favicon.ico", "sitemap.xml"}
_NOT_WORKDAY_SITES = {"wday", "job", "details", "login"}
_NOT_SMARTRECRUITERS = {"oneclick-ui", "my-applications", "external-referrals", "xhtmlized"}


def _greenhouse_slug(host: str, segments: list[str], query: str) -> str:
    if host == _GREENHOUSE_API:  # /v1/boards/<slug>/jobs
        return segments[2] if segments[:2] == ["v1", "boards"] and len(segments) > 2 else ""
    if segments[:1] == ["embed"]:  # /embed/job_board?for=<slug>
        return parse_qs(query).get("for", [""])[0]
    return segments[0] if segments else ""


def _workday_board(tenant: str, datacenter: str, segments: list[str]) -> Company | None:
    if segments and _LANGUAGE.fullmatch(segments[0]):
        segments = segments[1:]
    site = segments[0] if segments else ""
    if not _SLUG.fullmatch(site) or site.lower() in _NOT_SLUGS | _NOT_WORKDAY_SITES:
        return None
    return Company(name=tenant, ats="workday", slug=f"{tenant}/{site}", datacenter=datacenter)


def board_from_url(url: str) -> Company | None:
    """The job board a URL points at, if any.

    Its name is a placeholder (the slug, or a Workday tenant); nothing in a URL tells us the
    real one.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    segments = [unquote(s) for s in parts.path.split("/") if s]

    if m := _WORKDAY_HOST.fullmatch(host):
        return _workday_board(m.group(1), m.group(2), segments)

    ats: ATSName
    if host == _GREENHOUSE_API or _GREENHOUSE_BOARD.fullmatch(host):
        ats, slug = "greenhouse", _greenhouse_slug(host, segments, parts.query)
    elif host in _BOARD_HOSTS:
        ats, slug = _BOARD_HOSTS[host], segments[0] if segments else ""
    else:
        return None

    if ats not in _KEEPS_CASE:
        slug = slug.lower()
    not_slugs = _NOT_SLUGS | _NOT_SMARTRECRUITERS if ats == "smartrecruiters" else _NOT_SLUGS
    if not _SLUG.fullmatch(slug) or slug.lower() in not_slugs:
        return None
    return Company(name=slug, ats=ats, slug=slug)


def discover(urls: Iterable[str], known: Iterable[Company] = ()) -> list[Company]:
    """Unique boards found in ``urls``, minus ``known``, sorted by ATS then slug.

    Slugs are compared case-insensitively; the first spelling seen is kept.
    """
    seen = {c.key.lower() for c in known}
    found: list[Company] = []
    for url in urls:
        board = board_from_url(url)
        if board is None or board.key.lower() in seen:
            continue
        seen.add(board.key.lower())
        found.append(board)
    return sorted(found, key=lambda c: (c.ats, c.slug.lower()))


def _scalar(value: str) -> str:
    """Quote a string only when YAML would otherwise read it as something else."""
    try:
        plain = yaml.safe_load(value) == value
    except yaml.YAMLError:
        plain = False
    return value if plain else json.dumps(value)


def render(companies: Iterable[Company]) -> str:
    """Format entries in the style of config/companies.yaml, without the ``companies:`` key."""
    return "\n".join(
        f"  - name: {_scalar(c.name)}\n"
        f"    ats: {c.ats}\n"
        f"    slug: {_scalar(c.slug)}\n"
        + (f"    datacenter: {c.datacenter}\n" if c.datacenter else "")
        + f"    tags: [{', '.join(_scalar(t) for t in c.tags)}]\n"
        for c in companies
    )


def _trim(url: str) -> str:
    """Drop punctuation that ends a sentence, or a bracket the URL never opened."""
    while url:
        last = url[-1]
        if last in ".,;:" or (last in _CLOSERS and url.count(_CLOSERS[last]) < url.count(last)):
            url = url[:-1]
        else:
            return url
    return url


def read_urls(path: Path) -> Iterator[str]:
    """Yield every http(s) URL in a text file, in order."""
    with path.open(errors="replace") as f:
        for line in f:
            for url in _URL.findall(line):
                yield _trim(url)


def _client(fetch: settings.FetchSettings | None = None) -> httpx.Client:
    fetch = fetch or settings.load().fetch
    return httpx.Client(
        timeout=fetch.timeout,
        headers={"User-Agent": fetch.user_agent},
        follow_redirects=True,
    )


def check_workers() -> int:
    """Threads for --check (slugs.check_workers / JOBHUNT_SLUGS_CHECK_WORKERS)."""
    return settings.load().slugs.check_workers


def _has_jobs(company: Company, client: httpx.Client) -> bool:
    """Whether a board is worth listing. Boards that can't be checked right now are kept."""
    try:
        jobs = fetch_company(company, client, wants_body=lambda job: False, max_pages=1)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status < 500 and status != 429:  # 429: busy, not gone
            log.info("dropped %s: HTTP %s", company.key, status)
            return False
        log.warning("%s: kept, could not check (HTTP %s)", company.key, status)
        return True
    except httpx.HTTPError as e:
        log.warning("%s: kept, could not check (%s)", company.key, e)
        return True
    if not jobs:
        log.info("dropped %s: no open postings", company.key)
    return bool(jobs)


def check(
    companies: list[Company], client: httpx.Client, workers: int | None = None
) -> list[Company]:
    """The boards that have open postings, in their original order."""
    with ThreadPoolExecutor(workers or check_workers()) as pool:
        keep = list(pool.map(lambda c: _has_jobs(c, client), companies))
    return [c for c, k in zip(companies, keep, strict=True) if k]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobhunt.slugs", description=__doc__)
    parser.add_argument("index", nargs="*", type=Path, default=[DEFAULT_INDEX])
    parser.add_argument("--companies", type=Path, help="existing companies.yaml to skip")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    check_help = "fetch the first page of each new board; drop those with no open postings"
    parser.add_argument("--check", action="store_true", help=check_help)
    args = parser.parse_args(argv)

    missing = [p for p in args.index if not p.is_file()]
    if missing:
        log.error("no such index file: %s", ", ".join(map(str, missing)))
        return 2

    known = config.load_companies(args.companies)
    found = discover((url for p in args.index for url in read_urls(p)), known)
    if args.check:
        try:
            s = settings.load()
        except settings.SettingsError as e:
            log.error("%s", e)
            return 2
        with _client(s.fetch) as client:
            checked = check(found, client, s.slugs.check_workers)
        log.info("checked %d boards: %d dropped", len(found), len(found) - len(checked))
        found = checked
    args.out.write_text(render(found))
    log.info("%d new companies -> %s (%d already known)", len(found), args.out, len(known))
    return 0


if __name__ == "__main__":
    # Progress and drop reasons from this module only: --check would otherwise log every request
    # (httpx) and every board's job count (sources) as well.
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    log.setLevel(logging.INFO)
    raise SystemExit(main())
