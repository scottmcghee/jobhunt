"""ATS adapters: fixture-driven, network mocked with respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobhunt.sources import ashby, fetch_company, greenhouse, lever, smartrecruiters, workday
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


WD = "https://examplecorp.wd5.myworkdayjobs.com/wday/cxs/examplecorp/External"
WD_DETAIL = WD + "/job/Seattle-WA/Director-of-Platform-Engineering_R1001"


def _wants_directors(job):
    return job.title.startswith("Director of Platform")


@respx.mock
def test_workday_lists_everything_but_fetches_bodies_only_when_wanted(workday_company, fixture_json):
    listing = respx.post(WD + "/jobs").mock(return_value=httpx.Response(200, json=fixture_json("workday_jobs.json")))
    detail = respx.get(WD_DETAIL).mock(return_value=httpx.Response(200, json=fixture_json("workday_job.json")))
    with httpx.Client() as client:
        jobs = workday.fetch(workday_company, client, _wants_directors)

    assert [j.title for j in jobs] == ["Director of Platform Engineering", "Senior Software Engineer", "Director of Sales"]
    assert __import__("json").loads(listing.calls.last.request.content) == {
        "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""
    }
    assert detail.call_count == 1  # only the wanted posting

    j = jobs[0]
    assert j.source == "workday"
    assert j.company == "ExampleCorp" and j.company_slug == "examplecorp/External"
    assert j.external_id == "Director-of-Platform-Engineering_R1001"
    assert j.key == "workday:examplecorp/External:Director-of-Platform-Engineering_R1001"
    assert j.url == "https://examplecorp.wd5.myworkdayjobs.com/External/job/Seattle-WA/Director-of-Platform-Engineering_R1001"
    assert j.location == "US-WA-Seattle; US-OR-Remote Location"
    assert j.remote is True
    assert j.posted_at == "2026-10-02"
    assert "infrastructure & developer experience" in j.body and "<" not in j.body

    unwanted = jobs[1]
    assert unwanted.body == "" and unwanted.location == "US-WA-Seattle" and unwanted.remote is None
    assert jobs[2].remote is True  # "US-Remote" in the listing is enough


@respx.mock
def test_workday_paginates_using_first_page_total(workday_company):
    def page(request):
        offset = __import__("json").loads(request.content)["offset"]
        n = min(20, 45 - offset)
        postings = [
            {"title": f"Job {offset + i}", "externalPath": f"/job/X/Job_{offset + i}", "locationsText": "X"}
            for i in range(n)
        ]
        # like the real API, only the first page reports the total
        return httpx.Response(200, json={"total": 45 if offset == 0 else 0, "jobPostings": postings})

    listing = respx.post(WD + "/jobs").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = workday.fetch(workday_company, client, lambda job: False)

    assert len(jobs) == 45 and len({j.external_id for j in jobs}) == 45
    assert listing.call_count == 3


@respx.mock
def test_workday_unknown_site_raises_404(workday_company):
    respx.post(WD + "/jobs").mock(return_value=httpx.Response(404, json={"errorCode": "S21"}))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        workday.fetch(workday_company, client, _wants_directors)


@respx.mock
def test_workday_failed_detail_keeps_job_without_body(workday_company, fixture_json, caplog):
    respx.post(WD + "/jobs").mock(return_value=httpx.Response(200, json=fixture_json("workday_jobs.json")))
    respx.get(WD_DETAIL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client:
        jobs = workday.fetch(workday_company, client, _wants_directors)
    assert len(jobs) == 3 and jobs[0].body == ""
    assert "Director-of-Platform-Engineering_R1001" in caplog.text


@respx.mock
def test_fetch_company_passes_wants_body_to_workday(workday_company, fixture_json):
    respx.post(WD + "/jobs").mock(return_value=httpx.Response(200, json=fixture_json("workday_jobs.json")))
    detail = respx.get(url__startswith=WD + "/job/").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_job.json"))
    )
    with httpx.Client() as client:
        fetch_company(workday_company, client, wants_body=lambda job: False)
        assert detail.call_count == 0
        fetch_company(workday_company, client)  # default: every posting gets its description
        assert detail.call_count == 3


SR = "https://api.smartrecruiters.com/v1/companies/ExampleCorp/postings"
SR_DETAIL = SR + "/744000000001001"


@respx.mock
def test_smartrecruiters_lists_everything_but_fetches_bodies_only_when_wanted(smartrecruiters_company, fixture_json):
    listing = respx.get(SR).mock(return_value=httpx.Response(200, json=fixture_json("smartrecruiters_postings.json")))
    detail = respx.get(SR_DETAIL).mock(return_value=httpx.Response(200, json=fixture_json("smartrecruiters_posting.json")))
    with httpx.Client() as client:
        jobs = smartrecruiters.fetch(smartrecruiters_company, client, _wants_directors)

    assert [j.title for j in jobs] == ["Director of Platform Engineering", "Senior Software Engineer", "Director of Sales"]
    assert dict(listing.calls.last.request.url.params) == {"limit": "100", "offset": "0"}
    assert detail.call_count == 1  # only the wanted posting

    j = jobs[0]
    assert j.source == "smartrecruiters"
    assert j.company == "ExampleCorp" and j.company_slug == "ExampleCorp"
    assert j.external_id == "744000000001001"
    assert j.key == "smartrecruiters:ExampleCorp:744000000001001"
    assert j.url == "https://jobs.smartrecruiters.com/ExampleCorp/744000000001001"
    assert j.location == "Seattle, Washington, United States"
    assert j.remote is True
    assert j.posted_at == "2026-10-02T17:04:11.000Z"
    assert "infrastructure & developer experience" in j.body and "<" not in j.body
    assert "workflow software for regulated industries" in j.body  # company description kept
    assert "10+ years leading platform teams" in j.body and "remote-eligible" in j.body

    assert jobs[1].body == "" and jobs[1].remote is False  # hybrid
    assert jobs[2].remote is None  # neither flag set: on-site and unset look the same


def test_smartrecruiters_remote_from_location_text(smartrecruiters_company):
    posting = {
        "id": "1",
        "name": "VP Engineering",
        "location": {"remote": False, "hybrid": False, "fullLocation": "Remote, United States"},
    }
    assert smartrecruiters.normalize(smartrecruiters_company, posting).remote is True


@respx.mock
def test_smartrecruiters_paginates_using_total_found(smartrecruiters_company):
    def page(request):
        offset = int(request.url.params["offset"])
        n = max(0, min(100, 250 - offset))
        postings = [{"id": str(offset + i), "name": f"Job {offset + i}", "location": {}} for i in range(n)]
        return httpx.Response(200, json={"offset": offset, "limit": 100, "totalFound": 250, "content": postings})

    listing = respx.get(SR).mock(side_effect=page)
    with httpx.Client() as client:
        jobs = smartrecruiters.fetch(smartrecruiters_company, client, lambda job: False)

    assert len(jobs) == 250 and len({j.external_id for j in jobs}) == 250
    assert listing.call_count == 3


@respx.mock
def test_smartrecruiters_empty_board_warns(smartrecruiters_company, caplog):
    # an unknown identifier is a 200 with no postings, not a 404
    respx.get(SR).mock(return_value=httpx.Response(200, json={"offset": 0, "limit": 100, "totalFound": 0, "content": []}))
    with httpx.Client() as client:
        assert smartrecruiters.fetch(smartrecruiters_company, client) == []
    assert "check the identifier" in caplog.text


@respx.mock
def test_smartrecruiters_failed_detail_keeps_job_without_body(smartrecruiters_company, fixture_json, caplog):
    respx.get(SR).mock(return_value=httpx.Response(200, json=fixture_json("smartrecruiters_postings.json")))
    respx.get(SR_DETAIL).mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        jobs = smartrecruiters.fetch(smartrecruiters_company, client, _wants_directors)
    assert len(jobs) == 3 and jobs[0].body == ""
    assert "744000000001001" in caplog.text


@respx.mock
def test_fetch_company_passes_wants_body_to_smartrecruiters(smartrecruiters_company, fixture_json):
    respx.get(SR).mock(return_value=httpx.Response(200, json=fixture_json("smartrecruiters_postings.json")))
    detail = respx.get(url__startswith=SR + "/").mock(
        return_value=httpx.Response(200, json=fixture_json("smartrecruiters_posting.json"))
    )
    with httpx.Client() as client:
        fetch_company(smartrecruiters_company, client, wants_body=lambda job: False)
        assert detail.call_count == 0
        fetch_company(smartrecruiters_company, client)  # default: every posting gets its description
        assert detail.call_count == 3
