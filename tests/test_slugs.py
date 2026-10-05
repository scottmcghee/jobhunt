"""Board-slug harvesting from Common Crawl index dumps."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import yaml

from jobhunt import slugs
from jobhunt.schema import Company


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/0x/jobs/4209667002?ref=x.com", ("greenhouse", "0x")),
        ("https://boards.greenhouse.io/1047games/", ("greenhouse", "1047games")),
        ("https://job-boards.greenhouse.io/figma?error=true", ("greenhouse", "figma")),
        ("https://job-boards.anz.greenhouse.io/dawnaerospace/jobs/1", ("greenhouse", "dawnaerospace")),
        ("https://job-boards.eu.greenhouse.io/acme", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/Convene/jobs/8053971", ("greenhouse", "convene")),
        ("https://boards.greenhouse.io/dicefm-careers/jobs/1", ("greenhouse", "dicefm-careers")),
        ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/embed/job_app?token=1&for=Acme", ("greenhouse", "acme")),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", ("greenhouse", "acme")),
        ("https://jobs.lever.co/Timely/abc-123/apply", ("lever", "timely")),
        ("https://jobs.ashbyhq.com/counterpart/abc-123", ("ashby", "counterpart")),
        ("https://jobs.ashbyhq.com/Some%20Co", ("ashby", "Some Co")),
        ("https://jobs.smartrecruiters.com/AbbVie/3743990009679496-manager?trid=x", ("smartrecruiters", "AbbVie")),
        ("https://careers.smartrecruiters.com/AveryDennison", ("smartrecruiters", "AveryDennison")),
        ("https://careers.smartrecruiters.com/BoydGaming/main-street-station", ("smartrecruiters", "BoydGaming")),
        ("https://careers.smartrecruiters.com/Evooq?location=Zurich", ("smartrecruiters", "Evooq")),
        # not boards
        ("https://www.greenhouse.io/blog/5-culture-fit-questions", None),
        ("https://app7.greenhouse.io/ai_opt_out_request/job_post/4823689007/ai_opt_out", None),
        ("https://api.greenhouse.io/users/sign_in", None),
        # board hosts, but no slug
        ("https://boards.greenhouse.io/", None),
        ("https://job-boards.anz.greenhouse.io/robots.txt", None),
        ("https://boards.greenhouse.io/embed/job_board", None),
        ("https://boards-api.greenhouse.io/v1/boards/", None),
        ("https://jobs.smartrecruiters.com/", None),
        ("https://jobs.smartrecruiters.com/robots.txt", None),
        ("https://jobs.smartrecruiters.com/oneclick-ui/company/X/publication/1", None),
        ("https://jobs.smartrecruiters.com/my-applications", None),
        ("https://careers.smartrecruiters.com/external-referrals", None),
        ("https://www.smartrecruiters.com/blog", None),
        ("http://[bad", None),
    ],
)
def test_board_from_url(url, expected):
    board = slugs.board_from_url(url)
    assert (board.ats, board.slug) == expected if expected else board is None


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://adobe.wd5.myworkdayjobs.com/external_experienced", "adobe/external_experienced"),
        ("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/San-Jose/X_R1", "adobe/external_experienced"),
        ("https://Abbott.wd5.myworkdayjobs.com/abbottcareers2/job/X", "abbott/abbottcareers2"),
        ("https://alcoa.wd5.myworkdayjobs.com/es/careers", "alcoa/careers"),
        ("https://agilent.wd5.myworkdayjobs.com/en-us/Agilent_Careers", "agilent/Agilent_Careers"),
        ("https://acme.wd12.myworkdayjobs.com/External/details/X_R2", "acme/External"),
    ],
)
def test_board_from_workday_url(url, slug):
    board = slugs.board_from_url(url)
    assert (board.ats, board.slug, board.name) == ("workday", slug, slug.split("/")[0])
    assert board.datacenter == url.split(".")[1]


@pytest.mark.parametrize(
    "url",
    [
        "https://adobe.wd5.myworkdayjobs.com/",
        "https://adobe.wd5.myworkdayjobs.com/robots.txt",
        "https://abbott.wd5.myworkdayjobs.com/llms.txt",
        "https://adobe.wd5.myworkdayjobs.com/en-US",
        "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external/jobs",
        "https://www.myworkday.com/adobe",
    ],
)
def test_workday_urls_without_a_site(url):
    assert slugs.board_from_url(url) is None


def test_discover_dedupes_workday_sites_case_insensitively():
    urls = [
        "https://aaamidatlantic.wd5.myworkdayjobs.com/AAAMidAtlantic/job/1",
        "https://aaamidatlantic.wd5.myworkdayjobs.com/aaamidatlantic",
        "https://aaamidatlantic.wd5.myworkdayjobs.com/External_Career_ASE",
    ]
    assert [c.slug for c in slugs.discover(urls)] == [
        "aaamidatlantic/AAAMidAtlantic",
        "aaamidatlantic/External_Career_ASE",
    ]


def test_render_includes_workday_datacenter():
    c = Company(name="adobe", ats="workday", slug="adobe/external_experienced", datacenter="wd5")
    out = slugs.render([c])
    assert "    datacenter: wd5\n" in out
    assert Company.model_validate(yaml.safe_load("companies:\n" + out)["companies"][0]) == c


def test_discover_dedupes_and_sorts():
    urls = [
        "https://jobs.lever.co/zeta/1",
        "https://boards.greenhouse.io/beta/jobs/1",
        "https://boards.greenhouse.io/alpha/jobs/1",
        "https://job-boards.greenhouse.io/Beta/jobs/2",
        "https://www.greenhouse.io/",
    ]
    found = slugs.discover(urls)
    assert [(c.ats, c.slug) for c in found] == [
        ("greenhouse", "alpha"),
        ("greenhouse", "beta"),
        ("lever", "zeta"),
    ]
    assert all(c.name == c.slug and c.tags == [] for c in found)


def test_discover_skips_known_companies():
    known = [Company(name="Alpha", ats="greenhouse", slug="alpha")]
    urls = ["https://boards.greenhouse.io/alpha", "https://jobs.lever.co/alpha"]
    assert [(c.ats, c.slug) for c in slugs.discover(urls, known)] == [("lever", "alpha")]


def test_discover_dedupes_ashby_case_insensitively():
    urls = ["https://jobs.ashbyhq.com/Acme/1", "https://jobs.ashbyhq.com/acme/2"]
    assert [c.slug for c in slugs.discover(urls)] == ["Acme"]


def test_discover_dedupes_smartrecruiters_case_insensitively():
    urls = ["https://jobs.smartrecruiters.com/ServiceNow/1-a", "https://careers.smartrecruiters.com/servicenow"]
    assert [c.slug for c in slugs.discover(urls)] == ["ServiceNow"]


def test_render_matches_companies_yaml_style():
    out = slugs.render([Company(name="alpha", ats="greenhouse", slug="alpha")])
    assert out == "  - name: alpha\n    ats: greenhouse\n    slug: alpha\n    tags: []\n"


def test_render_roundtrips_awkward_slugs():
    awkward = ["0x", "123", "true", "null", "1e3", "Some Co", "a-b.c"]
    companies = [Company(name=s, ats="ashby", slug=s) for s in awkward]
    loaded = yaml.safe_load("companies:\n" + slugs.render(companies))["companies"]
    assert [Company.model_validate(c) for c in loaded] == companies


def test_render_empty_is_empty():
    assert slugs.render([]) == ""


def test_read_urls_from_cdx_index_lines(tmp_path):
    # raw Common Crawl index lines, as grep prints them: SURT key, timestamp, JSON
    p = tmp_path / "cc.txt"
    p.write_text(
        'com,myworkdayjobs,wd5,adobe)/external 20260910104126 {"url": "https://adobe.wd5.myworkdayjobs.com/External", "status": "200"}\n'
        'com,myworkdayjobs,wd3,lonza)/x/apply?source={pipeline_id} 20260915181741 {"url": "https://lonza.wd3.myworkdayjobs.com/Lonza_Careers/job/x/apply?source={pipeline_id}", "mime": "text/html"}\n'
    )
    assert list(slugs.read_urls(p)) == [
        "https://adobe.wd5.myworkdayjobs.com/External",
        "https://lonza.wd3.myworkdayjobs.com/Lonza_Careers/job/x/apply?source={pipeline_id}",
    ]


def test_read_urls_from_any_text(tmp_path):
    p = tmp_path / "mixed.txt"
    p.write_text(
        json.dumps({"url": "https://boards.greenhouse.io/alpha", "status": "301"})
        + "\n\nno links here\n"
        + "https://jobs.lever.co/beta\n"
        + '<a href="https://jobs.ashbyhq.com/gamma/1">Apply</a> or http://boards.greenhouse.io/delta.\n'
        + "(see https://jobs.lever.co/epsilon), https://jobs.lever.co/zeta;\n"
        + "[https://jobs.lever.co/eta/a_(b)]\n"
    )
    assert list(slugs.read_urls(p)) == [
        "https://boards.greenhouse.io/alpha",
        "https://jobs.lever.co/beta",
        "https://jobs.ashbyhq.com/gamma/1",
        "http://boards.greenhouse.io/delta",
        "https://jobs.lever.co/epsilon",
        "https://jobs.lever.co/zeta",
        "https://jobs.lever.co/eta/a_(b)",
    ]


def test_read_urls_survives_bad_bytes(tmp_path):
    p = tmp_path / "bad.txt"
    p.write_bytes(b"\xff\xfe junk https://jobs.lever.co/alpha\n")
    assert list(slugs.read_urls(p)) == ["https://jobs.lever.co/alpha"]


def test_main_reads_cdx_index_lines(tmp_path):
    index = tmp_path / "cc.txt"
    index.write_text(
        'com,myworkdayjobs,wd5,adobe)/en-us/external/job/x 20260910104126 {"url": "https://adobe.wd5.myworkdayjobs.com/en-US/External/job/X"}\n'
        'com,lever,jobs)/gamma 20260910104127 {"url": "https://jobs.lever.co/gamma"}\n'
    )
    out = tmp_path / "out.yaml"
    assert slugs.main([str(index), "-o", str(out)]) == 0
    found = yaml.safe_load("companies:\n" + out.read_text())["companies"]
    assert [(c["ats"], c["slug"]) for c in found] == [("lever", "gamma"), ("workday", "adobe/External")]


def test_main_writes_pasteable_yaml(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"url": "https://boards.greenhouse.io/alpha/jobs/1"}) + "\n")
    b.write_text(
        json.dumps({"url": "https://boards.greenhouse.io/known/jobs/1"})
        + "\n"
        + json.dumps({"url": "https://jobs.lever.co/gamma"})
        + "\n"
    )
    existing = tmp_path / "companies.yaml"
    existing.write_text("companies:\n  - name: Known\n    ats: greenhouse\n    slug: known\n")
    out = tmp_path / "out.yaml"

    rc = slugs.main([str(a), str(b), "--companies", str(existing), "-o", str(out)])

    assert rc == 0
    merged = yaml.safe_load(existing.read_text() + out.read_text())["companies"]
    assert [(c["ats"], c["slug"]) for c in merged] == [
        ("greenhouse", "known"),
        ("greenhouse", "alpha"),
        ("lever", "gamma"),
    ]


def test_default_index_is_a_text_file():
    assert slugs.DEFAULT_INDEX.name == "commoncrawl.txt"


def test_main_missing_input_is_an_error(tmp_path):
    assert slugs.main([str(tmp_path / "nope.json"), "-o", str(tmp_path / "out.yaml")]) == 2


GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
SR = "https://api.smartrecruiters.com/v1/companies/{}/postings"
EMPTY_SR = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}


@respx.mock
def test_check_keeps_only_boards_with_jobs(fixture_json, caplog):
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    respx.get(GH.format("gone")).mock(return_value=httpx.Response(404))
    respx.get(SR.format("Empty")).mock(return_value=httpx.Response(200, json=EMPTY_SR))
    respx.get(SR.format("Locked")).mock(return_value=httpx.Response(401))
    boards = [
        Company(name="live", ats="greenhouse", slug="live"),
        Company(name="gone", ats="greenhouse", slug="gone"),
        Company(name="Empty", ats="smartrecruiters", slug="Empty"),
        Company(name="Locked", ats="smartrecruiters", slug="Locked"),
    ]
    caplog.set_level("INFO", logger="jobhunt.slugs")
    with httpx.Client() as client:
        kept = slugs.check(boards, client)
    assert [c.slug for c in kept] == ["live"]
    assert "gone: HTTP 404" in caplog.text and "Locked: HTTP 401" in caplog.text
    assert "Empty: no open postings" in caplog.text


@respx.mock
def test_check_keeps_boards_it_could_not_reach(caplog):
    respx.get(GH.format("flaky")).mock(return_value=httpx.Response(503))
    respx.get(GH.format("slow")).mock(side_effect=httpx.ConnectTimeout("timed out"))
    boards = [Company(name=s, ats="greenhouse", slug=s) for s in ("flaky", "slow")]
    with httpx.Client() as client:
        assert slugs.check(boards, client) == boards
    assert "flaky: kept, could not check" in caplog.text and "slow: kept, could not check" in caplog.text


@respx.mock
def test_check_does_not_fetch_descriptions(fixture_json):
    respx.get(SR.format("Acme")).mock(
        return_value=httpx.Response(200, json=fixture_json("smartrecruiters_postings.json"))
    )
    detail = respx.get(url__startswith=SR.format("Acme") + "/")
    board = Company(name="Acme", ats="smartrecruiters", slug="Acme")
    with httpx.Client() as client:
        assert slugs.check([board], client) == [board]
    assert detail.call_count == 0


def _index_with(tmp_path, *urls):
    index = tmp_path / "cc.txt"
    index.write_text("".join(u + "\n" for u in urls))
    return index


@respx.mock
def test_main_check_drops_dead_boards(tmp_path, monkeypatch, fixture_json):
    monkeypatch.setattr(slugs, "_client", httpx.Client)
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    respx.get(GH.format("gone")).mock(return_value=httpx.Response(404))
    index = _index_with(tmp_path, "https://boards.greenhouse.io/live", "https://boards.greenhouse.io/gone")
    out = tmp_path / "out.yaml"
    assert slugs.main([str(index), "-o", str(out), "--check"]) == 0
    found = yaml.safe_load("companies:\n" + out.read_text())["companies"]
    assert [c["slug"] for c in found] == ["live"]


@respx.mock
def test_main_without_check_makes_no_requests(tmp_path):
    # respx.mock fails any request that has no route, so this passes only if none is made
    index = _index_with(tmp_path, "https://boards.greenhouse.io/live")
    out = tmp_path / "out.yaml"
    assert slugs.main([str(index), "-o", str(out)]) == 0
    assert "slug: live" in out.read_text()


@respx.mock
def test_check_reads_only_the_first_page():
    page = {"offset": 0, "limit": 100, "totalFound": 5000, "content": [{"id": "1", "name": "Job", "location": {}}]}
    listing = respx.get(SR.format("Big")).mock(return_value=httpx.Response(200, json=page))
    board = Company(name="Big", ats="smartrecruiters", slug="Big")
    with httpx.Client() as client:
        assert slugs.check([board], client) == [board]
    assert listing.call_count == 1


@respx.mock
def test_check_keeps_rate_limited_boards(caplog):
    respx.get(GH.format("busy")).mock(return_value=httpx.Response(429))
    board = Company(name="busy", ats="greenhouse", slug="busy")
    with httpx.Client() as client:
        assert slugs.check([board], client) == [board]
    assert "busy: kept, could not check (HTTP 429)" in caplog.text


def test_check_workers_and_client_come_from_settings(monkeypatch):
    monkeypatch.setenv("JOBHUNT_SLUGS_CHECK_WORKERS", "2")
    monkeypatch.setenv("JOBHUNT_FETCH_TIMEOUT", "7")
    monkeypatch.setenv("JOBHUNT_FETCH_USER_AGENT", "test-agent/1")
    with slugs._client() as client:
        assert client.timeout.read == 7 and client.headers["User-Agent"] == "test-agent/1"
    assert slugs.check_workers() == 2
