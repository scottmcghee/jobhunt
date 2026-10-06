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
        ("https://apply.workable.com/huggingface/", ("workable", "huggingface")),
        ("https://apply.workable.com/Kettle-And-Fire/j/A1B2C3D4E5/apply", ("workable", "kettle-and-fire")),
        ("https://apply.workable.com/acme/jobs/12345", ("workable", "acme")),
        ("https://designer-group-1.workable.com/jobs/1234567", ("workable", "designer-group-1")),
        ("https://mux.workable.com/j/A1B2C3D4E5", ("workable", "mux")),
        ("https://evolve.bamboohr.com/careers", ("bamboohr", "evolve")),
        ("https://Evolve.bamboohr.com/careers/46?source=x", ("bamboohr", "evolve")),
        ("https://acme.bamboohr.com/jobs/view.php?id=12", ("bamboohr", "acme")),
        ("https://acme.bamboohr.com/careers/list", ("bamboohr", "acme")),
        ("https://eaton.eightfold.ai/careers/job/687239400802", ("eightfold", "eaton.eightfold.ai")),
        ("https://fa-exty-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/118142",
         ("oracle", "fa-exty-saasfaprod1.fa.ocs.oraclecloud.com/CX_1")),
        ("https://EEHO.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/fr-CA/sites/CX_45001/requisitions?keyword=x",
         ("oracle", "eeho.fa.us2.oraclecloud.com/CX_45001")),
        ("https://efzu.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CityOfParramattaCareers",
         ("oracle", "efzu.fa.em2.oraclecloud.com/CityOfParramattaCareers")),
        ("https://Eaton.eightfold.ai/careers?query=director&pid=1", ("eightfold", "eaton.eightfold.ai")),
        ("https://350.bamboohr.com/careers/32", ("bamboohr", "350")),  # all-digit names are real
        ("https://apply.workable.com/1871", ("workable", "1871")),
        ("https://apply.workable.com/12345/j/A1B2C3D4E5", ("workable", "12345")),
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
        ("https://apply.workable.com/", None),
        ("https://apply.workable.com/j/A1B2C3D4E5", None),  # a short link names no account
        ("https://apply.workable.com/api/v1/widget/accounts/acme", None),
        ("https://apply.workable.com/robots.txt", None),
        ("https://jobs.workable.com/company/1kLpatGRMtQjAU1NRRNCyp/jobs-at-acme", None),  # Workable's job search
        ("https://resources.workable.com/stories-and-insights/x", None),
        ("https://help.workable.com/hc/en-us/articles/1", None),
        ("https://www.workable.com/post-jobs-for-free", None),
        ("https://events.workable.com/", None),  # an account board's path is /jobs or /j
        ("https://acme.workable.com/", None),
        ("https://www.workable.com/jobs/1", None),
        ("https://www.bamboohr.com/careers", None),
        ("https://help.bamboohr.com/s/article/1", None),
        ("https://documentation.bamboohr.com/reference", None),
        ("https://partners.bamboohr.com/careers", None),
        ("https://acme.bamboohr.com/login.php", None),
        ("https://acme.bamboohr.com/", None),
        ("https://workablelifesolutions.com/careers", None),
        ("https://acme.bamboohr.com.evil.example/careers", None),
        ("https://eightfold.ai/careers", None),  # Eightfold's own site
        ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/", None),
        ("https://eeho.fa.us2.oraclecloud.com/hcmUI/faces/AtkHomePageWelcome", None),  # the HCM app itself
        ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/job/11138", None),  # no site
        ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/errors/404", None),
        ("https://ab-.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1", None),  # not a valid host
        ("https://ab-.eightfold.ai/careers", None),
        ("https://www.oracle.com/careers/", None),
        ("https://eeho.fa.us2.oraclecloud.com.evil.example/hcmUI/CandidateExperience/en/sites/CX_1", None),
        ("https://www.eightfold.ai/careers", None),
        ("https://app.eightfold.ai/careers", None),
        ("https://community.eightfold.ai/careers", None),
        ("https://eaton.eightfold.ai/events/candidate/landing", None),  # not the careers site
        ("https://eaton.eightfold.ai/", None),
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


def test_discover_keeps_one_oracle_board_per_host():
    # the search ignores the site, so a second site on a host would repeat the first one's postings
    urls = [
        "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/job/1",
        "https://EEHO.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/jobsearch/job/2",
        "https://ehzq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/GrantThorntonBermuda",
    ]
    assert [c.slug for c in slugs.discover(urls)] == [
        "eeho.fa.us2.oraclecloud.com/CX_45001",
        "ehzq.fa.us2.oraclecloud.com/GrantThorntonBermuda",
    ]
    known = [Company(name="eeho", ats="oracle", slug="Eeho.fa.us2.oraclecloud.com/CX_1")]
    assert [c.slug for c in slugs.discover(urls[:2], known)] == []


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
def test_check_drops_a_board_it_cannot_read_and_carries_on(caplog):
    # an Eightfold careers page with no domain raises ValueError: unfetchable as configured
    respx.get("https://gone.eightfold.ai/careers").mock(return_value=httpx.Response(200, text="<html>no config</html>"))
    respx.get(GH.format("live")).mock(
        return_value=httpx.Response(200, json={"jobs": [{"id": 1, "title": "x", "absolute_url": "https://x", "location": {"name": "y"}}]})
    )
    boards = [
        Company(name="gone", ats="eightfold", slug="gone.eightfold.ai"),
        Company(name="live", ats="greenhouse", slug="live"),
    ]
    with httpx.Client() as client:
        kept = slugs.check(boards, client, workers=1)
    assert [c.slug for c in kept] == ["live"]
    assert "no Eightfold domain" in caplog.text


@respx.mock
def test_check_keeps_a_board_that_answers_with_a_page_that_is_not_json(caplog):
    # a gateway or proxy page served with 200: the board can't be checked right now, not dead
    respx.get(GH.format("proxied")).mock(
        return_value=httpx.Response(200, text="<html>502 Bad Gateway</html>", headers={"content-type": "text/html"})
    )
    boards = [Company(name="proxied", ats="greenhouse", slug="proxied")]
    with httpx.Client() as client:
        assert slugs.check(boards, client, workers=1) == boards
    assert "proxied: kept, could not check (not JSON" in caplog.text


@respx.mock
def test_check_keeps_a_board_that_answers_with_a_page_that_is_neither_json_nor_utf8(caplog):
    # httpx's .json() raises UnicodeDecodeError here, not JSONDecodeError
    page = "<html>Passerelle indisponible - réessayez</html>"
    respx.get(GH.format("latin")).mock(
        return_value=httpx.Response(
            200, content=page.encode("latin-1"), headers={"content-type": "text/html; charset=iso-8859-1"}
        )
    )
    boards = [Company(name="latin", ats="greenhouse", slug="latin")]
    with httpx.Client() as client:
        assert slugs.check(boards, client, workers=1) == boards
    assert "latin: kept, could not check (not JSON" in caplog.text


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
    monkeypatch.setattr(slugs, "_client", lambda fetch: httpx.Client())
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


def test_the_check_client_is_throttled_like_fetch(monkeypatch):
    # --check once sent unthrottled bursts that got the IP banned by Workable's Cloudflare
    monkeypatch.setenv("JOBHUNT_FETCH_MAX_RATE", '{"workable": 1.5}')
    monkeypatch.setenv("JOBHUNT_FETCH_PER_HOST", "3")
    with slugs._client() as client:
        transport = client._transport
        assert isinstance(transport, slugs.throttle.ThrottledTransport)
        assert transport.limiter("workable").rate == 1.5
        assert transport.limiter("greenhouse").rate is None
        assert transport.limiter("greenhouse").ceiling == 3
        assert client.follow_redirects


def test_main_check_with_a_bad_setting_is_a_friendly_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JOBHUNT_SLUGS_CHECK_WORKERS", "lots")
    out = tmp_path / "out.yaml"
    assert slugs.main([str(_index_with(tmp_path, "https://boards.greenhouse.io/live")), "-o", str(out), "--check"]) == 2
    assert "JOBHUNT_SLUGS_CHECK_WORKERS" in caplog.text
    assert not out.exists()


def test_main_check_passes_its_settings_down(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNT_SLUGS_CHECK_WORKERS", "3")
    monkeypatch.setenv("JOBHUNT_FETCH_USER_AGENT", "test-agent/1")
    seen = {}

    def client(fetch):
        seen["agent"] = fetch.user_agent
        return httpx.Client()

    def check(found, client, workers):
        seen["workers"] = workers
        return found

    monkeypatch.setattr(slugs, "_client", client)
    monkeypatch.setattr(slugs, "check", check)
    monkeypatch.setattr(slugs, "check_workers", lambda: pytest.fail("settings loaded again"))
    assert slugs.main([str(_index_with(tmp_path, "https://boards.greenhouse.io/live")), "-o", str(tmp_path / "o"), "--check"]) == 0
    assert seen == {"agent": "test-agent/1", "workers": 3}


def test_check_pool_size_comes_from_the_argument_or_settings(monkeypatch):
    sizes = []
    real = slugs.ThreadPoolExecutor
    monkeypatch.setattr(slugs, "ThreadPoolExecutor", lambda n: sizes.append(n) or real(n))
    monkeypatch.setenv("JOBHUNT_SLUGS_CHECK_WORKERS", "3")
    with httpx.Client() as client:
        slugs.check([], client, workers=2)
        slugs.check([], client)
    assert sizes == [2, 3]


def test_render_writes_an_eightfold_location():
    board = Company(name="eaton", ats="eightfold", slug="eaton.eightfold.ai", location="United States")
    text = slugs.render([board])
    assert "    location: United States\n" in text
    loaded = yaml.safe_load("companies:\n" + text)["companies"][0]
    assert Company.model_validate(loaded) == board
