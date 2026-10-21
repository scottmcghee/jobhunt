"""Kula: job boards at careers.kula.ai/<slug>, read from the board's own page.

Kula's JSON API (``/api/``) is disallowed by robots.txt, but each board's page is server-rendered
by Next.js, and the payload it streams into the page (``self.__next_f.push([1, "..."])`` scripts)
carries the whole listing, descriptions included, in one request (plus the host's robots.txt,
read once per run):

    GET https://careers.kula.ai/{slug}

The payload is a run of rows, ``<hex id>:<row>``. Most end at a newline; a text row,
``<id>:T<hex length>,<text>``, runs for that many UTF-8 bytes, newline or not. One row holds the
listing, an object with ``"jobs": [...]``; each job's ``ats_job.job_description`` is a reference,
``"$<id>"``, to a text row with its HTML.

A board is ``ats: kula`` with ``slug:`` the name in ``careers.kula.ai/<slug>`` (case doesn't
matter). A posting's page is ``careers.kula.ai/<slug>/<id>``. An unknown board answers 404.

Some boards don't render their listing into the page; the browser loads it from ``/api/``. Those
come back empty, with a warning, and noted as partial (``_partial``) so no recorded job closes.

robots.txt asks for no crawl delay, but every board shares one host, so its requests carry a cap
of ``RATE_CAP`` a second (a ``fetch.max_rate`` entry for ``kula`` overrides it).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import weakref
from datetime import UTC, datetime
from urllib.robotparser import RobotFileParser

import httpx

from jobhunt.schema import Company, Job
from jobhunt.sources import _partial, _sitemap
from jobhunt.sources._html import to_text
from jobhunt.sources._postings import with_ids
from jobhunt.sources._rate import RATE

log = logging.getLogger(__name__)

HOST = "careers.kula.ai"
RATE_CAP = 1.0  # requests a second, shared by every board on the host

_PUSH = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_ROW = re.compile(rb"([0-9a-f]*):")  # a row's id may be empty (":HL[...]", a preload hint)
_LISTING = '{"jobs":['

# robots.txt is read once per client (a run), not per board; boards are fetched from worker threads
_robots: weakref.WeakKeyDictionary[httpx.Client, RobotFileParser] = weakref.WeakKeyDictionary()
_robots_lock = threading.Lock()


def _payload(page: str) -> str:
    """The Next.js payload streamed into the page, its chunks joined."""
    chunks = []
    for m in _PUSH.finditer(page):
        try:
            push = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(push, list) and len(push) > 1 and push[0] == 1 and isinstance(push[1], str):
            chunks.append(push[1])
    return "".join(chunks)


def _rows(payload: str) -> tuple[dict[str, str], list[str]]:
    """The payload's text rows by id, and its other rows; ValueError if it isn't rows."""
    data = payload.encode()
    texts: dict[str, str] = {}
    rows: list[str] = []
    pos = 0
    while pos < len(data):
        m = _ROW.match(data, pos)
        if not m:
            raise ValueError(f"unreadable page payload at byte {pos}")
        start = m.end()
        if data[start : start + 1] == b"T":
            comma = data.find(b",", start)
            if comma < 0:
                raise ValueError(f"unreadable text row at byte {pos}")
            end = comma + 1 + int(data[start + 1 : comma], 16)
            texts[m.group(1).decode()] = data[comma + 1 : end].decode(errors="replace")
        else:
            end = data.find(b"\n", start)
            end = len(data) if end < 0 else end
            rows.append(data[start:end].decode(errors="replace"))
            end += 1
        pos = end
    return texts, rows


def _listing(rows: list[str]) -> list[dict] | None:
    """The jobs in the row that lists them, or None if no row does."""
    for row in rows:
        i = row.find(_LISTING)
        if i < 0:
            continue
        listing, _ = json.JSONDecoder().raw_decode(row, i)
        jobs = listing.get("jobs")
        return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []
    return None


def _description(value: object, texts: dict[str, str]) -> str:
    """A job's description: the text row a ``$<id>`` reference names (empty if it's missing)."""
    if not isinstance(value, str):
        return ""
    if value.startswith("$$"):  # an escaped literal "$"
        return value[1:]
    if value.startswith("$"):
        return texts.get(value[1:], "")
    return value


def _rules(client: httpx.Client) -> RobotFileParser:
    """The host's robots.txt rules, fetched the first time this client asks."""
    with _robots_lock:
        if client not in _robots:
            _robots[client] = _sitemap.robots(client, HOST, RATE_CAP)
        return _robots[client]


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _is_remote(workplace: str, location: str, title: str) -> bool | None:
    """From Kula's ``workplace`` when it says, else from the location and title."""
    if workplace == "remote":
        return True
    if workplace in ("hybrid", "office"):
        return False
    return True if "remote" in f"{location} {title}".lower() else None


def normalize(company: Company, raw: dict, texts: dict[str, str]) -> Job:
    ats_job = raw.get("ats_job") if isinstance(raw.get("ats_job"), dict) else {}
    offices = [o for o in ats_job.get("offices") or [] if isinstance(o, dict)]
    location = "; ".join(o["location"] for o in offices if isinstance(o.get("location"), str))
    title = raw.get("title") or ""
    posted = _when(raw.get("launch_at"))
    return Job(
        source="kula",
        company=company.name,
        company_slug=company.slug,
        external_id=str(raw["id"]),
        title=title,
        location=location,
        remote=_is_remote(str(ats_job.get("workplace") or "").lower(), location, title),
        url=f"https://{HOST}/{company.slug}/{raw['id']}",
        body=to_text(_description(ats_job.get("job_description"), texts)),
        posted_at=posted.isoformat() if posted else None,
    )


def _open(raw: dict, now: datetime) -> bool:
    """Listed, and not past its end date."""
    end = _when(raw.get("end_at"))
    return raw.get("listed") is not False and (end is None or end > now)


def fetch(company: Company, client: httpx.Client, now: datetime | None = None) -> list[Job]:
    """Every open posting the board's page lists."""
    now = now or datetime.now(UTC)
    url = f"https://{HOST}/{company.slug}"
    rules = _rules(client)
    if not rules.can_fetch(_sitemap.agent(client), url):
        log.warning("kula %s: robots.txt disallows the board", company.slug)
        _partial.note(company.key, "robots.txt disallows the board")
        return []
    resp = client.get(url, extensions={RATE: RATE_CAP})
    resp.raise_for_status()
    texts, rows = _rows(_payload(resp.text))
    listed = _listing(rows)
    if listed is None:
        log.warning(
            "kula %s: the page lists no jobs (it may load them in the browser)", company.slug
        )
        _partial.note(company.key, "the page lists no jobs")
        return []
    jobs = [normalize(company, raw, texts) for raw in with_ids(company, listed) if _open(raw, now)]
    log.info("kula %s: %d jobs", company.slug, len(jobs))
    return jobs
