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
    ]


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
