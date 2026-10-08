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
"""

from __future__ import annotations

import bisect
import gzip
import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from jobhunt.sources._rate import RATE

log = logging.getLogger(__name__)

COLLINFO = "https://index.commoncrawl.org/collinfo.json"  # robots.txt allows this one path
DATA = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/{file}"
RATE_CAP = 1.0  # requests a second


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


def latest_crawls(client: httpx.Client, n: int = 1) -> list[str]:
    """The ids of the ``n`` most recent crawls, newest first (``CC-MAIN-2026-39``)."""
    resp = client.get(COLLINFO, extensions={RATE: RATE_CAP})
    resp.raise_for_status()
    return [c["id"] for c in resp.json()[:n] if isinstance(c, dict) and c.get("id")]


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
