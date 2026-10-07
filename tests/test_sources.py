"""ATS adapters: fixture-driven, network mocked with respx."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import respx

from jobhunt.filter import check_location
from jobhunt.schema import Company
from jobhunt.sources import (
    amazon,
    apple,
    ashby,
    bamboohr,
    eightfold,
    fetch_company,
    greenhouse,
    lever,
    oracle,
    rate_group,
    request_group,
    smartrecruiters,
    workable,
    workday,
)
from jobhunt.sources._html import to_text
from tests.conftest import FIXTURES


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


@respx.mock
def test_workday_no_description_warning_fits_on_one_line(workday_company, fixture_json, caplog):
    respx.post(WD + "/jobs").mock(return_value=httpx.Response(200, json=fixture_json("workday_jobs.json")))
    respx.get(WD_DETAIL).mock(side_effect=httpx.ConnectError("boom\nFor more information: x"))
    with httpx.Client() as client:
        jobs = workday.fetch(workday_company, client, _wants_directors)
    assert jobs[0].body == ""
    (warning,) = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "no description" in warning and "boom For more information: x" in warning
    assert "\n" not in warning


SR = "https://api.smartrecruiters.com/v1/companies/ExampleCorp/postings"
GH_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"
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


@respx.mock
def test_smartrecruiters_no_description_warning_fits_on_one_line(smartrecruiters_company, fixture_json, caplog):
    respx.get(SR).mock(return_value=httpx.Response(200, json=fixture_json("smartrecruiters_postings.json")))
    respx.get(SR_DETAIL).mock(side_effect=httpx.ConnectError("boom\nFor more information: x"))
    with httpx.Client() as client:
        jobs = smartrecruiters.fetch(smartrecruiters_company, client, _wants_directors)
    assert jobs[0].body == ""
    (warning,) = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "no description" in warning and "boom For more information: x" in warning
    assert "\n" not in warning


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


@respx.mock
def test_workday_max_pages_stops_early(workday_company):
    page = {
        "total": 45,
        "jobPostings": [{"title": f"Job {i}", "externalPath": f"/job/X/Job_{i}", "locationsText": "X"} for i in range(20)],
    }
    listing = respx.post(WD + "/jobs").mock(return_value=httpx.Response(200, json=page))
    with httpx.Client() as client:
        jobs = workday.fetch(workday_company, client, lambda job: False, max_pages=1)
    assert len(jobs) == 20 and listing.call_count == 1


@respx.mock
def test_smartrecruiters_max_pages_stops_early(smartrecruiters_company):
    page = {"offset": 0, "limit": 100, "totalFound": 250, "content": [{"id": str(i), "name": "Job", "location": {}} for i in range(100)]}
    listing = respx.get(SR).mock(return_value=httpx.Response(200, json=page))
    with httpx.Client() as client:
        jobs = smartrecruiters.fetch(smartrecruiters_company, client, lambda job: False, max_pages=1)
    assert len(jobs) == 100 and listing.call_count == 1


@respx.mock
def test_fetch_company_passes_max_pages(smartrecruiters_company, gh_company, fixture_json):
    page = {"offset": 0, "limit": 100, "totalFound": 250, "content": [{"id": str(i), "name": "Job", "location": {}} for i in range(100)]}
    listing = respx.get(SR).mock(return_value=httpx.Response(200, json=page))
    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    with httpx.Client() as client:
        fetch_company(smartrecruiters_company, client, wants_body=lambda job: False, max_pages=1)
        assert listing.call_count == 1
        assert len(fetch_company(gh_company, client, max_pages=1)) == 4  # one request anyway



# ------------------------------------------------------------------ Workable

WK = "https://apply.workable.com/api/v1/widget/accounts/examplecorp"


@respx.mock
def test_workable_fetch_normalizes(workable_company, fixture_json):
    route = respx.get(WK).mock(return_value=httpx.Response(200, json=fixture_json("workable_account.json")))
    with httpx.Client() as client:
        jobs = workable.fetch(workable_company, client)
    assert route.calls.last.request.url.params["details"] == "true"  # descriptions in one request
    assert [j.title for j in jobs] == ["Director of Platform Engineering", "Senior Software Engineer", "Head of Infrastructure"]
    first = jobs[0]
    assert (first.source, first.company, first.company_slug, first.external_id) == (
        "workable", "ExampleCorp", "examplecorp", "A1B2C3D4E5"
    )
    assert first.url == "https://apply.workable.com/j/A1B2C3D4E5"
    assert first.location == "United States"
    assert first.remote is True  # telecommuting
    assert first.body.startswith("Lead our platform & infrastructure teams.")
    assert first.posted_at == "2026-09-20"
    assert jobs[1].location == "Seattle, Washington, United States"
    assert jobs[1].remote is None  # not telecommuting doesn't say on-site or hybrid
    assert jobs[2].location == "Austin, Texas, United States; Bellevue, Washington, United States"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"telecommuting": True}, True),
        ({"telecommuting": False, "city": "Remote"}, True),
        ({"telecommuting": False, "title": "Director, Platform (Remote)"}, True),
        ({"telecommuting": False, "city": "Austin"}, None),
    ],
)
def test_workable_remote(raw, expected):
    assert workable._is_remote(raw, workable._location(raw)) is expected


def test_workable_location_falls_back_to_the_top_level_fields():
    raw = {"city": "Paris", "state": "Île-de-France", "country": "France", "locations": []}
    assert workable._location(raw) == "Paris, Île-de-France, France"


@pytest.mark.parametrize(
    ("raw", "field", "expected"),
    [
        ({}, "url", "https://apply.workable.com/examplecorp/j/K1L2M3N4O5"),  # no url: built from the shortcode
        ({"locations": [{"city": "Austin", "country": "US"}] * 2}, "location", "Austin, US"),  # duplicates dropped
        ({"created_at": "2026-09-01"}, "posted_at", "2026-09-01"),  # no published_on
    ],
)
def test_workable_normalize_fallbacks(workable_company, raw, field, expected):
    job = workable.normalize(workable_company, {"shortcode": "K1L2M3N4O5", **raw})
    assert getattr(job, field) == expected


@respx.mock
def test_workable_unknown_account_raises_404(workable_company):
    respx.get(WK).mock(return_value=httpx.Response(404, text="Not Found"))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        workable.fetch(workable_company, client)


# ------------------------------------------------------------------ BambooHR

BH = "https://examplecorp.bamboohr.com/careers"


@respx.mock
def test_bamboohr_lists_everything_but_fetches_bodies_only_when_wanted(bamboohr_company, fixture_json):
    listing = respx.get(BH + "/list").mock(return_value=httpx.Response(200, json=fixture_json("bamboohr_list.json")))
    detail = respx.get(BH + "/101/detail").mock(return_value=httpx.Response(200, json=fixture_json("bamboohr_job.json")))
    with httpx.Client() as client:
        jobs = bamboohr.fetch(bamboohr_company, client, lambda job: job.external_id == "101")
    assert listing.call_count == 1 and detail.call_count == 1
    assert [j.title for j in jobs] == ["Director of Platform Engineering", "Office Manager", "VP, Infrastructure"]
    first = jobs[0]
    assert (first.source, first.company_slug, first.external_id) == ("bamboohr", "examplecorp", "101")
    assert first.url == "https://examplecorp.bamboohr.com/careers/101"
    assert first.remote is True and first.location == "United States"
    assert first.body == "Own our platform & developer experience.\n\nCompensation: $200,000 - $240,000"
    assert first.posted_at == "2026-09-28"
    office, vp = jobs[1], jobs[2]
    assert (office.remote, office.location, office.body) == (False, "Seattle, Washington", "")
    assert (vp.remote, vp.location) == (False, "Bellevue, Washington")  # hybrid counts as not remote


@pytest.mark.parametrize(
    ("location_type", "is_remote", "expected"),
    [
        ("0", None, False), ("1", None, True), ("2", None, False), (1, None, True),  # an integer means the same
        (None, True, True), (None, None, None), ("9", None, None),
    ],
)
def test_bamboohr_remote(location_type, is_remote, expected):
    assert bamboohr._is_remote({"locationType": location_type, "isRemote": is_remote}) is expected


def test_bamboohr_remote_with_no_place_says_remote():
    raw = {"locationType": "1", "location": {"city": None, "state": None}, "atsLocation": None}
    assert bamboohr._location(raw) == "Remote"


def test_bamboohr_location_falls_back_to_the_province():
    raw = {"location": {"city": None, "state": None}, "atsLocation": {"province": "Ontario", "country": "Canada"}}
    assert bamboohr._location(raw) == "Ontario, Canada"


@pytest.mark.parametrize(
    ("opening", "expected"),
    [
        ({"description": "<p>Lead.</p>", "compensation": "$1"}, "Lead.\n\nCompensation: $1"),
        ({"description": None, "compensation": "$1"}, "Compensation: $1"),
        ({"description": "<p>Lead.</p>", "compensation": " "}, "Lead."),
    ],
)
def test_bamboohr_body(opening, expected):
    assert bamboohr._body(opening) == expected


@respx.mock
def test_bamboohr_detail_redirect_isnt_followed(bamboohr_company, fixture_json):
    respx.get(BH + "/list").mock(return_value=httpx.Response(200, json=fixture_json("bamboohr_list.json")))
    respx.get(BH + "/101/detail").mock(return_value=httpx.Response(302, headers={"Location": "https://elsewhere.example/x"}))
    elsewhere = respx.get("https://elsewhere.example/x").mock(
        return_value=httpx.Response(200, json=fixture_json("bamboohr_job.json"))
    )
    with httpx.Client(follow_redirects=True) as client:
        jobs = bamboohr.fetch(bamboohr_company, client, lambda job: job.external_id == "101")
    assert len(jobs) == 3 and jobs[0].body == "" and elsewhere.call_count == 0


@respx.mock
def test_bamboohr_failed_detail_keeps_job_without_body(bamboohr_company, fixture_json, caplog):
    respx.get(BH + "/list").mock(return_value=httpx.Response(200, json=fixture_json("bamboohr_list.json")))
    respx.get(BH + "/101/detail").mock(side_effect=httpx.ConnectError("boom\nmore"))
    with httpx.Client() as client:
        jobs = bamboohr.fetch(bamboohr_company, client, lambda job: job.external_id == "101")
    assert jobs[0].body == "" and len(jobs) == 3
    (line,) = [r.getMessage() for r in caplog.records if "no description" in r.getMessage()]
    assert line == "bamboohr examplecorp: no description for 101 (boom more)"


@respx.mock
def test_bamboohr_unknown_tenant_redirects_and_raises(bamboohr_company):
    respx.get(BH + "/list").mock(return_value=httpx.Response(302, headers={"Location": "https://www.bamboohr.com/"}))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError) as e:
        bamboohr.fetch(bamboohr_company, client)
    assert e.value.response.status_code == 302


@respx.mock
def test_bamboohr_unknown_tenant_raises_even_when_the_client_follows_redirects(bamboohr_company):
    # slugs --check uses such a client; following to bamboohr.com's home page would give HTML
    respx.get(BH + "/list").mock(return_value=httpx.Response(302, headers={"Location": "https://www.bamboohr.com/"}))
    home = respx.get("https://www.bamboohr.com/").mock(return_value=httpx.Response(200, text="<html></html>"))
    with httpx.Client(follow_redirects=True) as client, pytest.raises(httpx.HTTPStatusError) as e:
        bamboohr.fetch(bamboohr_company, client)
    assert e.value.response.status_code == 302 and home.call_count == 0


@respx.mock
def test_fetch_company_passes_wants_body_to_bamboohr(bamboohr_company, fixture_json):
    respx.get(BH + "/list").mock(return_value=httpx.Response(200, json=fixture_json("bamboohr_list.json")))
    detail = respx.get(url__regex=BH + r"/\d+/detail").mock(
        return_value=httpx.Response(200, json=fixture_json("bamboohr_job.json"))
    )
    with httpx.Client() as client:
        fetch_company(bamboohr_company, client, wants_body=lambda job: False)
        assert detail.call_count == 0
        with ThreadPoolExecutor(2) as pool:
            fetch_company(bamboohr_company, client, pool=pool)
        assert detail.call_count == 3


# ------------------------------------------------------------------ Amazon (search, not a full listing)

AZ = "https://www.amazon.jobs/en/search.json"


@respx.mock
def test_amazon_searches_each_term_and_normalizes(amazon_company, fixture_json):
    route = respx.get(AZ).mock(return_value=httpx.Response(200, json=fixture_json("amazon_search.json")))
    with httpx.Client() as client:
        jobs = amazon.fetch(amazon_company, client, ["director", "senior manager"])
    assert route.call_count == 2  # one page each: 3 hits fit in one page
    params = [call.request.url.params for call in route.calls]
    assert [p["base_query"] for p in params] == ["director", "senior manager"]
    assert all(p["normalized_country_code[]"] == "USA" and p["result_limit"] == "100" for p in params)
    assert all(p["sort"] == "recent" for p in params)  # roughly newest first, so a capped term keeps mostly new ones
    assert [j.external_id for j in jobs] == ["10000001", "10000002", "10000003", "10000004"]  # deduped
    first = jobs[0]
    assert (first.source, first.company, first.company_slug) == ("amazon", "Amazon", "USA")
    assert first.title == "Director, Platform Engineering"
    assert first.location == "Seattle, Washington, USA"
    assert first.url == "https://www.amazon.jobs/en/jobs/10000001/director-platform-engineering"
    assert first.posted_at == "2026-10-06"  # "October  6, 2026"
    assert first.body.startswith("Lead our platform & infrastructure org.")
    assert "Basic qualifications" in first.body and "Experience with AWS" in first.body
    assert "Preferred qualifications" in first.body
    assert first.remote is None
    assert jobs[1].remote is True  # "US, Virtual"
    assert jobs[3].location == "Austin, Texas, USA; Seattle, Washington, USA"  # every location
    assert jobs[3].remote is False  # every location is ONSITE


def test_amazon_a_secondary_seattle_location_passes_the_location_filter(amazon_company, fixture_json, prefs):
    raw = fixture_json("amazon_search.json")["jobs"][3]
    job = amazon.normalize(amazon_company, raw)
    assert check_location(job, prefs) is None
    alone = amazon.normalize(amazon_company, {**raw, "locations": raw["locations"][:1]})
    assert check_location(alone, prefs) is not None  # Austin on its own doesn't pass


def _az_loc(city: str, kind: str | None) -> str:
    entry = {"normalizedLocation": f"{city}, USA", "city": city}
    return json.dumps(entry | ({"type": kind} if kind else {}))


@pytest.mark.parametrize(
    ("locations", "location", "remote"),
    [
        ([_az_loc("Austin", "ONSITE"), _az_loc("Virtual", "VIRTUAL")], "Austin, USA; Virtual, USA", True),
        ([_az_loc("Austin", "ONSITE"), _az_loc("Austin", "ONSITE")], "Austin, USA", False),  # deduped
        ([_az_loc("Austin", "ONSITE"), _az_loc("Dallas", None)], "Austin, USA; Dallas, USA", None),
        ([{"normalizedLocation": "Austin, USA", "type": "VIRTUAL"}], "Austin, USA", True),  # a dict, not a string
        (["{not json", 7, None, json.dumps(["a list"])], "US, TX, Austin", None),  # falls back to location
        ([], "US, TX, Austin", None),
    ],
)
def test_amazon_locations_and_remote_come_from_every_location(amazon_company, locations, location, remote):
    raw = {"id": "u1", "title": "Director", "location": "US, TX, Austin", "locations": locations}
    job = amazon.normalize(amazon_company, raw)
    assert (job.location, job.remote) == (location, remote)


@pytest.mark.parametrize(
    ("raw", "remote"),
    [
        ({"location": "US, Virtual"}, True),
        ({"normalized_location": "Remote, USA"}, True),
        ({"location": "US, TX, Austin", "title": "Director, Remote Operations"}, True),  # remote in the title
        ({"location": "US, TX, Austin", "title": "Director, Virtual Care"}, True),
        ({"location": "US, TX, Austin", "title": "Director"}, None),
        ({"location": "US, Virtual", "locations": ["{bad"]}, True),  # unreadable locations: the text decides
    ],
)
def test_amazon_remote_falls_back_to_the_location_and_title_text(amazon_company, raw, remote):
    assert amazon.normalize(amazon_company, {"id": "u1", "title": "Director"} | raw).remote is remote


@respx.mock
def test_amazon_pages_until_the_hits_are_in(amazon_company):
    def page(request):
        offset = int(request.url.params["offset"])
        n = max(0, min(100, 250 - offset))
        jobs = [{"id": f"u{offset + i}", "id_icims": str(offset + i), "title": "Director", "location": "US, WA, Seattle",
                 "job_path": f"/en/jobs/{offset + i}/x", "description": ""} for i in range(n)]
        return httpx.Response(200, json={"error": None, "hits": 250, "jobs": jobs})

    route = respx.get(AZ).mock(side_effect=page)
    with httpx.Client() as client:
        jobs = amazon.fetch(amazon_company, client, ["director"])
        assert len(jobs) == 250 and route.call_count == 3
        assert len(amazon.fetch(amazon_company, client, ["director"], max_pages=1)) == 100


@respx.mock
def test_amazon_a_broad_term_stops_at_the_cap_and_says_so(amazon_company, caplog):
    def page(request):
        offset = int(request.url.params["offset"])
        jobs = [{"id": f"u{offset + i}", "id_icims": str(offset + i), "title": "Manager", "job_path": "/x"}
                for i in range(100)]
        return httpx.Response(200, json={"error": None, "hits": 5883, "jobs": jobs})

    route = respx.get(AZ).mock(side_effect=page)
    with httpx.Client() as client, caplog.at_level("WARNING"):
        jobs = amazon.fetch(amazon_company, client, ["manager"])
    assert route.call_count == 20 and len(jobs) == 2000
    assert "amazon USA: manager has 5883 hits; kept the first 2000 in Amazon's 'recent' order (not strictly by posting date)" in caplog.messages


@respx.mock
def test_amazon_the_first_posting_seen_wins_across_terms(amazon_company):
    def page(request):
        title = request.url.params["base_query"].title()
        return httpx.Response(200, json={"error": None, "hits": 1, "jobs": [{"id": "u1", "title": title}]})

    respx.get(AZ).mock(side_effect=page)
    with httpx.Client() as client:
        jobs = amazon.fetch(amazon_company, client, ["director", "head of"])
    assert [j.title for j in jobs] == ["Director"]


@respx.mock
def test_amazon_warns_about_a_board_with_no_postings(amazon_company, caplog):
    respx.get(AZ).mock(return_value=httpx.Response(200, json={"error": None, "hits": 0, "jobs": []}))
    with httpx.Client() as client, caplog.at_level("WARNING"):
        assert amazon.fetch(amazon_company, client, ["director", "vp"]) == []
    assert caplog.messages == ["amazon USA: 0 postings — check the country code (ISO alpha-3, e.g. USA)"]


@respx.mock
def test_amazon_warns_once_per_wildcard_term(amazon_company, caplog):
    respx.get(AZ).mock(return_value=httpx.Response(200, json={"error": None, "hits": 0, "jobs": []}))
    with httpx.Client() as client, caplog.at_level("WARNING"):
        amazon.fetch(amazon_company, client, ["recruit*", "Recruit* ", "director"])
    wildcard = [m for m in caplog.messages if "prefix" in m]
    assert wildcard == ["amazon can't search by prefix; searching 'recruit' only for 'recruit*'"]


@respx.mock
def test_amazon_an_empty_page_ends_the_listing(amazon_company):
    route = respx.get(AZ).mock(return_value=httpx.Response(200, json={"error": None, "hits": 5000, "jobs": []}))
    with httpx.Client() as client:
        assert amazon.fetch(amazon_company, client, ["director"]) == []
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("terms", "queries"),
    [
        ([], [""]),  # no terms: one unfiltered search (slugs --check)
        (["director", "Director ", "recruit*", "  "], ["director", "recruit"]),
    ],
)
@respx.mock
def test_amazon_search_terms_are_cleaned_up(amazon_company, terms, queries):
    route = respx.get(AZ).mock(return_value=httpx.Response(200, json={"error": None, "hits": 0, "jobs": []}))
    with httpx.Client() as client:
        amazon.fetch(amazon_company, client, terms)
    assert [call.request.url.params["base_query"] for call in route.calls] == queries


@respx.mock
def test_amazon_reports_an_error_in_the_payload(amazon_company):
    respx.get(AZ).mock(return_value=httpx.Response(200, json={"error": "bad query", "hits": 0, "jobs": []}))
    with httpx.Client() as client, pytest.raises(ValueError, match="bad query"):
        amazon.fetch(amazon_company, client, ["director"])


@pytest.mark.parametrize("raw", ["", "not a date", None])
def test_amazon_an_unreadable_date_is_left_out(raw):
    assert amazon._posted(raw) is None


@respx.mock
def test_fetch_company_passes_search_terms_only_to_search_sources(amazon_company, gh_company, fixture_json):
    route = respx.get(AZ).mock(return_value=httpx.Response(200, json=fixture_json("amazon_search.json")))
    respx.get(GH_JOBS_URL).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    with httpx.Client() as client:
        fetch_company(amazon_company, client, search=["vice president"])
        assert route.calls.last.request.url.params["base_query"] == "vice president"
        assert len(fetch_company(gh_company, client, search=["vice president"])) == 4  # ignored


# ------------------------------------------------------------------ Eightfold (search; a platform)

EF = "https://example.eightfold.ai"


def _eightfold_routes(fixture_json, careers_html=None):
    page = (careers_html or (FIXTURES / "eightfold_careers.html").read_text())
    careers = respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text=page))
    search = respx.get(EF + "/api/pcsx/search").mock(
        return_value=httpx.Response(200, json=fixture_json("eightfold_search.json"))
    )
    detail = respx.get(EF + "/api/pcsx/position_details").mock(
        return_value=httpx.Response(200, json=fixture_json("eightfold_position.json"))
    )
    return careers, search, detail


@respx.mock
def test_eightfold_finds_the_domain_searches_and_normalizes(eightfold_company, fixture_json):
    careers, search, detail = _eightfold_routes(fixture_json)
    with httpx.Client() as client:
        jobs = eightfold.fetch(
            eightfold_company, client, ["director", "senior manager"], wants_body=lambda j: j.external_id == "900000000001"
        )
    assert careers.call_count == 1  # the domain is read once per board
    params = [call.request.url.params for call in search.calls]
    assert [p["query"] for p in params] == ["director", "senior manager"]
    assert all(p["domain"] == "example.com" and p["start"] == "0" and "location" not in p for p in params)
    assert [j.external_id for j in jobs] == ["900000000001", "900000000002", "900000000003", "900000000004"]  # deduped
    assert detail.call_count == 1
    assert detail.calls.last.request.url.params["position_id"] == "900000000001"
    assert detail.calls.last.request.url.params["domain"] == "example.com"
    first = jobs[0]
    assert (first.source, first.company, first.company_slug) == ("eightfold", "ExampleCorp", "example.eightfold.ai")
    assert first.title == "Director, Platform Engineering"
    assert first.url == "https://example.eightfold.ai/careers/job/900000000001"
    assert first.location == "Seattle, Washington, United States"
    assert first.posted_at == "2026-09-21T14:13:20+00:00"
    assert first.body == "Lead our platform & developer experience teams."
    assert first.remote is False  # hybrid
    assert jobs[1].remote is True and jobs[1].body == ""
    assert jobs[2].remote is False
    assert jobs[2].location == "Austin, Texas, United States; Monterrey, Nuevo Leon, Mexico"
    assert jobs[3].remote is True  # remote_local, Nvidia's US remote roles


@respx.mock
def test_eightfold_passes_a_board_location(fixture_json):
    _, search, _ = _eightfold_routes(fixture_json)
    board = Company(name="X", ats="eightfold", slug="example.eightfold.ai", location="United States")
    with httpx.Client() as client:
        eightfold.fetch(board, client, ["director"], wants_body=lambda j: False)
    assert search.calls.last.request.url.params["location"] == "United States"


@pytest.mark.parametrize(("option", "expected"), [("remote", True), ("remote_local", True), ("onsite", False), ("hybrid", False), ("other", None), (None, None), ("", None)])
def test_eightfold_remote(option, expected):
    assert eightfold._remote({"workLocationOption": option}) is expected


@respx.mock
def test_eightfold_pages_by_ten_until_the_count_is_in(eightfold_company, caplog):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))

    def page(request):
        start = int(request.url.params["start"])
        n = max(0, min(10, 25 - start))
        positions = [{"id": start + i + 1, "name": "Director", "locations": [], "positionUrl": f"/careers/job/{start + i}"}
                     for i in range(n)]
        return httpx.Response(200, json={"data": {"positions": positions, "count": 25}})

    search = respx.get(EF + "/api/pcsx/search").mock(side_effect=page)
    with httpx.Client() as client:
        assert len(eightfold.fetch(eightfold_company, client, ["director"], wants_body=lambda j: False)) == 25
        assert [c.request.url.params["start"] for c in search.calls] == ["0", "10", "20"]
        assert len(eightfold.fetch(eightfold_company, client, ["director"], max_pages=1, wants_body=lambda j: False)) == 10
    assert "kept the first" not in caplog.text  # max_pages, not the cap, stopped it


@respx.mock
def test_eightfold_a_broad_term_stops_at_the_cap_and_says_so(eightfold_company, caplog):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))

    def page(request):
        start = int(request.url.params["start"])
        positions = [{"id": start + i + 1, "name": "Manager", "locations": [], "positionUrl": f"/j/{start + i}"} for i in range(10)]
        return httpx.Response(200, json={"data": {"positions": positions, "count": 9999}})

    search = respx.get(EF + "/api/pcsx/search").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = eightfold.fetch(eightfold_company, client, ["manager"], wants_body=lambda j: False)
    assert len(jobs) == eightfold.MAX_PER_TERM and search.call_count == eightfold.MAX_PER_TERM // 10
    assert "eightfold example.eightfold.ai: 'manager' has 9999 hits; kept the first" in caplog.text


@pytest.mark.parametrize(
    "page",
    [
        '<div>{&#34;domain&#34;: &#34;example.com&#34;}</div>',  # entity-escaped JSON, as served
        '<script>window.cfg = {"domain": "example.com"}</script>',
    ],
)
def test_eightfold_reads_the_domain_from_the_careers_page(page):
    assert eightfold._domain(page) == "example.com"


@respx.mock
def test_eightfold_a_careers_page_without_a_domain_is_an_error(eightfold_company):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text="<html>no config</html>"))
    with httpx.Client() as client, pytest.raises(ValueError, match="no Eightfold domain"):
        eightfold.fetch(eightfold_company, client, ["director"])


@respx.mock
def test_eightfold_failed_detail_keeps_job_without_body(eightfold_company, fixture_json, caplog):
    _eightfold_routes(fixture_json)
    respx.get(EF + "/api/pcsx/position_details").mock(side_effect=httpx.ConnectError("boom\nmore"))
    with httpx.Client() as client:
        jobs = eightfold.fetch(eightfold_company, client, ["director"], wants_body=lambda j: j.external_id == "900000000001")
    assert len(jobs) == 4 and jobs[0].body == ""
    assert "eightfold example.eightfold.ai: no description for 900000000001 (boom more)" in caplog.text


class _RecordingPool(ThreadPoolExecutor):
    """A pool that counts the work handed to it."""

    def __init__(self, workers: int) -> None:
        super().__init__(workers)
        self.used = 0

    def map(self, fn, *iterables, **kwargs):
        self.used += 1
        return super().map(fn, *iterables, **kwargs)

    def submit(self, fn, /, *args, **kwargs):
        self.used += 1
        return super().submit(fn, *args, **kwargs)


@respx.mock
def test_eightfold_descriptions_go_through_the_pool(eightfold_company, fixture_json):
    _, _, detail = _eightfold_routes(fixture_json)
    with httpx.Client() as client, _RecordingPool(2) as pool:
        fetch_company(eightfold_company, client, pool=pool, search=["director"])
    assert detail.call_count == 4
    assert pool.used  # not a serial map


# What an older-interface tenant answers on /api/pcsx (seen live).
PCSX_OFF = {"message": "PCSX is not enabled for this user."}


@respx.mock
def test_eightfold_falls_back_to_the_older_api_when_pcsx_is_forbidden(eightfold_company, fixture_json):
    careers = respx.get(EF + "/careers").mock(
        return_value=httpx.Response(200, text=(FIXTURES / "eightfold_careers.html").read_text())
    )
    pcsx = respx.get(EF + "/api/pcsx/search").mock(return_value=httpx.Response(403, json=PCSX_OFF))
    v2 = respx.get(EF + "/api/apply/v2/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("eightfold_v2_search.json"))
    )
    v2_detail = respx.get(EF + "/api/apply/v2/jobs/800000000001").mock(
        return_value=httpx.Response(200, json=fixture_json("eightfold_v2_position.json"))
    )
    with httpx.Client() as client:
        jobs = eightfold.fetch(
            eightfold_company, client, ["director", "vice president"], wants_body=lambda j: j.external_id == "800000000001"
        )
    assert careers.call_count == 1 and pcsx.call_count == 1  # tried once, then the older API for the rest
    params = [call.request.url.params for call in v2.calls]
    assert [p["query"] for p in params] == ["director", "vice president"]
    assert all(p["domain"] == "example.com" and p["start"] == "0" and p["num"] == "10" for p in params)
    assert v2_detail.call_count == 1 and v2_detail.calls.last.request.url.params["domain"] == "example.com"
    assert [j.external_id for j in jobs] == ["800000000001", "800000000002"]
    first, second = jobs
    assert first.title == "Director, Platform Engineering"
    # the canonical URL's path, on the board's host, without its query
    assert first.url == "https://example.eightfold.ai/careers/job/800000000001/director-platform"
    assert first.location == "Seattle, WA, United States"
    assert first.remote is True
    assert first.posted_at == "2026-09-21T14:13:20+00:00"
    assert first.body == "Lead our platform & developer experience teams."
    assert second.location == "Windsor, UK Office (WINDSOR); Munich, Germany Office (MUNICH)"
    assert second.remote is False and second.body == ""


@respx.mock
def test_eightfold_the_older_api_gets_the_board_location(fixture_json):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))
    respx.get(EF + "/api/pcsx/search").mock(return_value=httpx.Response(403, json=PCSX_OFF))
    v2 = respx.get(EF + "/api/apply/v2/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("eightfold_v2_search.json"))
    )
    board = Company(name="X", ats="eightfold", slug="example.eightfold.ai", location="United States")
    with httpx.Client() as client:
        eightfold.fetch(board, client, ["director"], wants_body=lambda j: False)
    assert v2.calls.last.request.url.params["location"] == "United States"


@respx.mock
def test_eightfold_pages_the_older_api_by_ten(eightfold_company):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))
    respx.get(EF + "/api/pcsx/search").mock(return_value=httpx.Response(403, json=PCSX_OFF))

    def page(request):
        start = int(request.url.params["start"])
        rows = [{"id": start + i + 1, "name": "Director", "locations": []} for i in range(max(0, min(10, 25 - start)))]
        return httpx.Response(200, json={"count": 25, "positions": rows})

    route = respx.get(EF + "/api/apply/v2/jobs").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = eightfold.fetch(eightfold_company, client, ["director"], wants_body=lambda j: False)
    assert len(jobs) == 25 and [c.request.url.params["start"] for c in route.calls] == ["0", "10", "20"]
    assert jobs[0].url == "https://example.eightfold.ai/careers/job/1"  # built when there's no canonical URL


@respx.mock
def test_eightfold_other_errors_dont_switch_apis(eightfold_company):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))
    respx.get(EF + "/api/pcsx/search").mock(return_value=httpx.Response(404))
    v2 = respx.get(EF + "/api/apply/v2/jobs").mock(return_value=httpx.Response(200, json={"count": 0, "positions": []}))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        eightfold.fetch(eightfold_company, client, ["director"])
    assert v2.call_count == 0


@respx.mock
def test_eightfold_a_403_from_both_apis_is_raised(eightfold_company):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))
    respx.get(EF + "/api/pcsx/search").mock(return_value=httpx.Response(403, json=PCSX_OFF))
    respx.get(EF + "/api/apply/v2/jobs").mock(return_value=httpx.Response(403))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError) as e:
        eightfold.fetch(eightfold_company, client, ["director"])
    assert e.value.response.status_code == 403


@respx.mock
def test_eightfold_older_api_failed_detail_keeps_job_without_body(eightfold_company, fixture_json, caplog):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))
    respx.get(EF + "/api/pcsx/search").mock(return_value=httpx.Response(403, json=PCSX_OFF))
    respx.get(EF + "/api/apply/v2/jobs").mock(return_value=httpx.Response(200, json=fixture_json("eightfold_v2_search.json")))
    respx.get(EF + "/api/apply/v2/jobs/800000000001").mock(return_value=httpx.Response(500))
    with httpx.Client() as client:
        jobs = eightfold.fetch(eightfold_company, client, ["director"], wants_body=lambda j: j.external_id == "800000000001")
    assert len(jobs) == 2 and jobs[0].body == ""
    assert "eightfold example.eightfold.ai: no description for 800000000001" in caplog.text


def test_eightfold_older_rows_fall_back_to_location_and_posting_name():
    row = {"id": 7, "posting_name": "Director, Data", "location": "Austin, TX, United States"}
    raw = eightfold._from_older(row)
    job = eightfold.normalize(Company(name="X", ats="eightfold", slug="example.eightfold.ai"), raw)
    assert job.title == "Director, Data" and job.location == "Austin, TX, United States"


@pytest.mark.parametrize(
    "blocked",
    [
        httpx.Response(403, html="<html><body>Access denied</body></html>"),
        httpx.Response(403, json={"message": "Forbidden"}),
        httpx.Response(403),
    ],
    ids=["html", "other-json", "empty"],
)
@respx.mock
def test_eightfold_only_the_pcsx_not_enabled_403_switches_apis(eightfold_company, blocked):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))
    respx.get(EF + "/api/pcsx/search").mock(return_value=blocked)
    v2 = respx.get(EF + "/api/apply/v2/jobs").mock(return_value=httpx.Response(200, json={"count": 0, "positions": []}))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError) as e:
        eightfold.fetch(eightfold_company, client, ["director"])
    assert e.value.response.status_code == 403 and v2.call_count == 0


@pytest.mark.parametrize("slug", ["example.eightfold.ai/careers", "https://example.eightfold.ai", "", "a b"])
def test_an_eightfold_slug_is_a_host(slug):
    with pytest.raises(ValueError, match="host"):
        Company(name="x", ats="eightfold", slug=slug)


def test_location_is_only_for_search_sources():
    with pytest.raises(ValueError, match="location"):
        Company(name="x", ats="greenhouse", slug="x", location="United States")


# ------------------------------------------------------------------ Oracle Recruiting Cloud (search; a platform)

OR = "https://example.fa.us2.oraclecloud.com/hcmRestApi/resources/latest"


def _finder(request):
    """The finder's parameters as a dict: findReqs;siteNumber=CX_1,keyword=director,... ."""
    name, _, rest = request.url.params["finder"].partition(";")
    return name, dict(part.split("=", 1) for part in rest.split(","))


@respx.mock
def test_oracle_searches_each_term_and_normalizes(oracle_company, fixture_json):
    listing = respx.get(OR + "/recruitingCEJobRequisitions").mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_requisitions.json"))
    )
    detail = respx.get(OR + "/recruitingCEJobRequisitionDetails").mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_requisition.json"))
    )
    with httpx.Client() as client:
        jobs = oracle.fetch(oracle_company, client, ["director", "senior manager"], wants_body=lambda j: j.external_id == "300001")
    finders = [_finder(call.request) for call in listing.calls]
    assert [f[1]["keyword"] for f in finders] == ["director", "senior manager"]
    assert all(f[0] == "findReqs" and f[1]["siteNumber"] == "CX_1" and f[1]["limit"] == "200" and f[1]["offset"] == "0" for f in finders)
    assert [j.external_id for j in jobs] == ["300001", "300002", "300003"]  # deduped across terms
    assert detail.call_count == 1
    name, args = _finder(detail.calls.last.request)
    assert name == "ById" and args == {"Id": '"300001"', "siteNumber": "CX_1"}
    first = jobs[0]
    assert (first.source, first.company, first.company_slug) == ("oracle", "ExampleCorp", "example.fa.us2.oraclecloud.com/CX_1")
    assert first.url == "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/300001"
    assert first.location == "Seattle, WA, United States; Bellevue, WA, United States"
    assert first.remote is False  # hybrid
    assert first.posted_at == "2026-09-21"
    assert first.body.startswith("Lead our platform & developer experience teams.")
    assert "Own reliability" in first.body and "10+ years leading" in first.body
    assert "ExampleCorp builds things" not in first.body  # boilerplate about the company is left out
    assert jobs[1].remote is True  # no code, but "Remote" in the location
    assert jobs[2].remote is False and jobs[2].body == ""


@pytest.mark.parametrize(
    ("code", "location", "title", "expected"),
    [
        ("ORA_REMOTE", "Austin, TX", "Director", True),
        ("ORA_ON_SITE", "Remote, US", "Director", False),  # the code wins over text
        ("ORA_HYBRID", "Austin, TX", "Director", False),
        (None, "Austin, TX", "Director (Remote)", True),
        (None, "Austin, TX", "Director", None),  # blank is unknown: the location filter decides
        ("", "", "", None),
    ],
)
def test_oracle_remote(code, location, title, expected):
    assert oracle._remote({"WorkplaceTypeCode": code, "Title": title}, location) is expected


def test_oracle_a_blank_code_with_a_remote_body_passes_the_location_filter(oracle_company, prefs):
    # Austin is outside onsite_accept_any, so only the remote body can let it through.
    raw = {"Id": "1", "Title": "Director, Platform Engineering", "PrimaryLocation": "Austin, TX, United States", "WorkplaceTypeCode": None}
    detail = {"ExternalDescriptionStr": "<p>Lead our cloud platform team. Position is remote.</p>"}
    assert check_location(oracle.normalize(oracle_company, raw, detail), prefs) is None
    assert check_location(oracle.normalize(oracle_company, raw, None), prefs) is not None


@respx.mock
def test_oracle_pages_by_200_until_the_total_is_in(oracle_company):
    def page(request):
        offset = int(_finder(request)[1]["offset"])
        n = max(0, min(200, 450 - offset))
        reqs = [{"Id": str(offset + i + 1), "Title": "Director", "PrimaryLocation": "X"} for i in range(n)]
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 450, "requisitionList": reqs}]})

    route = respx.get(OR + "/recruitingCEJobRequisitions").mock(side_effect=page)
    with httpx.Client() as client:
        assert len(oracle.fetch(oracle_company, client, ["director"], wants_body=lambda j: False)) == 450
        assert [_finder(c.request)[1]["offset"] for c in route.calls] == ["0", "200", "400"]
        assert len(oracle.fetch(oracle_company, client, ["director"], max_pages=1, wants_body=lambda j: False)) == 200


@respx.mock
def test_oracle_a_broad_term_stops_at_the_cap_and_says_so(oracle_company, caplog):
    def page(request):
        offset = int(_finder(request)[1]["offset"])
        reqs = [{"Id": str(offset + i + 1), "Title": "Manager", "PrimaryLocation": "X"} for i in range(200)]
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 5000, "requisitionList": reqs}]})

    route = respx.get(OR + "/recruitingCEJobRequisitions").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = oracle.fetch(oracle_company, client, ["manager"], wants_body=lambda j: False)
        assert len(jobs) == oracle.MAX_PER_TERM and route.call_count == oracle.MAX_PER_TERM // 200
        assert "'manager' has 5000 hits; kept the first" in caplog.text
        caplog.clear()
        oracle.fetch(oracle_company, client, ["manager"], max_pages=1, wants_body=lambda j: False)
    assert "kept the first" not in caplog.text


@respx.mock
def test_oracle_an_empty_page_ends_the_listing(oracle_company):
    route = respx.get(OR + "/recruitingCEJobRequisitions").mock(
        return_value=httpx.Response(200, json={"items": [{"TotalJobsCount": 900, "requisitionList": []}]})
    )
    with httpx.Client() as client:
        assert oracle.fetch(oracle_company, client, ["director"]) == []
    assert route.call_count == 1


@respx.mock
def test_oracle_failed_detail_keeps_job_without_body(oracle_company, fixture_json, caplog):
    respx.get(OR + "/recruitingCEJobRequisitions").mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_requisitions.json"))
    )
    respx.get(OR + "/recruitingCEJobRequisitionDetails").mock(side_effect=httpx.ConnectError("boom\nmore"))
    with httpx.Client() as client:
        jobs = oracle.fetch(oracle_company, client, ["director"], wants_body=lambda j: j.external_id == "300001")
    assert len(jobs) == 3 and jobs[0].body == ""
    assert "oracle example.fa.us2.oraclecloud.com/CX_1: no description for 300001 (boom more)" in caplog.text


@respx.mock
def test_oracle_descriptions_go_through_the_pool(oracle_company, fixture_json):
    respx.get(OR + "/recruitingCEJobRequisitions").mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_requisitions.json"))
    )
    detail = respx.get(OR + "/recruitingCEJobRequisitionDetails").mock(
        return_value=httpx.Response(200, json=fixture_json("oracle_requisition.json"))
    )
    used = []

    class Pool(ThreadPoolExecutor):
        def map(self, *a, **kw):
            used.append(True)
            return super().map(*a, **kw)

    with httpx.Client() as client, Pool(2) as pool:
        fetch_company(oracle_company, client, pool=pool, search=["director"])
    assert used and detail.call_count == 3


@pytest.mark.parametrize(
    "slug",
    ["example.fa.us2.oraclecloud.com", "example.fa.us2.oraclecloud.com/", "/CX_1", "example.fa.us2.oraclecloud.com/CX_1/x", "a b/CX_1"],
)
def test_an_oracle_slug_is_host_slash_site(slug):
    with pytest.raises(ValueError, match="host/site"):
        Company(name="x", ats="oracle", slug=slug)


# ------------------------------------------------------------------ Apple (search; server-rendered HTML)

AP = "https://jobs.apple.com/en-us"


def _apple_routes():
    search = respx.get(AP + "/search").mock(
        return_value=httpx.Response(200, text=(FIXTURES / "apple_search.html").read_text())
    )
    detail = respx.get(url__startswith=AP + "/details/").mock(
        return_value=httpx.Response(200, text=(FIXTURES / "apple_details.html").read_text())
    )
    return search, detail


@respx.mock
def test_apple_searches_each_term_and_normalizes(apple_company):
    search, detail = _apple_routes()
    with httpx.Client() as client:
        jobs = apple.fetch(apple_company, client, ["director", "senior manager"], wants_body=lambda j: j.external_id == "200000001-0001")
    params = [call.request.url.params for call in search.calls]
    assert [p["search"] for p in params] == ["director", '"senior manager"']  # a phrase, or Apple matches either word
    assert all(p["location"] == "united-states-USA" and p["page"] == "1" for p in params)
    assert [j.external_id for j in jobs] == ["200000001-0001", "200000002-0001", "200000003-0001"]  # deduped
    assert detail.call_count == 1
    assert str(detail.calls.last.request.url) == AP + "/details/200000001-0001/director-platform-engineering"
    first = jobs[0]
    assert (first.source, first.company, first.company_slug) == ("apple", "Apple", "united-states-USA")
    assert first.url == AP + "/details/200000001-0001/director-platform-engineering"
    assert first.location == "Seattle, United States of America; Cupertino, United States of America"
    assert first.posted_at == "2026-09-21T17:19:04.171+00:00"
    assert first.body.startswith("The Platform team builds the tools every Apple engineer uses.\n\nLead our platform")
    for part in ("Own reliability", "Minimum qualifications:\n10+ years", "Preferred qualifications:\nExperience with internal"):
        assert part in first.body
    assert first.remote is None  # homeOffice false says nothing more
    assert jobs[1].remote is True and jobs[1].body == ""  # homeOffice true


@respx.mock
def test_apple_pages_by_twenty_until_the_total_is_in(apple_company):
    def page(request):
        n = int(request.url.params["page"])
        first = (n - 1) * 20
        rows = [{"id": f"r{first + i}", "postingTitle": "Director", "transformedPostingTitle": "d", "locations": []}
                for i in range(max(0, min(20, 45 - first)))]
        loader = {"search": {"searchResults": rows, "totalRecords": 45}}
        body = f"<script>window.__staticRouterHydrationData = JSON.parse({json.dumps(json.dumps({'loaderData': loader}))});</script>"
        return httpx.Response(200, text=body)

    route = respx.get(AP + "/search").mock(side_effect=page)
    with httpx.Client() as client:
        assert len(apple.fetch(apple_company, client, ["director"], wants_body=lambda j: False)) == 45
        assert [c.request.url.params["page"] for c in route.calls] == ["1", "2", "3"]
        assert len(apple.fetch(apple_company, client, ["director"], max_pages=1, wants_body=lambda j: False)) == 20


@respx.mock
def test_apple_a_broad_term_stops_at_the_cap_and_says_so(apple_company, caplog):
    def page(request):
        n = int(request.url.params["page"])
        rows = [{"id": f"r{n}-{i}", "postingTitle": "Manager", "transformedPostingTitle": "m", "locations": []} for i in range(20)]
        loader = {"search": {"searchResults": rows, "totalRecords": 2297}}
        return httpx.Response(200, text=f"<script>window.__staticRouterHydrationData = JSON.parse({json.dumps(json.dumps({'loaderData': loader}))});</script>")

    route = respx.get(AP + "/search").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = apple.fetch(apple_company, client, ["manager"], wants_body=lambda j: False)
        assert len(jobs) == apple.MAX_PER_TERM and route.call_count == apple.MAX_PER_TERM // 20
        assert "'manager' has 2297 hits; kept the first" in caplog.text
        caplog.clear()
        apple.fetch(apple_company, client, ["manager"], max_pages=1, wants_body=lambda j: False)
    assert "kept the first" not in caplog.text


@respx.mock
def test_apple_a_page_without_its_data_is_an_error(apple_company):
    respx.get(AP + "/search").mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    with httpx.Client() as client, pytest.raises(ValueError, match="no job data"):
        apple.fetch(apple_company, client, ["director"])


@respx.mock
def test_apple_failed_detail_keeps_job_without_body(apple_company, caplog):
    _apple_routes()
    respx.get(url__startswith=AP + "/details/").mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with httpx.Client() as client:
        jobs = apple.fetch(apple_company, client, ["director"], wants_body=lambda j: j.external_id == "200000001-0001")
    assert len(jobs) == 3 and jobs[0].body == ""
    assert "apple united-states-USA: no description for 200000001-0001" in caplog.text


@respx.mock
def test_apple_detail_http_error_keeps_job_without_body(apple_company, caplog):
    _apple_routes()
    respx.get(url__startswith=AP + "/details/").mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        jobs = apple.fetch(apple_company, client, ["director"], wants_body=lambda j: j.external_id == "200000001-0001")
    assert len(jobs) == 3 and jobs[0].body == ""
    assert "apple united-states-USA: no description for 200000001-0001 (Client error '404 Not Found'" in caplog.text


@respx.mock
def test_apple_warns_about_a_board_with_no_postings(apple_company, caplog):
    loader = {"search": {"searchResults": [], "totalRecords": 0}}
    page = f"<script>window.__staticRouterHydrationData = JSON.parse({json.dumps(json.dumps({'loaderData': loader}))});</script>"
    respx.get(AP + "/search").mock(return_value=httpx.Response(200, text=page))
    with httpx.Client() as client, caplog.at_level("WARNING"):
        assert apple.fetch(apple_company, client, ["director", "vp"]) == []
    assert caplog.messages == [
        "apple united-states-USA: 0 postings — check the location filter (e.g. united-states-USA)"
    ]


@respx.mock
def test_apple_descriptions_go_through_the_pool(apple_company):
    _, detail = _apple_routes()
    used = []

    class Pool(ThreadPoolExecutor):
        def map(self, *a, **kw):
            used.append(True)
            return super().map(*a, **kw)

    with httpx.Client() as client, Pool(2) as pool:
        fetch_company(apple_company, client, pool=pool, search=["director"])
    assert used and detail.call_count == 3


@pytest.mark.parametrize(
    ("locations", "expected"),
    [
        ([{"city": "Austin", "stateProvince": "Texas", "countryName": "United States of America", "name": "Austin"}],
         "Austin, Texas, United States of America"),
        ([{"name": "Sunnyvale", "countryName": "United States of America"}], "Sunnyvale, United States of America"),
        ([{"name": "United States", "countryName": "United States of America"}], "United States, United States of America"),
        ([], ""),
        (["junk", None], ""),
    ],
)
def test_apple_location(locations, expected):
    assert apple._location({"locations": locations}) == expected


# ------------------------------------------------------------------ per-term caps from settings

@respx.mock
def test_amazon_takes_its_cap_from_the_caller_but_stays_in_the_api_window(amazon_company, caplog):
    def page(request):
        offset = int(request.url.params["offset"])
        jobs = [{"id": f"u{offset + i}", "id_icims": str(offset + i), "title": "M", "job_path": "/x"} for i in range(100)]
        return httpx.Response(200, json={"error": None, "hits": 50000, "jobs": jobs})

    route = respx.get(AZ).mock(side_effect=page)
    with httpx.Client() as client:
        assert len(amazon.fetch(amazon_company, client, ["manager"], max_per_term=300)) == 300
        assert route.call_count == 3
        jobs = amazon.fetch(amazon_company, client, ["manager"], max_per_term=50000)
    assert len(jobs) == 9900  # offset + page must stay within Amazon's 10,000-result window
    assert max(int(c.request.url.params["offset"]) for c in route.calls) == 9800


@respx.mock
def test_eightfold_takes_its_cap_from_the_caller(eightfold_company):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))

    def page(request):
        start = int(request.url.params["start"])
        positions = [{"id": start + i + 1, "name": "M", "locations": []} for i in range(10)]
        return httpx.Response(200, json={"data": {"positions": positions, "count": 9999}})

    respx.get(EF + "/api/pcsx/search").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = eightfold.fetch(eightfold_company, client, ["manager"], wants_body=lambda j: False, max_per_term=30)
    assert len(jobs) == 30


@respx.mock
def test_oracle_takes_its_cap_from_the_caller(oracle_company):
    def page(request):
        offset = int(_finder(request)[1]["offset"])
        reqs = [{"Id": str(offset + i + 1), "Title": "M", "PrimaryLocation": "X"} for i in range(200)]
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 9999, "requisitionList": reqs}]})

    respx.get(OR + "/recruitingCEJobRequisitions").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = oracle.fetch(oracle_company, client, ["manager"], wants_body=lambda j: False, max_per_term=400)
    assert len(jobs) == 400


@respx.mock
def test_apple_takes_its_cap_from_the_caller(apple_company):
    def page(request):
        n = int(request.url.params["page"])
        rows = [{"id": f"r{n}-{i}", "postingTitle": "M", "transformedPostingTitle": "m", "locations": []} for i in range(20)]
        loader = {"search": {"searchResults": rows, "totalRecords": 9999}}
        return httpx.Response(200, text=f"<script>window.__staticRouterHydrationData = JSON.parse({json.dumps(json.dumps({'loaderData': loader}))});</script>")

    respx.get(AP + "/search").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = apple.fetch(apple_company, client, ["manager"], wants_body=lambda j: False, max_per_term=60)
    assert len(jobs) == 60


@respx.mock
def test_fetch_company_passes_the_cap_to_search_sources(amazon_company, monkeypatch):
    seen = []
    monkeypatch.setitem(
        __import__("jobhunt.sources", fromlist=["SEARCH_FETCHERS"]).SEARCH_FETCHERS,
        "amazon",
        lambda company, client, search, max_pages, wants_body, pool, max_per_term=None: seen.append(max_per_term) or [],
    )
    with httpx.Client() as client:
        fetch_company(amazon_company, client, max_per_term=123)
        fetch_company(amazon_company, client)
    assert seen == [123, None]


# source, company fixture, method, listing URL, fixture file, key holding the postings, ID field
SOURCES_WITH_IDS = [
    (greenhouse, "gh_company", "GET", "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs",
     "greenhouse_jobs.json", "jobs", "id"),
    (lever, "lever_company", "GET", "https://api.lever.co/v0/postings/examplelever",
     "lever_postings.json", None, "id"),
    (ashby, "ashby_company", "GET", "https://api.ashbyhq.com/posting-api/job-board/exampleashby",
     "ashby_board.json", "jobs", "id"),
    (workday, "workday_company", "POST", WD + "/jobs", "workday_jobs.json", "jobPostings", "externalPath"),
    (smartrecruiters, "smartrecruiters_company", "GET", SR, "smartrecruiters_postings.json", "content", "id"),
    (workable, "workable_company", "GET", WK, "workable_account.json", "jobs", "shortcode"),
    (bamboohr, "bamboohr_company", "GET", BH + "/list", "bamboohr_list.json", "result", "id"),
    (amazon, "amazon_company", "GET", AZ, "amazon_search.json", "jobs", "id"),
]


def _fetch_listing(source, company, method, url, data):
    with respx.mock:
        respx.route(method=method, url=url).mock(return_value=httpx.Response(200, json=data))
        with httpx.Client() as client:
            if source in (workday, smartrecruiters, bamboohr):
                return source.fetch(company, client, lambda job: False)
            return source.fetch(company, client)


@pytest.mark.parametrize(("source", "company_fixture", "method", "url", "payload", "key", "id_field"), SOURCES_WITH_IDS)
def test_postings_without_an_id_are_skipped(
    source, company_fixture, method, url, payload, key, id_field, fixture_json, request, caplog
):
    company = request.getfixturevalue(company_fixture)
    data = fixture_json(payload)
    before = _fetch_listing(source, company, method, url, data)

    postings = data[key] if key else data
    del postings[0][id_field]  # the first posting is a listed one in every fixture
    after = _fetch_listing(source, company, method, url, data)

    assert [j.title for j in after] == [j.title for j in before[1:]]
    assert f"{company.slug}: skipped a posting with no {id_field}" in caplog.text


def _requests_made(company, mocks):
    with respx.mock(assert_all_called=False) as router:
        for method, url, payload in mocks:
            router.route(method=method, url__startswith=url).mock(return_value=httpx.Response(200, json=payload))
        with httpx.Client() as client:
            fetch_company(company, client)
        return [call.request.url for call in router.calls]


@pytest.mark.parametrize(
    ("company_fixture", "mocks"),
    [
        ("gh_company", [("GET", GH_JOBS_URL, "greenhouse_jobs.json")]),
        ("lever_company", [("GET", "https://api.lever.co/v0/postings/examplelever", "lever_postings.json")]),
        ("ashby_company", [("GET", "https://api.ashbyhq.com/posting-api/job-board/exampleashby", "ashby_board.json")]),
        ("workday_company", [("POST", WD + "/jobs", "workday_jobs.json"), ("GET", WD + "/job/", "workday_job.json")]),
        ("smartrecruiters_company", [("GET", SR + "/", "smartrecruiters_posting.json"), ("GET", SR, "smartrecruiters_postings.json")]),
        ("workable_company", [("GET", WK, "workable_account.json")]),
        ("bamboohr_company", [("GET", BH + "/list", "bamboohr_list.json"), ("GET", BH + "/", "bamboohr_job.json")]),
        ("amazon_company", [("GET", AZ, "amazon_search.json")]),
    ],
)
def test_every_request_counts_against_its_boards_rate_group(company_fixture, mocks, request, fixture_json):
    company = request.getfixturevalue(company_fixture)
    urls = _requests_made(company, [(m, u, fixture_json(f)) for m, u, f in mocks])
    assert urls, "the adapter made no requests"
    assert {request_group(u) for u in urls} == {rate_group(company)}


def test_rate_groups():
    wd = lambda dc: Company(name="x", ats="workday", slug="x/y", datacenter=dc)  # noqa: E731
    assert rate_group(wd("wd1")) == "workday:wd1" and rate_group(wd("wd103")) == "workday:wd103"
    assert rate_group(Company(name="x", ats="lever", slug="x")) == "lever"
    assert request_group(httpx.URL("https://other.example.com/x")) == "other.example.com"
    # every BambooHR tenant has its own host, but they are one service
    assert request_group(httpx.URL("https://acme.bamboohr.com/careers/list")) == "bamboohr"
    assert request_group(httpx.URL("https://apply.workable.com/api/v1/widget/accounts/a")) == "workable"
    assert request_group(httpx.URL("https://www.amazon.jobs/en/search.json")) == "amazon"
    # an Eightfold board is its own host: rate limits seen so far are per host
    assert request_group(httpx.URL(AP + "/search")) == rate_group(Company(name="Apple", ats="apple", slug="united-states-USA")) == "apple"
    oc = Company(name="x", ats="oracle", slug="Example.fa.us2.oraclecloud.com/CX_1")
    assert rate_group(oc) == "example.fa.us2.oraclecloud.com"
    assert request_group(httpx.URL(OR + "/recruitingCEJobRequisitions")) == rate_group(oc)
    ef = Company(name="x", ats="eightfold", slug="Apply.Careers.Microsoft.com")
    assert rate_group(ef) == "apply.careers.microsoft.com"
    assert request_group(httpx.URL("https://apply.careers.microsoft.com/api/pcsx/search")) == rate_group(ef)
    assert request_group(httpx.URL("https://www.bamboohr.com.evil.example/x")) == "www.bamboohr.com.evil.example"


# ------------------------------------------------------------------ concurrent pages and details

BARRIER_WAIT = 5  # seconds; only reached if a test is broken


def _workday_pages(total, together):
    """Workday listing pages; the pages at offsets in ``together`` must be in flight at once."""
    barrier = threading.Barrier(len(together), timeout=BARRIER_WAIT) if together else None

    def page(request):
        offset = json.loads(request.content)["offset"]
        if barrier and offset in together:
            barrier.wait()  # raises BrokenBarrierError unless they overlap
        n = max(0, min(20, total - offset))
        postings = [{"title": f"Job {offset + i}", "externalPath": f"/job/X/Job_{offset + i}", "locationsText": "X"}
                    for i in range(n)]
        return httpx.Response(200, json={"total": total if offset == 0 else 0, "jobPostings": postings})

    return page


@respx.mock
def test_workday_fetches_later_pages_concurrently_and_keeps_their_order(workday_company):
    listing = respx.post(WD + "/jobs").mock(side_effect=_workday_pages(65, together={20, 40, 60}))
    with httpx.Client() as client, ThreadPoolExecutor(4) as pool:
        jobs = workday.fetch(workday_company, client, lambda job: False, pool=pool)
    assert [j.title for j in jobs] == [f"Job {i}" for i in range(65)]
    assert listing.call_count == 4


@respx.mock
def test_workday_fetches_descriptions_concurrently(workday_company, fixture_json):
    respx.post(WD + "/jobs").mock(return_value=httpx.Response(200, json=fixture_json("workday_jobs.json")))
    both = threading.Barrier(2, timeout=BARRIER_WAIT)

    def detail(request):
        both.wait()
        return httpx.Response(200, json=fixture_json("workday_job.json"))

    respx.get(url__startswith=WD + "/job/").mock(side_effect=detail)
    wanted = {"Director of Platform Engineering", "Director of Sales"}
    with httpx.Client() as client, ThreadPoolExecutor(4) as pool:
        jobs = workday.fetch(workday_company, client, lambda job: job.title in wanted, pool=pool)
    assert [bool(j.body) for j in jobs] == [True, False, True]


@respx.mock
def test_workday_page_error_fails_the_board_with_a_pool(workday_company):
    def page(request):
        offset = json.loads(request.content)["offset"]
        if offset == 20:
            return httpx.Response(500)
        return httpx.Response(200, json={"total": 45, "jobPostings": [
            {"title": "J", "externalPath": f"/job/X/J_{offset}", "locationsText": "X"}]})

    respx.post(WD + "/jobs").mock(side_effect=page)
    with httpx.Client() as client, ThreadPoolExecutor(4) as pool, pytest.raises(httpx.HTTPStatusError):
        workday.fetch(workday_company, client, lambda job: False, pool=pool)


@respx.mock
def test_smartrecruiters_fetches_later_pages_concurrently_and_keeps_their_order(smartrecruiters_company):
    barrier = threading.Barrier(2, timeout=BARRIER_WAIT)

    def page(request):
        offset = int(request.url.params["offset"])
        if offset in (100, 200):
            barrier.wait()
        n = max(0, min(100, 250 - offset))
        postings = [{"id": str(offset + i), "name": f"Job {offset + i}", "location": {}} for i in range(n)]
        return httpx.Response(200, json={"offset": offset, "limit": 100, "totalFound": 250, "content": postings})

    listing = respx.get(SR).mock(side_effect=page)
    with httpx.Client() as client, ThreadPoolExecutor(4) as pool:
        jobs = smartrecruiters.fetch(smartrecruiters_company, client, lambda job: False, pool=pool)
    assert [j.external_id for j in jobs] == [str(i) for i in range(250)]
    assert listing.call_count == 3


@respx.mock
def test_max_pages_still_limits_a_pooled_listing(workday_company):
    listing = respx.post(WD + "/jobs").mock(side_effect=_workday_pages(65, together=set()))
    with httpx.Client() as client, ThreadPoolExecutor(4) as pool:
        assert len(workday.fetch(workday_company, client, lambda job: False, max_pages=1, pool=pool)) == 20
    assert listing.call_count == 1


@respx.mock
def test_fetch_company_passes_the_pool(workday_company):
    respx.post(WD + "/jobs").mock(side_effect=_workday_pages(45, together={20, 40}))
    with httpx.Client() as client, ThreadPoolExecutor(4) as pool:
        assert len(fetch_company(workday_company, client, wants_body=lambda job: False, pool=pool)) == 45


def _pages_with_gaps(source, total, empty):
    """Listing pages for ``source``; the pages at offsets in ``empty`` come back with no postings."""
    size = 20 if source == "workday" else 100

    def page(request):
        if source == "workday":
            offset = json.loads(request.content)["offset"]
        else:
            offset = int(request.url.params["offset"])
        n = 0 if offset in empty else max(0, min(size, total - offset))
        if source == "workday":
            postings = [{"title": f"Job {offset + i}", "externalPath": f"/job/X/Job_{offset + i}", "locationsText": "X"}
                        for i in range(n)]
            return httpx.Response(200, json={"total": total if offset == 0 else 0, "jobPostings": postings})
        postings = [{"id": str(offset + i), "name": f"Job {offset + i}", "location": {}} for i in range(n)]
        return httpx.Response(200, json={"totalFound": total, "content": postings})

    return page


@pytest.mark.parametrize("pooled", [False, True], ids=["serial", "pooled"])
@pytest.mark.parametrize(
    ("source", "total", "empty", "expected"),
    [
        ("workday", 65, {0}, 0),  # an empty first page ends the listing, whatever the total says
        ("workday", 65, {20}, 20),  # so does an empty later page: nothing after it is kept
        ("smartrecruiters", 250, {0}, 0),
        ("smartrecruiters", 250, {100}, 100),
    ],
)
@respx.mock
def test_an_empty_page_ends_the_listing_in_both_paths(
    source, total, empty, expected, pooled, workday_company, smartrecruiters_company
):
    company, module, route = {
        "workday": (workday_company, workday, respx.post(WD + "/jobs")),
        "smartrecruiters": (smartrecruiters_company, smartrecruiters, respx.get(SR)),
    }[source]
    listing = route.mock(side_effect=_pages_with_gaps(source, total, empty))
    with httpx.Client() as client, ThreadPoolExecutor(4) as pool:
        jobs = module.fetch(company, client, lambda job: False, pool=pool if pooled else None)
    assert len(jobs) == expected
    if 0 in empty:
        assert listing.call_count == 1
