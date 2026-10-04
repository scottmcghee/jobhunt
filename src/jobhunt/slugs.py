"""Harvest ATS board slugs from Common Crawl index dumps.

    python -m jobhunt.slugs [INDEX ...] [--companies PATH] [-o OUT]

Each INDEX is a CDX index dump: one JSON record per line, each with a ``url``. The output is a
block of entries ready to paste under ``companies:`` in config/companies.yaml. Slugs already
listed there are left out, so the output can be regenerated and re-pasted as more data arrives.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from jobhunt import config
from jobhunt.schema import ATSName, Company

log = logging.getLogger("jobhunt.slugs")

DEFAULT_INDEX = Path("data/commoncrawl.json")
DEFAULT_OUT = Path("data/companies.generated.yaml")

# boards.greenhouse.io, job-boards.greenhouse.io, and regional variants (job-boards.eu., .anz.)
_GREENHOUSE_BOARD = re.compile(r"(job-)?boards(\.[a-z]+)?\.greenhouse\.io")
_GREENHOUSE_API = "boards-api.greenhouse.io"
_BOARD_HOSTS: dict[str, ATSName] = {"jobs.lever.co": "lever", "jobs.ashbyhq.com": "ashby"}

# <tenant>.<datacenter>.myworkdayjobs.com/[<language>/]<site>/...
_WORKDAY_HOST = re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com")
_LANGUAGE = re.compile(r"[a-z]{2}(-[a-z]{2})?", re.I)  # en-US, en-us, es

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]*")
_NOT_SLUGS = {"embed", "robots.txt", "llms.txt", "favicon.ico", "sitemap.xml"}
_NOT_WORKDAY_SITES = {"wday", "job", "details", "login"}


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

    if ats != "ashby":  # Ashby board names keep their case; the others are case-insensitive
        slug = slug.lower()
    if not _SLUG.fullmatch(slug) or slug.lower() in _NOT_SLUGS:
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


def read_urls(path: Path) -> Iterator[str]:
    """Yield the ``url`` of each record in a line-delimited CDX index dump."""
    with path.open() as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                url = json.loads(line)["url"]
            except (json.JSONDecodeError, KeyError, TypeError):
                log.warning("%s:%d: not a CDX record, skipped", path, lineno)
                continue
            yield url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobhunt.slugs", description=__doc__)
    parser.add_argument("index", nargs="*", type=Path, default=[DEFAULT_INDEX])
    parser.add_argument("--companies", type=Path, help="existing companies.yaml to skip")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    missing = [p for p in args.index if not p.is_file()]
    if missing:
        log.error("no such index file: %s", ", ".join(map(str, missing)))
        return 2

    known = config.load_companies(args.companies)
    found = discover((url for p in args.index for url in read_urls(p)), known)
    args.out.write_text(render(found))
    log.info("%d new companies -> %s (%d already known)", len(found), args.out, len(known))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
