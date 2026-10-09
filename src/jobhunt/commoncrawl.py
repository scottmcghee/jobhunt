"""Common Crawl's URL index, read by byte range: every URL a crawl saw under some hosts.

Each crawl's index is about 300 shard files (``cdx-NNNNN.gz``), sorted by SURT key, the URL with
its host reversed (``com,gem,jobs)/acme/1`` for ``https://jobs.gem.com/acme/1``). The shards are
cut into blocks of 3,000 lines, each its own gzip member, and ``cluster.idx`` (about 100 MB)
lists every block's first key and byte range. All of a host's URLs sit together in a few
blocks, so finding every URL under ``jobs.gem.com`` costs a few hundred KB, not the 300 GB index:

    1. find the blocks whose first key starts with the host's SURT prefix, plus the one before
       (the host's URLs may begin at its end);
    2. fetch each by byte range from data.commoncrawl.org and gunzip it;
    3. keep the lines whose key starts with the prefix; each ends in JSON with the URL.

``cluster.idx`` is cached per crawl. Common Crawl's robots.txt disallows its hosts to crawlers,
but its documentation directs people to download its published files from data.commoncrawl.org;
the owner decided (October 2026) this personal, non-commercial use of a public dataset is that.
Requests go one at a time, at most one a second.

The web graph is another of Common Crawl's datasets: each quarter it lists every host its crawls
saw (about 250 million), as ``<id>\t<reversed host>`` lines in some 50 gzip files (about 1.3 GB),
sorted by reversed host (``com.acme.careers``). ``webgraph_hosts`` streams them once per release
and keeps the hosts whose first label is wanted (``careers``, ``jobs``), caching what it keeps per
file, so an interrupted read resumes where it stopped.

data.commoncrawl.org is Amazon S3, which answers ``503 Slow Down`` when it's busy. Every request
here waits that out (``SLOW_DOWN_WAITS``) and tries again; if S3 is still busy, ``Busy`` is
raised. ``cluster.idx`` and the web graph files already read stay cached for the next run; index
blocks are not cached, so a rerun fetches them again.
"""

from __future__ import annotations

import bisect
import functools
import gzip
import hashlib
import json
import logging
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx

from jobhunt.sources._rate import RATE

log = logging.getLogger(__name__)

COLLINFO = "https://index.commoncrawl.org/collinfo.json"  # robots.txt allows this path
GRAPHINFO = "https://index.commoncrawl.org/graphinfo.json"  # and this one
GRAPH_FILES = "https://data.commoncrawl.org/{path}"
GRAPH_VERTICES = "projects/hyperlinkgraph/{release}/host/{release}-host-vertices.paths.gz"
DATA = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/{file}"
RATE_CAP = 1.0  # requests a second
SLOW_DOWN_WAITS = (10, 20, 40, 80, 160)  # seconds before each retry after a 503: about 5 min


class Busy(Exception):
    """Common Crawl kept answering 503 Slow Down."""


T = TypeVar("T")


def _slow_down_retries(call: Callable[..., T]) -> Callable[..., T]:
    """Retry ``call`` after a 503, waiting ``SLOW_DOWN_WAITS`` in turn; then raise ``Busy``."""

    @functools.wraps(call)
    def retrying(*args, **kwargs) -> T:
        for wait in (*SLOW_DOWN_WAITS, None):
            try:
                return call(*args, **kwargs)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 503:
                    raise
                if wait is None:
                    raise Busy(
                        f"{e.request.url.host} answered 503 {e.response.reason_phrase} "
                        f"{len(SLOW_DOWN_WAITS) + 1} times"
                    ) from None
                log.warning(
                    "commoncrawl: %s answered 503 %s; trying again in %d s",
                    e.request.url.host, e.response.reason_phrase, wait,
                )
                time.sleep(wait)
        raise AssertionError("unreachable")

    return retrying


@dataclass(frozen=True)
class Block:
    """One block of an index shard: its first SURT key, the shard, and its byte range."""

    key: str
    file: str
    offset: int
    length: int


def surt_prefix(host: str, subdomains: bool = False) -> str:
    """The SURT key prefix of a host's URLs: ``jobs.gem.com`` -> ``com,gem,jobs)``. With
    ``subdomains``, of every host under it: ``myworkdayjobs.com`` -> ``com,myworkdayjobs,``."""
    labels = ",".join(reversed(host.lower().strip(".").split(".")))
    return labels + ("," if subdomains else ")")


@_slow_down_retries
def latest_crawls(client: httpx.Client, n: int = 1) -> list[str]:
    """The ids of the ``n`` most recent crawls, newest first (``CC-MAIN-2026-39``)."""
    resp = client.get(COLLINFO, extensions={RATE: RATE_CAP})
    resp.raise_for_status()
    return [c["id"] for c in resp.json()[:n] if isinstance(c, dict) and c.get("id")]


@_slow_down_retries
def cluster_index(client: httpx.Client, crawl: str, cache_dir: Path) -> Path:
    """The crawl's cluster.idx, downloaded on first use; a failed download leaves no file."""
    path = cache_dir / crawl / "cluster.idx"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name("cluster.idx.part")
    url = DATA.format(crawl=crawl, file="cluster.idx")
    try:
        with client.stream("GET", url, extensions={RATE: RATE_CAP}) as resp:
            resp.raise_for_status()
            with partial.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    log.info("commoncrawl %s: cluster.idx cached (%d bytes)", crawl, path.stat().st_size)
    return path


def _parse_blocks(lines: Iterable[str]) -> Iterator[Block]:
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        try:
            yield Block(parts[0].split(" ")[0], parts[1], int(parts[2]), int(parts[3]))
        except ValueError:
            continue


def read_blocks(lines: Iterable[str]) -> list[Block]:
    """Blocks from cluster.idx lines.

    Each is ``<surt> <timestamp>\\t<file>\\t<offset>\\t<length>\\t<n>``; others are skipped.
    """
    return list(_parse_blocks(lines))


def blocks_for(blocks: Sequence[Block], prefixes: Iterable[str]) -> list[Block]:
    """The blocks that may hold URLs under any of ``prefixes``, each once, in index order."""
    if not blocks:
        return []
    keys = [b.key for b in blocks]
    wanted: set[int] = set()
    for prefix in prefixes:
        i = bisect.bisect_left(keys, prefix)
        wanted.add(max(i - 1, 0))  # the prefix's URLs may begin at the end of the block before
        while i < len(keys) and keys[i].startswith(prefix):
            wanted.add(i)
            i += 1
    return [blocks[i] for i in sorted(wanted)]


def select_blocks(lines: Iterable[str], prefixes: Iterable[str]) -> list[Block]:
    """``blocks_for`` in one pass over cluster.idx lines, keeping only the blocks it picks."""
    prefixes = tuple(prefixes)
    pending = sorted(set(prefixes))  # prefixes no block key has reached yet
    picked: list[Block] = []
    prev: Block | None = None

    def pick(block: Block) -> None:
        if not picked or picked[-1] != block:
            picked.append(block)

    for block in _parse_blocks(lines):
        while pending and block.key >= pending[0]:
            pending.pop(0)
            pick(prev or block)  # the prefix's URLs may begin at the end of the block before
        if block.key.startswith(prefixes):
            pick(block)
        prev = block
    if pending and prev is not None:
        pick(prev)  # prefixes sorting after every key fall in the last block
    return picked


@_slow_down_retries
def _block_lines(client: httpx.Client, crawl: str, block: Block) -> list[str]:
    resp = client.get(
        DATA.format(crawl=crawl, file=block.file),
        headers={"Range": f"bytes={block.offset}-{block.offset + block.length - 1}"},
        extensions={RATE: RATE_CAP},
    )
    resp.raise_for_status()
    return gzip.decompress(resp.content).decode("utf-8", errors="replace").splitlines()


def urls(
    client: httpx.Client, crawl: str, prefixes: Sequence[str], cache_dir: Path
) -> Iterator[str]:
    """Every URL the crawl saw whose SURT key starts with one of ``prefixes``."""
    path = cluster_index(client, crawl, cache_dir)
    with path.open(encoding="utf-8", errors="replace") as f:
        blocks = select_blocks(f, prefixes)
    for block in blocks:
        for line in _block_lines(client, crawl, block):
            key, _, rest = line.partition(" ")
            if not key.startswith(tuple(prefixes)):
                continue
            try:
                url = json.loads(rest.partition(" ")[2]).get("url")
            except ValueError:
                continue
            if isinstance(url, str):
                yield url


# ------------------------------------------------------------------ web graph host list


@_slow_down_retries
def latest_graph(client: httpx.Client) -> str:
    """The id of the most recent web graph release (``cc-main-2026-jul-aug-sep``)."""
    resp = client.get(GRAPHINFO, extensions={RATE: RATE_CAP})
    resp.raise_for_status()
    releases = [g["id"] for g in resp.json() if isinstance(g, dict) and g.get("id")]
    if not releases:
        raise ValueError(f"{GRAPHINFO}: no releases listed")
    return releases[0]


def _gunzip_lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Lines of a gzip stream, which may be several members back to back."""
    unzip = zlib.decompressobj(zlib.MAX_WBITS | 16)
    rest = b""
    for chunk in chunks:
        while chunk:
            rest += unzip.decompress(chunk)
            chunk = b""
            if unzip.eof:  # a member ended; whatever follows starts the next
                chunk = unzip.unused_data
                unzip = zlib.decompressobj(zlib.MAX_WBITS | 16)
        *lines, rest = rest.split(b"\n")
        yield from lines
    rest += unzip.flush()
    yield from (line for line in rest.split(b"\n") if line)


@_slow_down_retries
def _vertex_paths(client: httpx.Client, release: str) -> list[str]:
    url = GRAPH_FILES.format(path=GRAPH_VERTICES.format(release=release))
    resp = client.get(url, extensions={RATE: RATE_CAP})
    resp.raise_for_status()
    return [line.decode().strip() for line in _gunzip_lines([resp.content]) if line.strip()]


@_slow_down_retries
def _wanted_hosts(client: httpx.Client, path: str, labels: frozenset[str]) -> list[str]:
    hosts = []
    url = GRAPH_FILES.format(path=path)
    with client.stream("GET", url, extensions={RATE: RATE_CAP}) as resp:
        resp.raise_for_status()
        for line in _gunzip_lines(resp.iter_bytes()):
            reversed_host = line.partition(b"\t")[2].decode("utf-8", errors="replace").strip()
            parts = reversed_host.split(".")
            if len(parts) >= 3 and parts[-1] in labels:  # a subdomain, not the bare domain
                hosts.append(".".join(reversed(parts)))
    return hosts


def webgraph_hosts(
    client: httpx.Client, release: str, labels: Sequence[str], cache_dir: Path
) -> list[str]:
    """Every host in the release's web graph whose first label is one of ``labels``.

    Each vertices file's hosts are cached in ``cache_dir/webgraph/<release>/<labels key>/``, so a
    rerun with the same labels makes no requests and an interrupted one fetches only the files
    it hadn't finished.
    """
    wanted = frozenset(label.lower() for label in labels)
    key = hashlib.sha1(",".join(sorted(wanted)).encode()).hexdigest()[:10]
    folder = cache_dir / "webgraph" / release / key
    done = folder / "hosts.txt"
    if done.exists():
        return done.read_text().split()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "labels.txt").write_text("\n".join(sorted(wanted)) + "\n")
    hosts: list[str] = []
    paths = _vertex_paths(client, release)
    for n, path in enumerate(paths):
        part = folder / f"part-{n:05d}.txt"
        if not part.exists():
            found = _wanted_hosts(client, path, wanted)
            partial = part.with_suffix(".part")
            partial.write_text("".join(f"{h}\n" for h in found))
            partial.replace(part)
            log.info(
                "web graph %s: file %d of %d, %d hosts", release, n + 1, len(paths), len(found)
            )
        hosts += part.read_text().split()
    partial = done.with_suffix(".part")  # written whole, then renamed: hosts.txt means done
    partial.write_text("".join(f"{h}\n" for h in hosts))
    partial.replace(done)
    for n in range(len(paths)):
        (folder / f"part-{n:05d}.txt").unlink(missing_ok=True)
    return hosts


# Two-letter country codes that are used as generic domains in practice
GENERIC_COUNTRY_CODES = frozenset({"ai", "co", "io"})


def generic_or_listed_tld(host: str, country_tlds: Iterable[str], generic: bool = True) -> bool:
    """Whether the host's top-level domain is one of ``country_tlds``, or (with ``generic``)
    generic: anything but a two-letter country code, or one of ``GENERIC_COUNTRY_CODES``."""
    tld = host.lower().rstrip(".").rpartition(".")[2]
    if tld in {t.lower().lstrip(".") for t in country_tlds}:
        return True
    return generic and (len(tld) != 2 or tld in GENERIC_COUNTRY_CODES)
