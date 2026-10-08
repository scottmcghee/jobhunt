"""The S&P 500 careers survey (scripts/survey_careers.py). Network mocked with respx."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from jobhunt.schema import Company

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "survey_careers.py"
_spec = importlib.util.spec_from_file_location("survey_careers", _SCRIPT)
survey = importlib.util.module_from_spec(_spec)
sys.modules["survey_careers"] = survey  # dataclasses look their module up while it loads
_spec.loader.exec_module(survey)

WIKITEXT = """
Some intro.
{| class="wikitable sortable" id="constituents"
|-
! Symbol !! Security !! GICS Sector !! GICS Sub-Industry
|-
| {{NyseSymbol|MMM}} || [[3M]] || Industrials || Industrial Conglomerates
|-
| {{NasdaqSymbol|AAPL}} || [[Apple Inc.]] || Information Technology || Technology Hardware
|-
| {{NyseSymbol|BRK.B}} || [[Berkshire Hathaway]] || Financials || Multi-Sector Holdings
|-
| {{NyseSymbol|HWM}} || [[Howmet Aerospace|Howmet]] || Industrials || Aerospace & Defense
|-
|| {{NyseSymbol|RMD}}
|| [[ResMed]]|
|| Health Care
|| Health Care Equipment
|}
{| class="wikitable" id="changes"
|-
| {{NyseSymbol|OLD}} || [[Old Co]] || Energy || Oil
|}
"""

SPARQL = {
    "results": {
        "bindings": [
            {"cLabel": {"value": "3M"}, "site": {"value": "https://www.3m.com/"}, "tick": {"value": "MMM"}},
            {"cLabel": {"value": "Apple Inc."}, "site": {"value": "https://www.apple.com"}, "tick": {"value": "AAPL"}},
            {"cLabel": {"value": "Howmet Aerospace"}, "site": {"value": "http://howmet.com"}, "tick": {"value": "HWM"}},
            {"cLabel": {"value": "No Site Co"}, "tick": {"value": "NSC"}},
        ]
    }
}


def test_constituents_come_from_the_first_table_only():
    rows = survey.parse_constituents(WIKITEXT)
    assert [(c.ticker, c.name, c.sector) for c in rows] == [
        ("MMM", "3M", "Industrials"),
        ("AAPL", "Apple Inc.", "Information Technology"),
        ("BRK.B", "Berkshire Hathaway", "Financials"),
        ("HWM", "Howmet", "Industrials"),  # a piped link shows its label
        ("RMD", "ResMed", "Health Care"),  # a stray "|" after the link is not part of the name
    ]
    assert [c.article for c in rows] == ["3M", "Apple Inc.", "Berkshire Hathaway", "Howmet Aerospace", "ResMed"]


def test_sites_are_keyed_by_ticker():
    sites = survey.parse_sites(SPARQL)
    assert sites == {"MMM": "https://www.3m.com/", "AAPL": "https://www.apple.com", "HWM": "http://howmet.com"}


@pytest.mark.parametrize(
    ("site", "expected"),
    [
        ("https://www.3m.com/", ["https://www.3m.com/careers", "https://3m.com/careers",
                                 "https://careers.3m.com/", "https://jobs.3m.com/"]),
        ("http://howmet.com", ["https://www.howmet.com/careers", "https://howmet.com/careers",
                               "https://careers.howmet.com/", "https://jobs.howmet.com/"]),
        ("https://corporate.exxonmobil.com/en", ["https://www.corporate.exxonmobil.com/careers",
                                                 "https://corporate.exxonmobil.com/careers",
                                                 "https://careers.corporate.exxonmobil.com/",
                                                 "https://jobs.corporate.exxonmobil.com/"]),
    ],
)
def test_candidate_urls(site, expected):
    assert survey.candidate_urls(site) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('<a href="https://acme.wd5.myworkdayjobs.com/External">Jobs</a>', {"workday"}),
        ("<script src='https://cdn.phenompeople.com/x.js'></script>", {"phenom"}),
        ("var api = '/api/pcsx/search';", {"eightfold"}),
        ("https://fa-x.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", {"oracle"}),
        ("careers-acme.icims.com and jobs.jobvite.com/acme", {"icims", "jobvite"}),
        ("<p>We are hiring!</p>", set()),
    ],
)
def test_platforms(text, expected):
    assert survey.platforms(text) == expected


def test_career_links_on_a_homepage():
    page = """<a href="/en/careers">Careers</a> <a href="https://jobs.acme.com/">Jobs</a>
              <a href="/about">About</a> <a href="mailto:careers@acme.com">Mail</a>
              <a href="/en/careers">Careers again</a> <a href="/work-with-us/careers/students">Students</a>"""
    assert survey.career_links(page, "https://www.acme.com/") == [
        "https://www.acme.com/en/careers",
        "https://jobs.acme.com/",
    ]


ACME = "https://www.acme.com"


def _client():
    return httpx.Client(follow_redirects=True)


@respx.mock
def test_survey_finds_boards_and_platforms_and_names_them_after_the_company():
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(
        return_value=httpx.Response(
            200,
            text='<a href="https://acme.wd5.myworkdayjobs.com/en-US/External/job/1">Apply</a>'
            '<a href="https://boards.greenhouse.io/acmelabs">Labs</a>',
        )
    )
    company = survey.Constituent("ACM", "Acme Corp", "Industrials")
    with _client() as client:
        result = survey.survey_company(company, ACME + "/", client, delay=0)
    assert result.platforms == ["greenhouse", "workday"]
    assert [(b.ats, b.slug, b.name) for b in result.boards] == [
        ("workday", "acme/External", "Acme Corp"),
        ("greenhouse", "acmelabs", "Acme Corp"),
    ]
    assert result.pages == [ACME + "/careers"]  # the first candidate with a platform ends the search


@respx.mock
def test_survey_respects_robots_txt():
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /careers\n"))
    careers = respx.get(ACME + "/careers").mock(return_value=httpx.Response(200, text="workday"))
    for host in ("https://acme.com", "https://careers.acme.com", "https://jobs.acme.com"):
        respx.get(host + "/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(url__startswith=host + "/").mock(side_effect=httpx.ConnectError("no such host"))
    respx.get(ACME + "/").mock(return_value=httpx.Response(200, text="<html>home</html>"))
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert careers.call_count == 0
    assert ACME + "/careers" in result.skipped_by_robots
    assert result.platforms == [] and result.boards == []


@respx.mock
def test_survey_follows_a_homepage_careers_link_when_the_guesses_find_nothing():
    for host in ("https://www.acme.com", "https://acme.com", "https://careers.acme.com", "https://jobs.acme.com"):
        respx.get(host + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(return_value=httpx.Response(404))
    respx.get("https://acme.com/careers").mock(return_value=httpx.Response(404))
    respx.get("https://careers.acme.com/").mock(side_effect=httpx.ConnectError("no such host"))
    respx.get("https://jobs.acme.com/").mock(side_effect=httpx.ConnectError("no such host"))
    respx.get(ACME + "/").mock(return_value=httpx.Response(200, text='<a href="/who-we-are/join-us/careers">Careers</a>'))
    respx.get(ACME + "/who-we-are/join-us/careers").mock(
        return_value=httpx.Response(200, text='see https://acme.eightfold.ai/careers?domain=acme.com')
    )
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert result.platforms == ["eightfold"]
    assert [(b.ats, b.slug) for b in result.boards] == [("eightfold", "acme.eightfold.ai")]
    assert result.pages == [ACME + "/who-we-are/join-us/careers"]


@respx.mock
def test_survey_waits_between_requests(monkeypatch):
    slept = []
    monkeypatch.setattr(survey.time, "sleep", slept.append)
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(
        return_value=httpx.Response(200, text="https://acme.wd5.myworkdayjobs.com/External")
    )
    with _client() as client:
        survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=1.5)
    assert slept and all(s == 1.5 for s in slept)


def test_report_counts_platforms_and_flags_known_boards():
    known = [Company(name="Acme", ats="workday", slug="acme/External", datacenter="wd5")]
    results = [
        survey.Result("ACM", "Acme Corp", "Industrials", "https://acme.com", ["https://acme.com/careers"], ["workday"],
                      [Company(name="Acme Corp", ats="workday", slug="acme/External", datacenter="wd5")], []),
        survey.Result("BET", "Beta Inc", "Financials", "https://beta.com", ["https://beta.com/careers"], ["icims"], [], []),
        survey.Result("NOS", "No Site", "Energy", None, [], [], [], []),
    ]
    text = survey.report(results, known)
    assert "| workday | 1 |" in text and "| icims | 1 |" in text
    assert "| ACM | Acme Corp | workday | workday acme/External (in config) |" in text
    assert "| BET | Beta Inc | icims |  |" in text
    assert "| NOS | No Site | no website found |  |" in text


def test_new_boards_leave_out_known_ones_and_repeats():
    known = [Company(name="Acme", ats="workday", slug="acme/External", datacenter="wd5")]
    boards = [
        Company(name="Acme Corp", ats="workday", slug="acme/External", datacenter="wd5"),
        Company(name="Beta Inc", ats="greenhouse", slug="beta"),
        Company(name="Beta Inc", ats="greenhouse", slug="Beta"),
    ]
    results = [survey.Result("X", "X", "X", None, [], [], boards, [])]
    assert [(b.name, b.slug) for b in survey.new_boards(results, known)] == [("Beta Inc", "beta")]


@respx.mock
def test_main_surveys_writes_outputs_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(survey, "fetch_constituents", lambda client: survey.parse_constituents(WIKITEXT))
    monkeypatch.setattr(survey, "fetch_sites", lambda client: survey.parse_sites(SPARQL))
    monkeypatch.setattr(survey, "fetch_title_sites", lambda client, titles: {})
    monkeypatch.setattr(survey.time, "sleep", lambda s: None)
    for host in ("www.3m.com", "3m.com", "careers.3m.com", "jobs.3m.com", "www.apple.com", "apple.com",
                 "careers.apple.com", "jobs.apple.com", "www.howmet.com", "howmet.com", "careers.howmet.com",
                 "jobs.howmet.com"):
        respx.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://www.3m.com/careers").mock(
        return_value=httpx.Response(200, text='<a href="https://3m.wd1.myworkdayjobs.com/Search">x</a>')
    )
    respx.get(url__regex=r"https://(www\.)?apple\.com/.*").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r"https://(careers|jobs)\.apple\.com/.*").mock(return_value=httpx.Response(404))
    respx.get("https://www.howmet.com/careers").mock(
        return_value=httpx.Response(200, text="https://fa-x.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1")
    )
    known = tmp_path / "companies.yaml"
    known.write_text("companies:\n  - name: 3M\n    ats: workday\n    slug: 3m/Search\n    datacenter: wd1\n")
    out = tmp_path / "sp500"
    args = [str(out), "--companies", str(known), "--delay", "0", "--limit", "3"]
    assert survey.main(args) == 0
    done = json.loads((out / "results.json").read_text())
    assert [r["ticker"] for r in done] == ["MMM", "AAPL", "BRK.B"]
    report = (out / "survey.md").read_text()
    assert "| BRK.B | Berkshire Hathaway | no website found |  |" in report
    assert "3m/Search (in config)" in report
    generated = yaml.safe_load("companies:\n" + (out / "companies.generated.yaml").read_text())["companies"]
    assert generated is None  # 3M's only board is already known

    calls = len(respx.calls)
    assert survey.main([str(out), "--companies", str(known), "--delay", "0"]) == 0  # resumes: only HWM is new
    assert all("3m.com" not in str(c.request.url) for c in respx.calls[calls:])
    generated = yaml.safe_load("companies:\n" + (out / "companies.generated.yaml").read_text())["companies"]
    assert [(g["name"], g["ats"], g["slug"]) for g in generated] == [
        ("Howmet", "oracle", "fa-x.fa.ocs.oraclecloud.com/CX_1")
    ]


# ------------------------------------------------------------------ websites by Wikipedia title

# What en.wikipedia's query API answers for titles=Airbnb|Palantir Technologies|No Site Co|Missing Page
# (formatversion=2): a redirect, and a page with no Wikidata item.
PAGES = {
    "batchcomplete": True,
    "query": {
        "normalized": [{"fromencoded": False, "from": "palantir_Technologies", "to": "Palantir Technologies"}],
        "redirects": [{"from": "Palantir Technologies", "to": "Palantir"}],
        "pages": [
            {"ns": 0, "title": "Missing Page", "missing": True},
            {"pageid": 1, "ns": 0, "title": "Airbnb", "pageprops": {"wikibase_item": "Q63327"}},
            {"pageid": 2, "ns": 0, "title": "Palantir", "pageprops": {"wikibase_item": "Q2047336"}},
            {"pageid": 3, "ns": 0, "title": "No Site Co", "pageprops": {"wikibase_item": "Q1"}},
            {"pageid": 4, "ns": 0, "title": "No Item"},
        ],
    },
}

ENTITIES = {
    "entities": {
        "Q63327": {
            "id": "Q63327",
            "claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.airbnb.com/"}}},
                                {"mainsnak": {"datavalue": {"value": "https://www.airbnb.fr/"}}}]},
        },
        "Q2047336": {"id": "Q2047336",
                     "claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.palantir.com/"}}}]}},
        "Q1": {"id": "Q1", "claims": {"P856": [{"mainsnak": {"snaktype": "novalue"}}]}},
    }
}


def test_item_ids_follow_normalized_titles_and_redirects():
    titles = ["Airbnb", "palantir_Technologies", "Missing Page", "No Item"]
    assert survey.parse_item_ids(PAGES, titles) == {"Airbnb": "Q63327", "palantir_Technologies": "Q2047336"}


def test_item_sites_take_the_first_official_website():
    assert survey.parse_item_sites(ENTITIES) == {"Q63327": "https://www.airbnb.com/",
                                                 "Q2047336": "https://www.palantir.com/"}


@respx.mock
def test_title_sites_are_fetched_fifty_titles_a_call_with_mediawiki_titles():
    pages = respx.get("https://en.wikipedia.org/w/api.php").mock(return_value=httpx.Response(200, json=PAGES))
    items = respx.get("https://www.wikidata.org/w/api.php").mock(return_value=httpx.Response(200, json=ENTITIES))
    titles = ["airbnb", "Airbnb", "Palantir_Technologies", *(f"Co {i}" for i in range(60))]
    with httpx.Client() as client:
        sites = survey.fetch_title_sites(client, titles)
    # Keyed by the title asked for, even when Wikipedia redirects it.
    assert sites == {"Airbnb": "https://www.airbnb.com/", "Palantir Technologies": "https://www.palantir.com/"}
    assert pages.call_count == 2
    first = pages.calls[0].request.url.params
    assert (first["action"], first["redirects"], first["prop"], first["ppprop"], first["formatversion"]) == (
        "query", "1", "pageprops", "wikibase_item", "2")
    asked = [t for call in pages.calls for t in call.request.url.params["titles"].split("|")]
    assert asked[:2] == ["Airbnb", "Palantir Technologies"] and len(asked) == 62
    assert len(first["titles"].split("|")) == 50
    ids = [i for call in items.calls for i in call.request.url.params["ids"].split("|")]
    assert sorted(ids) == ["Q2047336", "Q63327"]
    assert items.calls[0].request.url.params["action"] == "wbgetentities"


@respx.mock
def test_item_websites_are_read_fifty_ids_a_call():
    many = {"query": {"pages": [{"title": f"Co {i}", "pageprops": {"wikibase_item": f"Q{i}"}} for i in range(60)]}}
    respx.get("https://en.wikipedia.org/w/api.php").mock(return_value=httpx.Response(200, json=many))
    items = respx.get("https://www.wikidata.org/w/api.php").mock(return_value=httpx.Response(200, json={"entities": {}}))
    with httpx.Client() as client:
        survey.fetch_title_sites(client, [f"Co {i}" for i in range(60)])
    assert [len(c.request.url.params["ids"].split("|")) for c in items.calls] == [50, 10]


@respx.mock
def test_main_finds_a_website_by_title_when_the_ticker_lookup_has_none(tmp_path, monkeypatch):
    abnb = survey.Constituent("ABNB", "Airbnb", "Consumer Discretionary", "Airbnb")
    nosite = survey.Constituent("NSC", "No Site Co", "Energy", "No Site Co")
    monkeypatch.setattr(survey, "fetch_constituents", lambda client: [abnb, nosite])
    monkeypatch.setattr(survey, "fetch_sites", lambda client: {})  # the SPARQL map misses both
    monkeypatch.setattr(survey.time, "sleep", lambda s: None)
    respx.get("https://en.wikipedia.org/w/api.php").mock(return_value=httpx.Response(200, json=PAGES))
    respx.get("https://www.wikidata.org/w/api.php").mock(return_value=httpx.Response(200, json=ENTITIES))
    respx.get("https://www.airbnb.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://www.airbnb.com/careers").mock(
        return_value=httpx.Response(200, text="https://jobs.lever.co/airbnb")
    )
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    out = tmp_path / "sp500"
    assert survey.main([str(out), "--companies", str(known), "--delay", "0"]) == 0
    report = (out / "survey.md").read_text()
    assert "| ABNB | Airbnb | lever | lever airbnb |" in report
    assert "| NSC | No Site Co | no website found |  |" in report


# ------------------------------------------------------------------ robots.txt

UA = "jobhunt/0.1 (+personal job search tool)"
WORKDAY = "https://acme.wd5.myworkdayjobs.com/External"


def _no_other_hosts():
    for host in ("https://acme.com", "https://careers.acme.com", "https://jobs.acme.com"):
        respx.get(host + "/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(url__startswith=host + "/").mock(return_value=httpx.Response(404))


@respx.mock
@pytest.mark.parametrize("status", [500, 503])
def test_robots_txt_server_error_disallows_everything(status):
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(status))
    page = respx.get(ACME + "/careers").mock(return_value=httpx.Response(200, text=WORKDAY))
    home = respx.get(ACME + "/").mock(return_value=httpx.Response(200, text="home"))
    _no_other_hosts()
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert page.call_count == 0 and home.call_count == 0
    assert ACME + "/careers" in result.skipped_by_robots


@respx.mock
@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
def test_unreachable_robots_txt_disallows_everything(error):
    respx.get(ACME + "/robots.txt").mock(side_effect=error)
    page = respx.get(ACME + "/careers").mock(return_value=httpx.Response(200, text=WORKDAY))
    _no_other_hosts()
    respx.get(ACME + "/").mock(return_value=httpx.Response(200, text="home"))
    with _client() as client:
        survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert page.call_count == 0


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 404])
def test_robots_txt_client_error_allows_everything(status):
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(status))
    respx.get(ACME + "/careers").mock(return_value=httpx.Response(200, text=WORKDAY))
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert result.platforms == ["workday"]


@respx.mock
def test_a_robots_txt_that_allows_the_page():
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    respx.get(ACME + "/careers").mock(return_value=httpx.Response(200, text=WORKDAY))
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert result.platforms == ["workday"] and result.skipped_by_robots == []


@respx.mock
def test_a_robots_txt_group_for_jobhunt_is_honoured():
    rules = "User-agent: jobhunt\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(200, text=rules))
    page = respx.get(ACME + "/careers").mock(return_value=httpx.Response(200, text=WORKDAY))
    _no_other_hosts()
    respx.get(ACME + "/").mock(return_value=httpx.Response(200, text="home"))
    with httpx.Client(headers={"User-Agent": UA}) as client:
        survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert page.call_count == 0


@respx.mock
def test_a_disallowed_homepage_is_not_fetched():
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    home = respx.get(ACME + "/").mock(return_value=httpx.Response(200, text='<a href="/careers">Careers</a>'))
    _no_other_hosts()
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert home.call_count == 0
    assert ACME + "/" in result.skipped_by_robots


@respx.mock
def test_a_redirect_to_a_disallowed_host_stops_there():
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(return_value=httpx.Response(301, headers={"Location": "https://careers.partner.com/acme"}))
    respx.get("https://careers.partner.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    target = respx.get("https://careers.partner.com/acme").mock(return_value=httpx.Response(200, text=WORKDAY))
    _no_other_hosts()
    respx.get(ACME + "/").mock(return_value=httpx.Response(200, text="home"))
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert target.call_count == 0
    assert "https://careers.partner.com/acme" in result.skipped_by_robots


@respx.mock
def test_an_allowed_redirect_is_followed_with_a_delay_per_hop(monkeypatch):
    slept = []
    monkeypatch.setattr(survey.time, "sleep", slept.append)
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(return_value=httpx.Response(302, headers={"Location": "/en/careers"}))
    respx.get(ACME + "/en/careers").mock(return_value=httpx.Response(200, text=WORKDAY))
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=1)
    assert result.pages == [ACME + "/en/careers"]
    assert len(slept) == 3  # robots.txt, /careers, /en/careers


@respx.mock
def test_redirects_stop_after_five_hops():
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    for i in range(10):
        respx.get(f"{ACME}/r{i}").mock(return_value=httpx.Response(302, headers={"Location": f"/r{i + 1}"}))
    loop = respx.get(ACME + "/careers").mock(return_value=httpx.Response(302, headers={"Location": "/r0"}))
    _no_other_hosts()
    respx.get(ACME + "/").mock(return_value=httpx.Response(200, text="home"))
    with _client() as client:
        survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert loop.call_count == 1
    assert respx.get(f"{ACME}/r4").call_count == 1 and respx.get(f"{ACME}/r5").call_count == 0


# ------------------------------------------------------------------ excluded, unreachable, --only


@respx.mock
@pytest.mark.parametrize("ticker", ["META", "GOOGL", "GOOG"])
def test_excluded_companies_make_no_requests(ticker):
    with _client() as client:
        result = survey.survey_company(survey.Constituent(ticker, "Off Limits", "X"), "https://www.meta.com/", client)
    assert len(respx.calls) == 0
    assert result.status.startswith("excluded: ")
    assert f"| {ticker} | Off Limits | {result.status} |  |" in survey.report([result], [])


@respx.mock
def test_network_outage_is_unreachable_and_a_rerun_retries_it(tmp_path, monkeypatch):
    acme = survey.Constituent("ACM", "Acme", "X", "Acme")
    monkeypatch.setattr(survey, "fetch_constituents", lambda client: [acme])
    monkeypatch.setattr(survey, "fetch_sites", lambda client: {"ACM": ACME})
    monkeypatch.setattr(survey, "fetch_title_sites", lambda client, titles: {})
    monkeypatch.setattr(survey.time, "sleep", lambda s: None)
    route = respx.route().mock(side_effect=httpx.ConnectError("network is down"))
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    out = tmp_path / "sp500"
    assert survey.main([str(out), "--companies", str(known), "--delay", "0"]) == 0
    assert "| ACM | Acme | unreachable |  |" in (out / "survey.md").read_text()
    saved = json.loads((out / "results.json").read_text())
    assert saved[0]["errors"] and all("ConnectError" in e for e in saved[0]["errors"])

    route.mock(return_value=httpx.Response(200, text=WORKDAY))
    assert survey.main([str(out), "--companies", str(known), "--delay", "0"]) == 0
    saved = json.loads((out / "results.json").read_text())
    assert [(r["ticker"], r["platforms"]) for r in saved] == [("ACM", ["workday"])]


@respx.mock
def test_only_resurveys_done_tickers_in_place(tmp_path, monkeypatch):
    rows = [survey.Constituent(t, t.title(), "X", t) for t in ("AAA", "BBB", "CCC")]
    monkeypatch.setattr(survey, "fetch_constituents", lambda client: rows)
    monkeypatch.setattr(survey, "fetch_sites", lambda client: {})
    monkeypatch.setattr(survey, "fetch_title_sites", lambda client, titles: {"BBB": "https://www.bbb.com"})
    monkeypatch.setattr(survey.time, "sleep", lambda s: None)
    respx.get("https://www.bbb.com/robots.txt").mock(return_value=httpx.Response(404))
    careers = respx.get("https://www.bbb.com/careers").mock(return_value=httpx.Response(200, text="none here"))
    respx.route().mock(return_value=httpx.Response(404))
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    out = tmp_path / "sp500"
    assert survey.main([str(out), "--companies", str(known), "--delay", "0"]) == 0
    assert "| BBB | Bbb | none found |  |" in (out / "survey.md").read_text()

    careers.mock(return_value=httpx.Response(200, text=WORKDAY))
    assert survey.main([str(out), "--companies", str(known), "--delay", "0", "--only", "BBB"]) == 0
    saved = json.loads((out / "results.json").read_text())
    assert [(r["ticker"], r["platforms"]) for r in saved] == [("AAA", []), ("BBB", ["workday"]), ("CCC", [])]


def test_report_says_none_found_when_a_site_has_no_platform():
    result = survey.Result("BET", "Beta Inc", "Financials", "https://beta.com", [], [], [], [])
    assert "| BET | Beta Inc | none found |  |" in survey.report([result], [])


# ------------------------------------------------------------------ homepage links and page URLs


@respx.mock
def test_survey_stops_at_the_first_homepage_link_that_finds_something():
    for host in ("https://www.acme.com", "https://acme.com", "https://careers.acme.com", "https://jobs.acme.com"):
        respx.get(host + "/robots.txt").mock(return_value=httpx.Response(404))
    for url in (ACME + "/careers", "https://acme.com/careers", "https://careers.acme.com/", "https://jobs.acme.com/"):
        respx.get(url).mock(return_value=httpx.Response(404))
    respx.get(ACME + "/").mock(
        return_value=httpx.Response(200, text='<a href="/en/careers">Careers</a> <a href="/en/jobs">Jobs</a>')
    )
    respx.get(ACME + "/en/careers").mock(return_value=httpx.Response(200, text=WORKDAY))
    second = respx.get(ACME + "/en/jobs").mock(return_value=httpx.Response(200, text="https://boards.greenhouse.io/acme"))
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert second.call_count == 0
    assert result.pages == [ACME + "/en/careers"]


@respx.mock
def test_urls_in_page_text_lose_trailing_punctuation():
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(return_value=httpx.Response(200, text="Apply at https://jobs.lever.co/acme."))
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert [(b.ats, b.slug) for b in result.boards] == [("lever", "acme")]


@pytest.mark.parametrize("href", ["https://[object Object]/careers", "http://[::1/careers", "https://acme.com:abc/careers"])
def test_career_links_skip_hrefs_that_do_not_parse(href):
    page = f'<a href="{href}">Careers</a> <a href="/careers">Careers</a>'
    assert survey.career_links(page, "https://www.acme.com/") == ["https://www.acme.com/careers"]


def test_career_links_skip_job_sites_and_social_networks():
    page = """<a href="https://www.linkedin.com/company/acme/jobs">LinkedIn</a>
              <a href="https://www.glassdoor.com/Jobs/acme-jobs">Glassdoor</a>
              <a href="https://x.com/acmecareers">X</a> <a href="https://careers.facebook.com/acme">FB</a>
              <a href="https://jobs.acme.com/">Jobs</a>"""
    assert survey.career_links(page, "https://www.acme.com/") == ["https://jobs.acme.com/"]


@respx.mock
@pytest.mark.parametrize("href", ["https://acme.com:abc/careers", "https://xn--zz.com/careers"])
def test_a_homepage_link_httpx_cannot_request_does_not_stop_the_survey(href):
    for host in ("https://www.acme.com", "https://acme.com", "https://careers.acme.com", "https://jobs.acme.com"):
        respx.get(host + "/robots.txt").mock(return_value=httpx.Response(404))
    for url in (ACME + "/careers", "https://acme.com/careers", "https://careers.acme.com/", "https://jobs.acme.com/"):
        respx.get(url).mock(return_value=httpx.Response(404))
    respx.get(ACME + "/").mock(
        return_value=httpx.Response(200, text=f'<a href="{href}">Careers</a> <a href="/en/careers">Careers</a>')
    )
    respx.get(ACME + "/en/careers").mock(return_value=httpx.Response(200, text=WORKDAY))
    with _client() as client:
        result = survey.survey_company(survey.Constituent("ACM", "Acme", "X"), ACME + "/", client, delay=0)
    assert result.platforms == ["workday"] and result.status == ""


def test_a_url_httpx_cannot_request_is_an_error_not_a_crash():
    with _client() as client:
        polite = survey._Polite(client, delay=0)
        assert polite._fetch("https://xn--zz.com/careers", follow_redirects=False) is None
        assert polite._fetch("https://acme.com:abc/careers", follow_redirects=False) is None
    assert [e.split(": ")[0] for e in polite.errors] == ["https://xn--zz.com/careers", "https://acme.com:abc/careers"]
    assert polite.responses == 0


def _main_setup(monkeypatch, rows, sites):
    monkeypatch.setattr(survey, "fetch_constituents", lambda client: rows)
    monkeypatch.setattr(survey, "fetch_sites", lambda client: sites)
    monkeypatch.setattr(survey, "fetch_title_sites", lambda client, titles: {})
    monkeypatch.setattr(survey.time, "sleep", lambda s: None)


@respx.mock
def test_limit_surveys_new_companies_before_retrying_unreachable_ones(tmp_path, monkeypatch):
    rows = [survey.Constituent(t, t, "X", t) for t in ("DED", "OK1", "OK2")]
    _main_setup(monkeypatch, rows, {"DED": "https://dead.example", "OK1": "https://ok1.example",
                                    "OK2": "https://ok2.example"})
    respx.route(host__regex=r".*dead\.example").mock(side_effect=httpx.ConnectError("NXDOMAIN"))
    respx.route().mock(return_value=httpx.Response(404))
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    out = tmp_path / "sp500"
    for _ in range(3):
        assert survey.main([str(out), "--companies", str(known), "--delay", "0", "--limit", "1"]) == 0
    saved = json.loads((out / "results.json").read_text())
    assert [(r["ticker"], r["status"]) for r in saved] == [("DED", "unreachable"), ("OK1", ""), ("OK2", "")]


@respx.mock
def test_an_unreachable_company_is_tried_three_times_in_all(tmp_path, monkeypatch):
    _main_setup(monkeypatch, [survey.Constituent("DED", "Dead", "X", "Dead")], {"DED": "https://dead.example"})
    respx.route().mock(side_effect=httpx.ConnectError("NXDOMAIN"))
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    out = tmp_path / "sp500"
    attempts = []
    for _ in range(4):
        calls = len(respx.calls)
        assert survey.main([str(out), "--companies", str(known), "--delay", "0"]) == 0
        attempts.append(len(respx.calls) > calls)
    assert attempts == [True, True, True, False]
    saved = json.loads((out / "results.json").read_text())
    assert [(r["ticker"], r["status"], r["attempts"]) for r in saved] == [("DED", "unreachable", 3)]
    assert "| DED | Dead | unreachable |  |" in (out / "survey.md").read_text()

    calls = len(respx.calls)
    assert survey.main([str(out), "--companies", str(known), "--delay", "0", "--only", "DED"]) == 0
    assert len(respx.calls) > calls  # --only still re-surveys it


@respx.mock
def test_a_company_with_any_response_is_not_unreachable_and_is_not_retried(tmp_path, monkeypatch):
    _main_setup(monkeypatch, [survey.Constituent("ACM", "Acme", "X", "Acme")], {"ACM": ACME})
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/").mock(return_value=httpx.Response(200, text="home"))
    for host in ("https://acme.com", "https://careers.acme.com", "https://jobs.acme.com"):
        respx.get(url__startswith=host + "/").mock(side_effect=httpx.ConnectError("no such host"))
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    out = tmp_path / "sp500"
    assert survey.main([str(out), "--companies", str(known), "--delay", "0"]) == 0
    saved = json.loads((out / "results.json").read_text())
    assert saved[0]["errors"] and saved[0]["status"] != "unreachable"
    assert "| ACM | Acme | none found |  |" in (out / "survey.md").read_text()

    calls = len(respx.calls)
    assert survey.main([str(out), "--companies", str(known), "--delay", "0"]) == 0
    assert len(respx.calls) == calls


def test_main_sends_everything_through_the_throttled_transport(tmp_path, monkeypatch):
    # Wikidata answered a burst of back-to-back batches with 429; the throttle retries it
    seen = {}

    def constituents(client):
        seen["transport"] = client._transport
        return []

    monkeypatch.setattr(survey, "fetch_constituents", constituents)
    monkeypatch.setattr(survey, "fetch_sites", lambda client: {})
    monkeypatch.setattr(survey, "fetch_title_sites", lambda client, titles: {})
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    assert survey.main([str(tmp_path / "out"), "--companies", str(known)]) == 0
    transport = seen["transport"]
    assert isinstance(transport, survey.throttle.ThrottledTransport)
    assert transport.limiter("www.wikidata.org").ceiling == 1  # one request at a time per host
    assert transport._transient_retries == 0  # a host that doesn't resolve isn't retried


@respx.mock
def test_a_redirect_without_a_location_is_the_final_answer():
    # a live site answered 302 with no Location header; looking it up crashed the whole run
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(return_value=httpx.Response(302))
    with _client() as client:
        polite = survey._Polite(client, delay=0)
        resp = polite.get(ACME + "/careers")
    assert resp is not None and resp.status_code == 302


@respx.mock
def test_main_retries_a_wikidata_429(tmp_path, monkeypatch):
    # Wikidata answered a burst of batches with 429; that ended the run before the throttle
    monkeypatch.setenv("JOBHUNT_FETCH_MAX_RETRIES", "2")
    monkeypatch.setenv("JOBHUNT_FETCH_MAX_RETRY_AFTER", "30")
    built = []
    real = survey.throttle.ThrottledTransport

    def transport(**kwargs):
        # a fake clock, so Retry-After is waited out at once
        clock = [1000.0]
        built.append(
            real(**kwargs, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        )
        return built[-1]

    monkeypatch.setattr(survey.throttle, "ThrottledTransport", transport)
    monkeypatch.setattr(
        survey, "fetch_constituents", lambda client: [survey.Constituent("ACME", "Acme", "X", "Acme")]
    )
    cookies_sent = []

    def fetch_sites(client):  # two requests to one host; the first answer sets a cookie
        url = "https://query.wikidata.org/sparql"
        respx.get(url).mock(return_value=httpx.Response(200, headers={"Set-Cookie": "WMF=x; Path=/"}))
        for _ in range(2):
            cookies_sent.append(client.get(url).request.headers.get("cookie"))
        return {}

    monkeypatch.setattr(survey, "fetch_sites", fetch_sites)
    monkeypatch.setattr(
        survey, "survey_company", lambda *a, **k: survey.Result("ACME", "Acme", "X", None, [], [], [])
    )
    respx.get(survey.WIKI_API).mock(
        return_value=httpx.Response(
            200,
            json={"query": {"pages": [{"title": "Acme", "pageprops": {"wikibase_item": "Q1"}}]}},
        )
    )
    wikidata = respx.get(survey.WIKIDATA_API).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"entities": {}})]
    )
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    assert survey.main([str(tmp_path / "out"), "--companies", str(known), "--delay", "0"]) == 0
    assert wikidata.call_count == 2
    assert cookies_sent == [None, None]  # the first answer's cookie wasn't kept
    (built,) = built
    assert built._max_retries == 2 and built._max_retry_after == 30  # from the fetch settings
    assert built._slots is not None and built._slots._initial_value == 1  # one request at a time


def test_main_tries_a_host_that_does_not_connect_once(tmp_path, monkeypatch):
    # careers.<host> guesses often don't resolve; each connect attempt can cost fetch.timeout
    import httpcore

    attempts = []

    class Backend(httpcore.NetworkBackend):
        def connect_tcp(self, *args, **kwargs):
            attempts.append(1)
            raise httpcore.ConnectError("simulated")

    def constituents(client):
        client._transport._inner._pool._network_backend = Backend()
        with pytest.raises(httpx.ConnectError):
            client.get("https://careers.example.invalid/")
        return []

    monkeypatch.setattr(survey, "fetch_constituents", constituents)
    monkeypatch.setattr(survey, "fetch_sites", lambda client: {})
    monkeypatch.setattr(survey, "fetch_title_sites", lambda client, titles: {})
    known = tmp_path / "companies.yaml"
    known.write_text("companies: []\n")
    assert survey.main([str(tmp_path / "out"), "--companies", str(known)]) == 0
    assert len(attempts) == 1


@respx.mock
def test_a_malformed_redirect_is_an_error_not_a_crash():
    # urljoin raises ValueError on a Location like http://[oops; that crashed the whole run
    respx.get(ACME + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/careers").mock(
        return_value=httpx.Response(302, headers={"Location": "http://[oops/careers"})
    )
    with _client() as client:
        polite = survey._Polite(client, delay=0)
        resp = polite.get(ACME + "/careers")
    assert resp is not None and resp.status_code == 302
    assert polite.errors == [f"{ACME}/careers: bad redirect"]


# ------------------------------------------------------------------ Phenom sites

PHENOM_PAGE = '<html><script src="https://cdn.phenompeople.com/CareerConnectResources/x.js"></script></html>'


def _phenom_site(apply_urls, widgets=None, robots="User-agent: *\nDisallow: */jobcart\n"):
    """careers.acme.com: a Phenom site at /us/en; the www and bare-host guesses find nothing."""
    for host in (ACME, "https://acme.com"):
        respx.get(host + "/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(host + "/careers").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/").mock(return_value=httpx.Response(404))
    site = "https://careers.acme.com"
    respx.get(site + "/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    respx.get(site + "/").mock(return_value=httpx.Response(302, headers={"location": "/us/en"}))
    respx.get(site + "/us/en").mock(return_value=httpx.Response(200, text=PHENOM_PAGE))
    jobs = [{"jobId": f"R{i}", "title": "Director", "applyUrl": u} for i, u in enumerate(apply_urls)]
    answer = widgets or httpx.Response(200, json={"refineSearch": {"totalHits": 40, "data": {"jobs": jobs}}})
    return respx.post(site + "/widgets").mock(return_value=answer)


def _survey_acme():
    with _client() as client:
        return survey.survey_company(survey.Constituent("ACM", "Acme Corp", "Industrials"), ACME + "/", client, delay=0)


@respx.mock
def test_a_phenom_site_in_front_of_workday_gives_the_workday_board():
    widgets = _phenom_site(["https://acme.wd5.myworkdayjobs.com/External/job/Seattle/Director_R1/apply",
                            "https://acme.wd5.myworkdayjobs.com/External/job/Austin/Director_R2/apply"])
    result = _survey_acme()
    assert result.platforms == ["phenom"]
    assert [(b.ats, b.slug, b.datacenter, b.name) for b in result.boards] == [("workday", "acme/External", "wd5", "Acme Corp")]
    sent = json.loads(widgets.calls.last.request.content)
    assert (sent["ddoKey"], sent["lang"], sent["country"]) == ("refineSearch", "en_us", "us")


@respx.mock
def test_a_phenom_site_in_front_of_an_unsupported_ats_is_a_phenom_board():
    _phenom_site(["https://acme.taleo.net/careersection/apply?job=1"])
    result = _survey_acme()
    assert [(b.ats, b.slug, b.name) for b in result.boards] == [("phenom", "careers.acme.com/us/en", "Acme Corp")]


@pytest.mark.parametrize("answer", [httpx.Response(400), httpx.Response(200, json={"status": "error"}),
                                    httpx.Response(200, json={"refineSearch": {"totalHits": 0, "data": {"jobs": []}}}),
                                    httpx.Response(200, json={"refineSearch": {"data": [1]}})])
@respx.mock
def test_a_phenom_site_whose_search_finds_nothing_gives_no_board(answer):
    _phenom_site([], widgets=answer)
    result = _survey_acme()
    assert result.platforms == ["phenom"] and result.boards == []


@respx.mock
def test_a_phenom_search_robots_txt_disallows_is_not_sent():
    widgets = _phenom_site(["https://acme.wd5.myworkdayjobs.com/External/job/1"], robots="User-agent: *\nDisallow: /widgets\n")
    result = _survey_acme()
    assert widgets.call_count == 0
    assert "https://careers.acme.com/widgets" in result.skipped_by_robots
    assert result.platforms == ["phenom"] and result.boards == []


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://careers.adobe.com/us/en", "careers.adobe.com/us/en"),
        ("https://careers.cisco.com/global/en/home", "careers.cisco.com/global/en"),
        ("https://jobs.cvshealth.com/us/en/home", "jobs.cvshealth.com/us/en"),
        ("https://careers.davita.com/", "careers.davita.com"),
        ("https://Careers.Example.com/search-results", "careers.example.com"),
    ],
)
def test_a_phenom_board_from_a_page_url(url, slug):
    assert survey.phenom_board(url, "Acme").slug == slug


# ------------------------------------------------------------------ SuccessFactors sites

CSB_PAGE = ('<html><script src="/platform/js/j2w/min/j2w.core.min.js?h=1"></script>'
            '<img src="https://rmkcdn.successfactors.com/x/logo.png"></html>')


def _csb_site(page_text):
    for host in (ACME, "https://acme.com"):
        respx.get(host + "/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(host + "/careers").mock(return_value=httpx.Response(404))
    respx.get(ACME + "/").mock(return_value=httpx.Response(404))
    respx.get("https://careers.acme.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://careers.acme.com/").mock(return_value=httpx.Response(200, text=page_text))


@pytest.mark.parametrize(
    "page",
    [
        CSB_PAGE,
        # a page that loads only a /platform/csb/ asset
        ('<html><link rel="stylesheet" href="/platform/csb/css/customHeader.css?h=1">'
         '<img src="https://rmkcdn.successfactors.com/x/logo.png"></html>'),
    ],
)
@respx.mock
def test_a_career_site_builder_page_is_a_successfactors_board_for_its_host(page):
    _csb_site(page)
    result = _survey_acme()
    assert result.platforms == ["successfactors"]
    assert [(b.ats, b.slug, b.name) for b in result.boards] == [("successfactors", "careers.acme.com", "Acme Corp")]


@respx.mock
def test_a_page_that_only_mentions_successfactors_gives_no_board():
    """A corporate page linking to a SuccessFactors site, or another platform in front of it."""
    _csb_site('<html><a href="https://career4.successfactors.com/career?company=acme">Jobs</a></html>')
    result = _survey_acme()
    assert result.platforms == ["successfactors"] and result.boards == []


# ------------------------------------------------------------------ Radancy and Paradox sites

RADANCY_PAGE = '<html><script src="https://tbcdn.talentbrew.com/company/45831/js/x.js"></script></html>'
JOBS_SITE = "https://careers.acme.com"


def _sitemap_site(page_text, sitemap, job_page=None, robots="User-agent: *\nDisallow: /search-jobs/\n"):
    _csb_site(page_text)  # careers.acme.com/ serves page_text; the other guesses find nothing
    respx.get(JOBS_SITE + "/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    respx.get(JOBS_SITE + "/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap))
    return respx.get(url__startswith=JOBS_SITE + "/job/").mock(
        return_value=httpx.Response(200, text=job_page or "<html>no apply link</html>")
    )


def _urlset(*paths):
    locs = "".join(f"<url><loc>{JOBS_SITE}{p}</loc></url>" for p in paths)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


@respx.mock
def test_a_radancy_site_in_front_of_workday_gives_the_workday_board():
    page = '<a href="https://acme.wd1.myworkdayjobs.com/External/job/Seattle/Director_R1/apply">Apply</a><a href="https://boards.greenhouse.io/other">x</a>'
    job = _sitemap_site(RADANCY_PAGE, _urlset("/", "/job/seattle/director/45831/1001", "/job/austin/manager/45831/1002"), page)
    result = _survey_acme()
    assert result.platforms == ["radancy"]
    assert [(b.ats, b.slug, b.name) for b in result.boards] == [("workday", "acme/External", "Acme Corp")]  # apply links only
    assert job.call_count == 1  # one posting's page is enough


@respx.mock
def test_a_radancy_site_in_front_of_an_unsupported_ats_is_a_radancy_board():
    page = '<a href="https://acme.avature.net/careers/JobApplication?id=1">Apply</a>'
    _sitemap_site(RADANCY_PAGE, _urlset("/job/seattle/director/45831/1001"), page)
    result = _survey_acme()
    assert [(b.ats, b.slug) for b in result.boards] == [("radancy", "careers.acme.com")]


@respx.mock
def test_a_paradox_site_gives_a_paradox_board_from_an_index_sitemap():
    paradox_page = '<html><script src="https://olivia.paradox.ai/widget.js"></script></html>'
    index = (f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>{JOBS_SITE}/en/jobs/sitemap.xml</loc>'
             '</sitemap></sitemapindex>')
    _sitemap_site(paradox_page, index)
    respx.get(JOBS_SITE + "/en/jobs/sitemap.xml").mock(return_value=httpx.Response(200, text=_urlset("/en/jobs/277916/sales-manager/")))
    respx.get(JOBS_SITE + "/en/jobs/277916/sales-manager/").mock(return_value=httpx.Response(200, text="<html></html>"))
    result = _survey_acme()
    assert result.platforms == ["paradox"]
    assert [(b.ats, b.slug) for b in result.boards] == [("paradox", "careers.acme.com")]


@pytest.mark.parametrize(
    ("sitemap", "robots"),
    [
        (_urlset("/", "/about/"), "User-agent: *\nAllow: /\n"),  # no job URLs: just the chat widget, say
        (_urlset("/job/seattle/director/45831/1001"), "User-agent: *\nDisallow: /sitemap.xml\n"),
        ("<html>not xml</html>", "User-agent: *\nAllow: /\n"),
    ],
)
@respx.mock
def test_a_sitemap_site_with_no_job_urls_to_read_gives_no_board(sitemap, robots):
    _sitemap_site(RADANCY_PAGE, sitemap, robots=robots)
    result = _survey_acme()
    assert result.platforms == ["radancy"] and result.boards == []
