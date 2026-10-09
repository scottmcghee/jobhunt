"""Reading Common Crawl's URL index by byte range: blocks for host prefixes, and their URLs."""

from __future__ import annotations

import gzip
import json

import httpx
import pytest
import respx

from jobhunt import commoncrawl as cc

CRAWL = "CC-MAIN-2026-39"
INDEXES = f"https://data.commoncrawl.org/cc-index/collections/{CRAWL}/indexes"


def _line(surt: str, url: str) -> str:
    record = {"url": url, "mime": "text/html", "status": "200"}
    return f"{surt} 20260911222300 {json.dumps(record)}"


# Three index blocks, as Common Crawl cuts them (each its own gzip member, in one shard file).
# Gem's URLs start at the end of the block before the first one whose key is Gem's.
BLOCKS = [
    [
        _line("com,geluk)/robots.txt", "https://geluk.com/robots.txt"),
        _line("com,gem,jobs)/acme/1", "https://jobs.gem.com/acme/1"),
    ],
    [
        _line("com,gem,jobs)/beta/2", "https://jobs.gem.com/Beta/2"),
        _line("com,gem-beauty)/pages/x", "https://gem-beauty.com/pages/x"),
    ],
    [
        _line("com,myworkdayjobs,wd5,acme)/external", "https://acme.wd5.myworkdayjobs.com/External"),
        _line("com,myworkdayjobs,wd12,beta)/careers", "https://beta.wd12.myworkdayjobs.com/Careers"),
    ],
]


def _shard_and_index() -> tuple[bytes, str]:
    shard, index, offset = b"", [], 0
    for n, block in enumerate(BLOCKS):
        member = gzip.compress(("\n".join(block) + "\n").encode())
        first_key = block[0].split(" ")[0]
        index.append(f"{first_key} 20260911222300\tcdx-00073.gz\t{offset}\t{len(member)}\t{n + 1}")
        shard += member
        offset += len(member)
    return shard, "\n".join(index) + "\n"


SHARD, CLUSTER = _shard_and_index()


@pytest.fixture(autouse=True)
def _waits(monkeypatch):
    """The waits after a 503 Slow Down, recorded instead of slept."""
    waits: list[float] = []
    monkeypatch.setattr(cc.time, "sleep", waits.append)
    return waits


def _serve_ranges(request: httpx.Request) -> httpx.Response:
    start, end = request.headers["range"].removeprefix("bytes=").split("-")
    return httpx.Response(206, content=SHARD[int(start) : int(end) + 1])


@pytest.mark.parametrize(
    ("host", "subdomains", "prefix"),
    [
        ("jobs.gem.com", False, "com,gem,jobs)"),
        ("ats.rippling.com", False, "com,rippling,ats)"),
        ("myworkdayjobs.com", True, "com,myworkdayjobs,"),
        ("Jobs.Lever.co", False, "co,lever,jobs)"),
    ],
)
def test_surt_prefix(host, subdomains, prefix):
    assert cc.surt_prefix(host, subdomains=subdomains) == prefix


def test_blocks_for_a_prefix_include_the_block_before_its_first_key():
    blocks = cc.read_blocks(CLUSTER.splitlines())
    picked = cc.blocks_for(blocks, ["com,gem,jobs)"])
    assert [b.key for b in picked] == ["com,geluk)/robots.txt", "com,gem,jobs)/beta/2"]
    assert picked[0].file == "cdx-00073.gz" and picked[0].offset == 0


def test_a_prefix_with_no_block_of_its_own_gets_the_block_it_falls_in():
    blocks = cc.read_blocks(CLUSTER.splitlines())
    assert [b.key for b in cc.blocks_for(blocks, ["com,gem,jobs)/acme"])] == ["com,geluk)/robots.txt"]


def test_blocks_for_several_prefixes_are_fetched_once_each():
    blocks = cc.read_blocks(CLUSTER.splitlines())
    picked = cc.blocks_for(blocks, ["com,gem,jobs)", "com,gem,jobs)/beta", "com,myworkdayjobs,"])
    assert len(picked) == len({b.key for b in picked}) == 3


def test_an_empty_index_picks_no_blocks():
    assert cc.blocks_for([], ["com,gem,jobs)"]) == []
    assert cc.select_blocks(iter([]), ["com,gem,jobs)"]) == []


@respx.mock
def test_urls_reads_only_the_matching_lines_by_byte_range(tmp_path):
    (tmp_path / CRAWL).mkdir()
    (tmp_path / CRAWL / "cluster.idx").write_text(CLUSTER)
    route = respx.get(f"{INDEXES}/cdx-00073.gz").mock(side_effect=_serve_ranges)
    with httpx.Client() as client:
        gem = list(cc.urls(client, CRAWL, ["com,gem,jobs)"], tmp_path))
        workday = list(cc.urls(client, CRAWL, ["com,myworkdayjobs,"], tmp_path))
    assert gem == ["https://jobs.gem.com/acme/1", "https://jobs.gem.com/Beta/2"]
    assert workday == [
        "https://acme.wd5.myworkdayjobs.com/External", "https://beta.wd12.myworkdayjobs.com/Careers"
    ]
    assert all(c.request.headers["range"].startswith("bytes=") for c in route.calls)
    assert route.call_count == 4  # each prefix: its blocks and the one before (it may end there)


@respx.mock
def test_the_cluster_index_is_downloaded_once_and_cached(tmp_path):
    route = respx.get(f"{INDEXES}/cluster.idx").mock(return_value=httpx.Response(200, text=CLUSTER))
    with httpx.Client() as client:
        path = cc.cluster_index(client, CRAWL, tmp_path)
        assert cc.cluster_index(client, CRAWL, tmp_path) == path
    assert route.call_count == 1 and path.read_text() == CLUSTER


@respx.mock
def test_a_failed_download_leaves_no_partial_index(tmp_path):
    respx.get(f"{INDEXES}/cluster.idx").mock(return_value=httpx.Response(503))
    with httpx.Client() as client, pytest.raises(cc.Busy):
        cc.cluster_index(client, CRAWL, tmp_path)
    assert not list(tmp_path.rglob("cluster.idx*"))


class _BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield CLUSTER[:40].encode()
        raise httpx.ReadError("connection reset")


@respx.mock
def test_a_download_cut_off_midway_leaves_no_partial_index(tmp_path):
    respx.get(f"{INDEXES}/cluster.idx").mock(return_value=httpx.Response(200, stream=_BrokenStream()))
    with httpx.Client() as client, pytest.raises(httpx.ReadError):
        cc.cluster_index(client, CRAWL, tmp_path)
    assert not list(tmp_path.rglob("cluster.idx*"))


@pytest.mark.parametrize(
    "prefixes",
    [
        ["com,gem,jobs)"],  # its blocks and the one before
        ["com,gem,jobs)/acme"],  # inside a block
        ["com,geluk)"],  # the first block
        ["a"],  # sorts before every block
        ["zzz"],  # sorts after every block
        ["com,gem,jobs)", "com,gem,jobs)/beta", "com,myworkdayjobs,"],
    ],
)
def test_streaming_block_selection_matches_blocks_for(prefixes):
    lines = CLUSTER.splitlines()
    assert cc.select_blocks(iter(lines), prefixes) == cc.blocks_for(cc.read_blocks(lines), prefixes)


@respx.mock
def test_latest_crawls_come_from_collinfo():
    respx.get(cc.COLLINFO).mock(
        return_value=httpx.Response(200, json=[{"id": "CC-MAIN-2026-39"}, {"id": "CC-MAIN-2026-34"}, {"id": "CC-MAIN-2026-30"}])
    )
    with httpx.Client() as client:
        assert cc.latest_crawls(client, 2) == ["CC-MAIN-2026-39", "CC-MAIN-2026-34"]


def test_a_malformed_index_line_is_skipped():
    blocks = cc.read_blocks(["not an index line", *CLUSTER.splitlines()])
    assert len(blocks) == 3


# ------------------------------------------------------------------ web graph host list

RELEASE = "cc-main-2026-jul-aug-sep"
GRAPH = f"https://data.commoncrawl.org/projects/hyperlinkgraph/{RELEASE}/host"
PARTS = [
    # id, reversed host: sorted by reversed host, as Common Crawl publishes them
    ["0\tai.acme.careers", "1\tca.maple.jobs", "2\tcom.acme.www", "3\tcom.beta.careers"],
    ["4\tcom.gamma.jobs", "5\tcom.gamma.jobs.eu", "6\torg.delta.talent", "7\tus.state.wa.careers"],
]


def _gz_members(lines: list[str]) -> bytes:
    """Two gzip members back to back, as a file written in pieces may be."""
    half = len(lines) // 2
    return b"".join(gzip.compress(("\n".join(p) + "\n").encode()) for p in (lines[:half], lines[half:]))


def _serve_graph(fail_part: int | None = None):
    paths = [f"projects/hyperlinkgraph/{RELEASE}/host/vertices/part-0000{i}.txt.gz" for i in range(len(PARTS))]
    respx.get(f"{GRAPH}/{RELEASE}-host-vertices.paths.gz").mock(
        return_value=httpx.Response(200, content=gzip.compress(("\n".join(paths) + "\n").encode()))
    )
    routes = []
    for i, lines in enumerate(PARTS):
        response = httpx.Response(503) if i == fail_part else httpx.Response(200, content=_gz_members(lines))
        routes.append(respx.get(f"https://data.commoncrawl.org/{paths[i]}").mock(return_value=response))
    return routes


@respx.mock
def test_latest_graph_comes_from_graphinfo():
    respx.get(cc.GRAPHINFO).mock(return_value=httpx.Response(200, json=[{"id": RELEASE}, {"id": "cc-main-2026-may-jun-jul"}]))
    with httpx.Client() as client:
        assert cc.latest_graph(client) == RELEASE


@respx.mock
def test_webgraph_hosts_keeps_hosts_whose_first_label_is_wanted(tmp_path):
    _serve_graph()
    with httpx.Client() as client:
        hosts = cc.webgraph_hosts(client, RELEASE, ["careers", "jobs"], tmp_path)
    assert hosts == ["careers.acme.ai", "jobs.maple.ca", "careers.beta.com", "jobs.gamma.com", "careers.wa.state.us"]


@respx.mock
def test_webgraph_hosts_are_cached_per_release_and_labels(tmp_path):
    routes = _serve_graph()
    with httpx.Client() as client:
        first = cc.webgraph_hosts(client, RELEASE, ["careers"], tmp_path)
        again = cc.webgraph_hosts(client, RELEASE, ["careers"], tmp_path)
        assert again == first and sum(r.call_count for r in routes) == 2
        other = cc.webgraph_hosts(client, RELEASE, ["talent"], tmp_path)  # other labels: read again
    assert other == ["talent.delta.org"] and sum(r.call_count for r in routes) == 4


@respx.mock
def test_an_interrupted_webgraph_read_keeps_the_files_it_finished(tmp_path):
    routes = _serve_graph(fail_part=1)
    with httpx.Client() as client, pytest.raises(cc.Busy):
        cc.webgraph_hosts(client, RELEASE, ["careers", "jobs"], tmp_path)
    assert [r.call_count for r in routes] == [1, 1 + len(cc.SLOW_DOWN_WAITS)]
    routes[1].mock(return_value=httpx.Response(200, content=_gz_members(PARTS[1])))
    with httpx.Client() as client:
        hosts = cc.webgraph_hosts(client, RELEASE, ["careers", "jobs"], tmp_path)
    assert routes[0].call_count == 1  # only the file that failed is read again
    assert len(hosts) == 5


@respx.mock
def test_an_interrupted_write_of_the_host_list_is_not_trusted(tmp_path, monkeypatch):
    _serve_graph()
    write_text = cc.Path.write_text

    def cut_off(path, text, *args, **kwargs):
        if path.stem == "hosts":  # the finished list: half of it, then Ctrl-C
            write_text(path, text[: len(text) // 2])
            raise KeyboardInterrupt
        return write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(cc.Path, "write_text", cut_off)
    with httpx.Client() as client, pytest.raises(KeyboardInterrupt):
        cc.webgraph_hosts(client, RELEASE, ["careers", "jobs"], tmp_path)
    monkeypatch.setattr(cc.Path, "write_text", write_text)
    with httpx.Client() as client:
        hosts = cc.webgraph_hosts(client, RELEASE, ["careers", "jobs"], tmp_path)
    assert len(hosts) == 5


@pytest.mark.parametrize(
    ("host", "kept"),
    [
        ("careers.acme.com", True),
        ("jobs.acme.technology", True),  # generic, however long
        ("careers.acme.io", True),  # a country code used generically
        ("careers.acme.ai", True),
        ("careers.acme.co", True),
        ("careers.wa.state.us", True),  # a country code on the list
        ("careers.acme.ca", False),  # a country code not on it
        ("careers.acme.co.uk", False),
    ],
)
def test_generic_or_listed_tld(host, kept):
    assert cc.generic_or_listed_tld(host, ["us"]) is kept


@pytest.mark.parametrize(
    ("host", "kept"),
    [("careers.acme.com", False), ("careers.acme.io", False), ("careers.wa.state.us", True)],
)
def test_without_generic_tlds_only_the_listed_ones_pass(host, kept):
    assert cc.generic_or_listed_tld(host, ["us"], generic=False) is kept


# ------------------------------------------------------------------ 503 Slow Down


@respx.mock
def test_a_slow_down_is_waited_out_and_retried(tmp_path, _waits):
    routes = _serve_graph()
    good = routes[1].return_value
    routes[1].mock(side_effect=[httpx.Response(503, text="Slow Down"), httpx.Response(503), good])
    with httpx.Client() as client:
        hosts = cc.webgraph_hosts(client, RELEASE, ["careers", "jobs"], tmp_path)
    assert len(hosts) == 5 and _waits == list(cc.SLOW_DOWN_WAITS[:2])


@respx.mock
def test_a_slow_down_that_lasts_raises_busy_after_the_waits(_waits):
    respx.get(cc.GRAPHINFO).mock(return_value=httpx.Response(503))
    with httpx.Client() as client, pytest.raises(cc.Busy, match="503"):
        cc.latest_graph(client)
    assert _waits == list(cc.SLOW_DOWN_WAITS)


@respx.mock
def test_other_errors_are_not_retried(tmp_path, _waits):
    route = respx.get(f"{INDEXES}/cluster.idx").mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        cc.cluster_index(client, CRAWL, tmp_path)
    assert route.call_count == 1 and _waits == []


@respx.mock
def test_index_blocks_and_the_cluster_index_retry_a_slow_down_too(tmp_path, _waits):
    respx.get(f"{INDEXES}/cluster.idx").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text=CLUSTER)]
    )
    calls = []

    def blocks(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else _serve_ranges(request)

    respx.get(f"{INDEXES}/cdx-00073.gz").mock(side_effect=blocks)
    with httpx.Client() as client:
        gem = list(cc.urls(client, CRAWL, ["com,gem,jobs)"], tmp_path))
    assert gem == ["https://jobs.gem.com/acme/1", "https://jobs.gem.com/Beta/2"]
    assert len(_waits) == 2
