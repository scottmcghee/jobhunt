"""Discovery: boards from Common Crawl's URLs on known platforms, and from careers hosts."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from jobhunt import discover
from jobhunt.schema import Company


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(discover.fingerprint.time, "sleep", lambda s: None)


# ------------------------------------------------------------------ careers hosts from a file


def test_hosts_from_every_input_form():
    lines = [
        "careers.acme.com",
        "https://jobs.beta.com/openings?x=1",
        "com,gamma,careers)/en/job/1 20260904133119\tcdx-00005.gz\t608548801\t282338\t17208",
        "com,delta,careers:8080)/jobs 20260904133119\tcdx-00005.gz\t1\t2\t3",
        "# a comment",
        "",
        "Careers.Acme.com",  # again, in another case
    ]
    hosts, skipped = discover.read_hosts(lines)
    assert hosts == ["careers.acme.com", "jobs.beta.com", "careers.gamma.com", "careers.delta.com"]
    assert skipped == []


def test_classic_icims_portals_are_skipped_without_a_request():
    hosts, skipped = discover.read_hosts(
        ["careers-acme.icims.com", "com,icims,careers-beta)/jobs 2026\tcdx\t1\t2\t3", "careers.acme.com"]
    )
    assert hosts == ["careers.acme.com"]
    assert skipped == ["careers-acme.icims.com", "careers-beta.icims.com"]


@pytest.mark.parametrize(
    ("host", "name"),
    [("careers.acme.com", "acme"), ("jobs.beta-co.io", "beta-co"), ("www.gamma.co.uk", "gamma"), ("acme.com", "acme")],
)
def test_a_placeholder_name_from_the_host(host, name):
    assert discover.name_from_host(host) == name


WORKDAY_PAGE = '<a href="https://acme.wd5.myworkdayjobs.com/External/job/Seattle/Director_R1">Apply</a>'


def _careers_site(host="careers.acme.com", page=WORKDAY_PAGE):
    respx.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))
    return respx.get(f"https://{host}/").mock(return_value=httpx.Response(200, text=page))


@respx.mock
def test_a_careers_host_gives_the_board_its_site_points_at(tmp_path):
    _careers_site()
    with httpx.Client() as client:
        boards = discover.survey_hosts(["careers.acme.com"], client, tmp_path / "hosts.json", delay=0)
    assert [(b.ats, b.slug, b.datacenter, b.name) for b in boards] == [("workday", "acme/External", "wd5", "acme")]


@respx.mock
def test_a_surveyed_host_is_cached_and_not_visited_again(tmp_path):
    page = _careers_site()
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(["careers.acme.com"], client, cache, delay=0)
        again = discover.survey_hosts(["careers.acme.com"], client, cache, delay=0)
    assert page.call_count == 1
    assert [b.slug for b in again] == ["acme/External"]  # from the cache
    saved = json.loads(cache.read_text())
    assert saved["careers.acme.com"]["platforms"] == ["workday"]


@respx.mock
def test_refresh_visits_cached_hosts_again(tmp_path):
    page = _careers_site()
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(["careers.acme.com"], client, cache, delay=0)
        discover.survey_hosts(["careers.acme.com"], client, cache, delay=0, refresh=True)
    assert page.call_count == 2


@respx.mock
def test_an_unreachable_host_is_not_cached(tmp_path):
    respx.get("https://careers.down.com/robots.txt").mock(side_effect=httpx.ConnectError("nope"))
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        assert discover.survey_hosts(["careers.down.com"], client, cache, delay=0) == []
    assert "careers.down.com" not in json.loads(cache.read_text())  # tried again next time


@respx.mock
@pytest.mark.parametrize(
    ("robots", "page"),
    [
        (httpx.Response(404), httpx.ConnectTimeout("slow")),  # robots.txt answered, the page didn't
        (httpx.Response(503), httpx.Response(200, text=WORKDAY_PAGE)),  # robots.txt 5xx: page never read
        (httpx.Response(404), httpx.Response(503)),
        (httpx.Response(404), httpx.Response(429)),
    ],
)
def test_a_host_that_failed_for_now_is_not_cached(tmp_path, robots, page):
    respx.get("https://careers.flaky.com/robots.txt").mock(return_value=robots)
    respx.get("https://careers.flaky.com/").mock(side_effect=[page])
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        assert discover.survey_hosts(["careers.flaky.com"], client, cache, delay=0) == []
    assert "careers.flaky.com" not in json.loads(cache.read_text())  # tried again next time


@respx.mock
def test_a_host_with_no_platform_is_cached(tmp_path):
    _careers_site("careers.plain.com", page="<p>We are hiring. Email us.</p>")
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        assert discover.survey_hosts(["careers.plain.com"], client, cache, delay=0) == []
    assert json.loads(cache.read_text())["careers.plain.com"]["platforms"] == []


@respx.mock
def test_refresh_keeps_the_cached_boards_of_a_host_that_failed_for_now(tmp_path):
    respx.get("https://careers.acme.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://careers.acme.com/").mock(
        side_effect=[httpx.Response(200, text=WORKDAY_PAGE), httpx.ConnectTimeout("slow")]
    )
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(["careers.acme.com"], client, cache, delay=0)
        again = discover.survey_hosts(["careers.acme.com"], client, cache, delay=0, refresh=True)
    assert [b.slug for b in again] == ["acme/External"]
    assert json.loads(cache.read_text())["careers.acme.com"]["platforms"] == ["workday"]


# ------------------------------------------------------------------ Common Crawl


def test_every_platform_slugs_reads_has_its_hosts():
    assert set(discover.PLATFORM_HOSTS) >= {
        "workday", "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "bamboohr",
        "eightfold", "gem", "rippling", "oracle",
    }
    assert "oracle" not in discover.DEFAULT_PLATFORMS  # all of oraclecloud.com: opt in
    assert discover.prefixes(["gem", "workday"]) == ["com,gem,jobs)", "com,myworkdayjobs,"]


def test_crawl_boards_dedupes_across_crawls_and_skips_known(monkeypatch, tmp_path):
    crawls = {
        "CC-MAIN-2026-39": ["https://jobs.gem.com/acme/1", "https://jobs.gem.com/beta"],
        "CC-MAIN-2026-34": ["https://jobs.gem.com/acme/2", "https://jobs.gem.com/gamma/3"],
    }
    seen_prefixes = []
    monkeypatch.setattr(discover.commoncrawl, "latest_crawls", lambda client, n: list(crawls)[:n])

    def urls(client, crawl, prefixes, cache_dir):
        seen_prefixes.append(list(prefixes))
        return iter(crawls[crawl])

    monkeypatch.setattr(discover.commoncrawl, "urls", urls)
    known = [Company(name="Beta", ats="gem", slug="beta")]
    boards = discover.crawl_boards(None, ["gem"], 2, tmp_path, known)
    assert [(b.ats, b.slug) for b in boards] == [("gem", "acme"), ("gem", "gamma")]
    assert seen_prefixes == [["com,gem,jobs)"], ["com,gem,jobs)"]]


# ------------------------------------------------------------------ the command


def _companies(tmp_path, *entries):
    p = tmp_path / "companies.yaml"
    p.write_text("companies:\n" + "".join(entries) if entries else "companies: []\n")
    return p


@respx.mock
def test_main_writes_boards_from_both_routes_minus_known(monkeypatch, tmp_path):
    monkeypatch.setattr(discover.commoncrawl, "latest_crawls", lambda client, n: ["CC-MAIN-2026-39"])
    monkeypatch.setattr(
        discover.commoncrawl, "urls",
        lambda client, crawl, prefixes, cache_dir: iter(["https://jobs.gem.com/acme/1", "https://jobs.gem.com/known"]),
    )
    _careers_site()
    hosts = tmp_path / "hosts.txt"
    hosts.write_text("careers.acme.com\ncareers-x.icims.com\n")
    known = _companies(tmp_path, "  - name: Known\n    ats: gem\n    slug: known\n")
    out = tmp_path / "discovered.yaml"
    rc = discover.main([
        "--companies", str(known), "--hosts", str(hosts), "--platforms", "gem",
        "--data-dir", str(tmp_path), "-o", str(out), "--delay", "0",
    ])
    assert rc == 0
    text = out.read_text()
    assert "slug: acme\n" in text and "slug: acme/External\n" in text and "known" not in text


def test_main_without_crawl_or_hosts_is_an_error(tmp_path, capsys):
    assert discover.main(["--no-crawl", "--data-dir", str(tmp_path)]) == 2


def test_main_check_drops_boards_with_no_postings(monkeypatch, tmp_path):
    monkeypatch.setattr(discover.commoncrawl, "latest_crawls", lambda client, n: ["CC-MAIN-2026-39"])
    monkeypatch.setattr(
        discover.commoncrawl, "urls",
        lambda client, crawl, prefixes, cache_dir: iter(["https://jobs.gem.com/live", "https://jobs.gem.com/dead"]),
    )
    monkeypatch.setattr(discover.slugs, "check", lambda boards, client, workers: [b for b in boards if b.slug == "live"])
    out = tmp_path / "discovered.yaml"
    rc = discover.main([
        "--companies", str(_companies(tmp_path)), "--platforms", "gem", "--check",
        "--data-dir", str(tmp_path), "-o", str(out),
    ])
    assert rc == 0 and "slug: live" in out.read_text() and "dead" not in out.read_text()


def test_an_unknown_platform_is_an_error(tmp_path, capsys):
    with pytest.raises(SystemExit):
        discover.main(["--platforms", "nosuch", "--data-dir", str(tmp_path)])


@pytest.mark.parametrize("crawls", ["0", "-1"])
def test_crawls_must_be_at_least_one(tmp_path, crawls):
    with pytest.raises(SystemExit):
        discover.main(["--no-crawl", "--crawls", crawls, "--data-dir", str(tmp_path)])
