"""ATS adapters: fixture-driven, network mocked with respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobhunt.sources import ashby, fetch_company, greenhouse, lever
from jobhunt.sources._html import to_text


def test_to_text_strips_tags_and_entities():
    # Greenhouse returns entity-escaped HTML; to_text must handle that directly.
    html = "&lt;div&gt;&lt;p&gt;Hello &lt;strong&gt;world&lt;/strong&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;a&lt;/li&gt;&lt;li&gt;b&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;"
    text = to_text(html)
    assert "Hello world" in text
    assert "<" not in text
    assert "a\n\nb" in text  # list items become separate lines


def test_to_text_handles_none_and_empty():
    assert to_text(None) == ""
    assert to_text("") == ""


@respx.mock
def test_greenhouse_fetch_normalizes(gh_company, fixture_json):
    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    with httpx.Client() as client:
        jobs = greenhouse.fetch(gh_company, client)

    assert len(jobs) == 4
    j = jobs[0]
    assert j.source == "greenhouse"
    assert j.company == "ExampleCorp"
    assert j.external_id == "1001"
    assert j.title == "Director of Platform Engineering"
    assert j.remote is True
    assert "Director of Platform Engineering" in j.body
    assert "<" not in j.body  # HTML stripped, including the double-escaped kind
    assert j.key == "greenhouse:examplecorp:1001"


@respx.mock
def test_greenhouse_404_raises(gh_company):
    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(404, json={"status": 404})
    )
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        greenhouse.fetch(gh_company, client)


@respx.mock
def test_lever_fetch_normalizes(lever_company, fixture_json):
    respx.get("https://api.lever.co/v0/postings/examplelever").mock(
        return_value=httpx.Response(200, json=fixture_json("lever_postings.json"))
    )
    with httpx.Client() as client:
        jobs = lever.fetch(lever_company, client)

    assert len(jobs) == 2
    head = jobs[0]
    assert head.title == "Head of Infrastructure"
    assert head.remote is True
    assert head.location == "United States"
    assert "What you'll do" in head.body
    assert "FinOps" in head.body
    assert head.posted_at is not None and head.posted_at.startswith("2025-09")
    assert jobs[1].remote is False  # hybrid


@respx.mock
def test_ashby_fetch_skips_unlisted(ashby_company, fixture_json):
    respx.get("https://api.ashbyhq.com/posting-api/job-board/exampleashby").mock(
        return_value=httpx.Response(200, json=fixture_json("ashby_board.json"))
    )
    with httpx.Client() as client:
        jobs = ashby.fetch(ashby_company, client)

    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "VP of Engineering"
    assert j.remote is True
    assert "New York" in j.location
    assert "30-person" in j.body


@respx.mock
def test_fetch_company_dispatches(gh_company, fixture_json):
    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    with httpx.Client() as client:
        jobs = fetch_company(gh_company, client)
    assert {j.source for j in jobs} == {"greenhouse"}
