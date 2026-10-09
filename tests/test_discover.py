"""Discovery: boards from Common Crawl's URLs on known platforms, and from careers hosts."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from jobhunt import discover, settings
from jobhunt.schema import Company


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(discover.fingerprint.time, "sleep", lambda s: None)


NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


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
def test_an_unreachable_host_is_tried_again_after_a_day(tmp_path):
    robots = respx.get("https://careers.down.com/robots.txt").mock(side_effect=httpx.ConnectError("nope"))
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        assert discover.survey_hosts(["careers.down.com"], client, cache, delay=0, now=NOW) == []
        entry = json.loads(cache.read_text())["careers.down.com"]
        assert entry["failures"] == 1 and "boards" not in entry
        discover.survey_hosts(["careers.down.com"], client, cache, delay=0, now=NOW + timedelta(hours=23))
        assert robots.call_count == 1  # too soon
        discover.survey_hosts(["careers.down.com"], client, cache, delay=0, now=NOW + timedelta(hours=25))
    assert robots.call_count == 2
    assert json.loads(cache.read_text())["careers.down.com"]["failures"] == 2


@respx.mock
def test_each_host_is_stamped_with_the_time_it_was_tried(tmp_path, monkeypatch):
    respx.get("https://careers.down.com/robots.txt").mock(side_effect=httpx.ConnectError("nope"))
    _careers_site()
    ticks = iter(NOW + timedelta(hours=h) for h in range(10))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    monkeypatch.setattr(discover, "datetime", Clock)
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(["careers.down.com", "careers.acme.com"], client, cache, delay=0)
    entries = json.loads(cache.read_text())
    assert entries["careers.down.com"]["failed_at"] < entries["careers.acme.com"]["surveyed_at"]


@respx.mock
def test_an_unreachable_host_is_given_up_after_three_tries_until_refresh(tmp_path, caplog):
    robots = respx.get("https://careers.down.com/robots.txt").mock(side_effect=httpx.ConnectError("nope"))
    cache = tmp_path / "hosts.json"
    caplog.set_level("INFO", logger="jobhunt.discover")
    with httpx.Client() as client:
        for day in range(5):
            discover.survey_hosts(["careers.down.com"], client, cache, delay=0, now=NOW + timedelta(days=day))
        assert robots.call_count == settings.DiscoverSettings().max_attempts == 3
        assert "1 given up after 3 tries" in caplog.records[-1].getMessage()
        discover.survey_hosts(["careers.down.com"], client, cache, delay=0, refresh=True, now=NOW + timedelta(days=5))
    assert robots.call_count == 4


@respx.mock
def test_retries_follow_the_discover_settings(tmp_path):
    robots = respx.get("https://careers.down.com/robots.txt").mock(side_effect=httpx.ConnectError("nope"))
    cache = tmp_path / "hosts.json"
    rules = settings.DiscoverSettings(max_attempts=2, retry_after_hours=1)
    with httpx.Client() as client:
        for hours in (0, 0.5, 1, 2, 3):
            discover.survey_hosts(
                ["careers.down.com"], client, cache, delay=0, now=NOW + timedelta(hours=hours), rules=rules
            )
    assert robots.call_count == 2  # at 0 h and 1 h: 0.5 h is too soon, and after 2 tries it's given up


def test_main_passes_the_discover_settings_down(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNT_DISCOVER_MAX_ATTEMPTS", "5")
    seen = {}

    def survey(hosts, client, cache, delay, refresh, rules, workers, limit):
        seen["rules"] = rules
        return []

    monkeypatch.setattr(discover, "survey_hosts", survey)
    hosts = tmp_path / "hosts.txt"
    hosts.write_text("careers.acme.com\n")
    assert discover.main(["--no-crawl", "--hosts", str(hosts), "--data-dir", str(tmp_path)]) == 0
    assert seen["rules"].max_attempts == 5


@respx.mock
def test_a_host_that_failed_before_and_answers_now_is_cached_as_surveyed(tmp_path):
    respx.get("https://careers.acme.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://careers.acme.com/").mock(
        side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, text=WORKDAY_PAGE)]
    )
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(["careers.acme.com"], client, cache, delay=0, now=NOW)
        again = discover.survey_hosts(["careers.acme.com"], client, cache, delay=0, now=NOW + timedelta(days=2))
    assert [b.slug for b in again] == ["acme/External"]
    entry = json.loads(cache.read_text())["careers.acme.com"]
    assert entry["platforms"] == ["workday"] and "failures" not in entry


@respx.mock
def test_progress_counts_the_hosts_to_survey(tmp_path, caplog):
    _careers_site()
    _careers_site("careers.beta.com")
    cache = tmp_path / "hosts.json"
    caplog.set_level("INFO", logger="jobhunt.discover")
    with httpx.Client() as client:
        discover.survey_hosts(["careers.acme.com"], client, cache, delay=0)
        caplog.clear()
        discover.survey_hosts(["careers.acme.com", "careers.beta.com"], client, cache, delay=0)
    messages = [r.getMessage() for r in caplog.records]
    assert "[1/1] careers.beta.com: workday" in messages
    assert messages[0].startswith("2 careers hosts: 1 to survey, 1 cached")


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
def test_a_host_that_failed_for_now_is_cached_as_a_failure(tmp_path, robots, page):
    respx.get("https://careers.flaky.com/robots.txt").mock(return_value=robots)
    respx.get("https://careers.flaky.com/").mock(side_effect=[page])
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        assert discover.survey_hosts(["careers.flaky.com"], client, cache, delay=0) == []
    entry = json.loads(cache.read_text())["careers.flaky.com"]
    assert entry["failures"] == 1 and "boards" not in entry  # tried again after a day


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


PHENOM_PAGE = '<script src="https://cdn.phenompeople.com/CareerConnectResources/x.js"></script>'
PHENOM_SEARCH = {
    "refineSearch": {
        "data": {"jobs": [{"applyUrl": "https://acme.wd5.myworkdayjobs.com/External/job/Seattle/Director_R1"}]},
        "totalHits": 1,
    }
}


@respx.mock
def test_a_platform_whose_board_request_failed_for_now_is_not_cached(tmp_path):
    _careers_site(page=PHENOM_PAGE)
    search = respx.post("https://careers.acme.com/widgets").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=PHENOM_SEARCH)]
    )
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        assert discover.survey_hosts(["careers.acme.com"], client, cache, delay=0, now=NOW) == []
        assert json.loads(cache.read_text())["careers.acme.com"]["failures"] == 1
        again = discover.survey_hosts(["careers.acme.com"], client, cache, delay=0, now=NOW + timedelta(days=2))
    assert search.call_count == 2
    assert [(b.ats, b.slug) for b in again] == [("workday", "acme/External")]


@respx.mock
def test_refresh_keeps_the_cached_boards_when_a_board_request_failed_for_now(tmp_path):
    _careers_site(page=PHENOM_PAGE)
    respx.post("https://careers.acme.com/widgets").mock(
        side_effect=[httpx.Response(200, json=PHENOM_SEARCH), httpx.Response(503)]
    )
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(["careers.acme.com"], client, cache, delay=0)
        again = discover.survey_hosts(["careers.acme.com"], client, cache, delay=0, refresh=True)
    assert [b.slug for b in again] == ["acme/External"]
    assert [b["slug"] for b in json.loads(cache.read_text())["careers.acme.com"]["boards"]] == ["acme/External"]


@respx.mock
def test_a_host_that_redirects_forever_is_cached(tmp_path):
    respx.get("https://careers.loop.com/robots.txt").mock(return_value=httpx.Response(404))
    page = respx.get("https://careers.loop.com/").mock(
        return_value=httpx.Response(302, headers={"location": "https://careers.loop.com/"})
    )
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(["careers.loop.com"], client, cache, delay=0)
        calls = page.call_count
        discover.survey_hosts(["careers.loop.com"], client, cache, delay=0)
    assert json.loads(cache.read_text())["careers.loop.com"]["platforms"] == []
    assert page.call_count == calls  # a permanent failure: not tried again


@respx.mock
def test_several_hosts_surveyed_at_once_give_their_boards_in_host_order(tmp_path):
    hosts = ["careers.acme.com", "careers.beta.com", "careers.gamma.com"]
    for host in hosts:
        name = host.split(".")[1]
        _careers_site(host, page=WORKDAY_PAGE.replace("acme", name))
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        boards = discover.survey_hosts(hosts, client, cache, delay=0, workers=3)
    assert [b.slug for b in boards] == ["acme/External", "beta/External", "gamma/External"]
    assert set(json.loads(cache.read_text())) == set(hosts)


@respx.mock
def test_limit_surveys_at_most_n_hosts_and_leaves_the_rest_for_later(tmp_path, caplog):
    first = _careers_site("careers.acme.com")
    second = _careers_site("careers.beta.com")
    cache = tmp_path / "hosts.json"
    caplog.set_level("INFO", logger="jobhunt.discover")
    with httpx.Client() as client:
        discover.survey_hosts(["careers.acme.com", "careers.beta.com"], client, cache, delay=0, limit=1)
        assert (first.call_count, second.call_count) == (1, 0)
        assert list(json.loads(cache.read_text())) == ["careers.acme.com"]
        assert "1 left for a later run" in caplog.records[0].getMessage()
        discover.survey_hosts(["careers.acme.com", "careers.beta.com"], client, cache, delay=0, limit=1)
    assert (first.call_count, second.call_count) == (1, 1)  # the next run picks up the rest


@respx.mock
def test_refresh_with_a_limit_surveys_the_least_recently_tried_hosts_first(tmp_path):
    hosts = ["careers.acme.com", "careers.beta.com", "careers.gamma.com"]
    pages = [_careers_site(host) for host in hosts]
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(hosts, client, cache, delay=0, now=NOW)
        for day in (1, 2, 3):
            discover.survey_hosts(hosts, client, cache, delay=0, refresh=True, limit=1, now=NOW + timedelta(days=day))
            assert [p.call_count for p in pages] == [1 + (i < day) for i in range(3)]


@respx.mock
def test_hosts_whose_owners_forbid_collection_get_no_requests(tmp_path, caplog):
    _careers_site()
    hosts = ["careers.facebook.com", "metacareers.com", "jobs.instagram.com", "careers.google.com", "careers.acme.com"]
    caplog.set_level("INFO", logger="jobhunt.discover")
    with httpx.Client() as client:
        boards = discover.survey_hosts(hosts, client, tmp_path / "hosts.json", delay=0)
    assert [b.slug for b in boards] == ["acme/External"]
    assert {c.request.url.host for c in respx.calls} == {"careers.acme.com"}
    assert any("skipped 4 hosts whose owners forbid it" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("host", "excluded"),
    [("facebook.com", True), ("careers.meta.com", True), ("a.b.youtube.com", True),
     ("careers.notfacebook.com", False), ("meta.com.acme.io", False), ("careers.acme.com", False)],
)
def test_excluded_matches_the_domain_or_any_subdomain(host, excluded):
    assert (discover.excluded(host) is not None) is excluded


def test_the_host_client_carries_the_configured_rate_caps():
    fetch = settings.FetchSettings(max_rate={"workable": 0.5})
    with discover._host_client(fetch, workers=4) as client:
        transport = client._transport
        group = discover.throttle.request_group(httpx.URL("https://apply.workable.com/acme/"))
        assert transport.limiter(group).rate == 0.5
    with discover._host_client(settings.FetchSettings(), workers=4) as client:
        assert client._transport.limiter(group).rate == settings.FetchSettings().max_rate["workable"]


@respx.mock
def test_the_cache_is_saved_every_few_seconds_not_after_every_host(tmp_path, monkeypatch):
    hosts = [f"careers.co{n}.com" for n in range(20)]
    for host in hosts:
        _careers_site(host)
    writes = []
    write = discover.storage._write_atomic
    monkeypatch.setattr(discover.storage, "_write_atomic", lambda path, text: (writes.append(path), write(path, text)))
    cache = tmp_path / "hosts.json"
    with httpx.Client() as client:
        discover.survey_hosts(hosts, client, cache, delay=0, workers=4)
    assert len(writes) <= 2
    assert set(json.loads(cache.read_text())) == set(hosts)


@respx.mock
def test_an_interrupted_survey_stops_its_requests_and_keeps_what_it_found(tmp_path, monkeypatch):
    _careers_site("careers.done.com")
    _careers_site("careers.slow.com")
    finished = threading.Event()
    survey_one = discover._survey_one

    def survey(client, host, delay, now, stop):
        if host == "careers.boom.com":  # Ctrl-C, once the first host is done
            finished.wait(5)
            raise KeyboardInterrupt
        result = survey_one(client, host, 0 if host == "careers.done.com" else 30, now, stop)
        finished.set()
        return result

    monkeypatch.setattr(discover, "_survey_one", survey)
    cache = tmp_path / "hosts.json"
    hosts = ["careers.done.com", "careers.boom.com", "careers.slow.com"]
    started = time.monotonic()
    with discover._host_client(settings.FetchSettings(), workers=3) as client, pytest.raises(KeyboardInterrupt):
        discover.survey_hosts(hosts, client, cache, delay=0, workers=3)
    assert time.monotonic() - started < 5  # the slow host's wait was cut short
    assert client._transport._stop.is_set()
    assert set(json.loads(cache.read_text())) == {"careers.done.com"}  # the stopped host isn't a failure


def test_main_webgraph_surveys_its_hosts_with_generic_or_listed_tlds(monkeypatch, tmp_path):
    monkeypatch.setattr(discover.commoncrawl, "latest_graph", lambda client: "cc-main-2026-jul-aug-sep")
    seen = {}

    def hosts(client, release, labels, cache_dir):
        seen["release"], seen["labels"] = release, labels
        return ["careers.acme.com", "careers.acme.ca", "jobs.beta.io"]

    def survey(hosts, client, cache, delay, refresh, rules, workers, limit):
        seen.update(hosts=hosts, workers=workers, limit=limit)
        return []

    monkeypatch.setattr(discover.commoncrawl, "webgraph_hosts", hosts)
    monkeypatch.setattr(discover, "survey_hosts", survey)
    rc = discover.main(["--no-crawl", "--webgraph", "--limit", "50", "--data-dir", str(tmp_path)])
    assert rc == 0
    assert seen["release"] == "cc-main-2026-jul-aug-sep" and "careers" in seen["labels"]
    assert seen["hosts"] == ["careers.acme.com", "jobs.beta.io"]  # .ca is a country code not listed
    assert (seen["workers"], seen["limit"]) == (8, 50)


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
    monkeypatch.setattr(discover.slugs, "check", lambda boards, client, workers, progress_every: [b for b in boards if b.slug == "live"])
    out = tmp_path / "discovered.yaml"
    rc = discover.main([
        "--companies", str(_companies(tmp_path)), "--platforms", "gem", "--check",
        "--data-dir", str(tmp_path), "-o", str(out),
    ])
    assert rc == 0 and "slug: live" in out.read_text() and "dead" not in out.read_text()


def test_main_interrupted_while_checking_leaves_the_unchecked_boards(monkeypatch, tmp_path):
    monkeypatch.setattr(discover.commoncrawl, "latest_crawls", lambda client, n: ["CC-MAIN-2026-39"])
    monkeypatch.setattr(
        discover.commoncrawl, "urls",
        lambda client, crawl, prefixes, cache_dir: iter(["https://jobs.gem.com/live", "https://jobs.gem.com/dead"]),
    )

    def interrupted(boards, client, workers, progress_every):
        raise KeyboardInterrupt

    monkeypatch.setattr(discover.slugs, "check", interrupted)
    out = tmp_path / "discovered.yaml"
    rc = discover.main([
        "--companies", str(_companies(tmp_path)), "--platforms", "gem", "--check",
        "--data-dir", str(tmp_path), "-o", str(out),
    ])
    assert rc == 130 and "slug: live" in out.read_text() and "slug: dead" in out.read_text()


def test_main_interrupted_while_surveying_exits_quietly(monkeypatch, tmp_path):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(discover, "survey_hosts", interrupted)
    hosts = tmp_path / "hosts.txt"
    hosts.write_text("careers.acme.com\n")
    rc = discover.main(["--no-crawl", "--hosts", str(hosts), "--data-dir", str(tmp_path)])
    assert rc == 130 and not (tmp_path / "discovered.yaml").exists()


def test_an_unknown_platform_is_an_error(tmp_path, capsys):
    with pytest.raises(SystemExit):
        discover.main(["--platforms", "nosuch", "--data-dir", str(tmp_path)])


@pytest.mark.parametrize("crawls", ["0", "-1"])
def test_crawls_must_be_at_least_one(tmp_path, crawls):
    with pytest.raises(SystemExit):
        discover.main(["--no-crawl", "--crawls", crawls, "--data-dir", str(tmp_path)])
