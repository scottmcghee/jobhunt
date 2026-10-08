"""Platform fingerprints and board finding on one careers site (the survey's tests cover more)."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobhunt import fingerprint

SITE = "https://careers.acme.com"
WORKDAY_PAGE = '<a href="https://acme.wd5.myworkdayjobs.com/External/job/Seattle/Director_R1">Apply</a>'


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(fingerprint.time, "sleep", lambda s: None)


def _polite():
    return fingerprint.Polite(httpx.Client(headers={"User-Agent": "jobhunt-test"}), delay=0)


def test_platforms_from_text():
    assert fingerprint.platforms("<script src='https://app.jibecdn.com/x.js'>") == {"icims_careers"}
    assert fingerprint.platforms("https://acme.wd5.myworkdayjobs.com/x") == {"workday"}
    assert fingerprint.platforms("nothing here") == set()


@respx.mock
def test_survey_site_reads_a_careers_host_given_directly():
    respx.get(SITE + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, text=WORKDAY_PAGE))
    site = fingerprint.survey_site(_polite(), SITE + "/", "Acme Corp", urls=[SITE + "/"])
    assert site.platforms == ["workday"] and site.pages == [SITE + "/"]
    assert [(b.ats, b.slug, b.datacenter, b.name) for b in site.boards] == [
        ("workday", "acme/External", "wd5", "Acme Corp")
    ]


@respx.mock
def test_survey_site_follows_a_careers_link_when_the_page_names_no_platform():
    respx.get(SITE + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(SITE + "/").mock(return_value=httpx.Response(200, text='<a href="/jobs">Open roles</a>'))
    respx.get(SITE + "/jobs").mock(return_value=httpx.Response(200, text=WORKDAY_PAGE))
    site = fingerprint.survey_site(_polite(), SITE + "/", "Acme Corp", urls=[SITE + "/"])
    assert site.pages == [SITE + "/jobs"] and [b.slug for b in site.boards] == ["acme/External"]


@respx.mock
def test_survey_site_fetches_home_once_when_it_is_among_the_urls():
    respx.get(SITE + "/robots.txt").mock(return_value=httpx.Response(404))
    home = respx.get(SITE + "/").mock(return_value=httpx.Response(200, text='<a href="/jobs">Open roles</a>'))
    respx.get(SITE + "/jobs").mock(return_value=httpx.Response(200, text=WORKDAY_PAGE))
    site = fingerprint.survey_site(_polite(), SITE + "/", "Acme Corp", urls=[SITE + "/"])
    assert home.call_count == 1 and site.pages == [SITE + "/jobs"]


@respx.mock
def test_survey_site_within_robots_txt():
    respx.get(SITE + "/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    page = respx.get(SITE + "/").mock(return_value=httpx.Response(200, text=WORKDAY_PAGE))
    polite = _polite()
    site = fingerprint.survey_site(polite, SITE + "/", "Acme Corp", urls=[SITE + "/"])
    assert page.call_count == 0 and site.boards == [] and SITE + "/" in polite.skipped
