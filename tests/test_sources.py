"""ATS adapters: fixture-driven, network mocked with respx."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import respx

from jobhunt import throttle
from jobhunt.filter import check_location, check_title
from jobhunt.schema import Company, Job
from jobhunt.sources import (
    _sitemap,
    amazon,
    apple,
    ashby,
    bamboohr,
    eightfold,
    fetch_company,
    gem,
    greenhouse,
    icims_careers,
    lever,
    oracle,
    paradox,
    phenom,
    radancy,
    rate_group,
    request_group,
    rippling,
    smartrecruiters,
    successfactors,
    usajobs,
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


# ------------------------------------------------------------------ Phenom (search; JSON widgets API)

PH = "https://careers.example.com/widgets"
IC = "https://careers.example.com/api/jobs"  # iCIMS Career Sites
GEM = "https://api.gem.com/job_board/v0/examplegem/job_posts/"
RIP = "https://api.rippling.com/platform/api/ats/v1/board/examplerip/jobs"
UJ = "https://data.usajobs.gov/api/Search"
UJ_AUTH = ("test-key", "me@example.com")


def _phenom_routes(fixture_json, detail=None):
    """One route for both calls (Phenom has one endpoint): searches get the search fixture."""
    def answer(request):
        sent = json.loads(request.content)
        if sent["ddoKey"] == "jobDetail":
            return detail if detail is not None else httpx.Response(200, json=fixture_json("phenom_job.json"))
        return httpx.Response(200, json=fixture_json("phenom_search.json"))

    return respx.post(PH).mock(side_effect=answer)


def _sent(route, kind):
    return [json.loads(c.request.content) for c in route.calls if json.loads(c.request.content)["ddoKey"] == kind]


@respx.mock
def test_phenom_searches_each_term_and_normalizes(phenom_company, fixture_json):
    route = _phenom_routes(fixture_json)
    with httpx.Client() as client:
        jobs = phenom.fetch(phenom_company, client, ["director", "senior manager"], wants_body=lambda j: j.external_id == "R1001")
    searches = _sent(route, "refineSearch")
    assert [s["keywords"] for s in searches] == ["director", "senior manager"]
    assert all((s["lang"], s["country"], s["from"], s["size"]) == ("en_us", "us", 0, 100) for s in searches)
    assert [j.external_id for j in jobs] == ["R1001", "R1002", "R1003"]  # deduped across terms
    assert [d["jobId"] for d in _sent(route, "jobDetail")] == ["R1001"]
    first = jobs[0]
    assert (first.source, first.company, first.company_slug) == ("phenom", "Example Co", "careers.example.com/us/en")
    assert first.title == "Director, Platform Engineering"
    assert first.url == "https://careers.example.com/us/en/job/R1001"
    assert first.location == "Seattle, Washington, United States of America; Austin, Texas, United States of America"
    assert first.posted_at == "2026-09-18T00:00:00.000+0000"
    assert first.body.startswith("The Opportunity\n") and "Own reliability" in first.body
    assert first.remote is False  # hybrid, and the detail says remote: No
    assert jobs[1].remote is True and jobs[1].body == ""  # RemoteType: Remote; no description asked for
    assert jobs[2].remote is None  # blank RemoteType says nothing


@respx.mock
def test_phenom_pages_by_a_hundred_until_the_total_is_in(phenom_company):
    def page(request):
        start = json.loads(request.content)["from"]
        rows = [{"jobId": f"r{start + i}", "title": "Director"} for i in range(max(0, min(100, 250 - start)))]
        return httpx.Response(200, json={"refineSearch": {"totalHits": 250, "data": {"jobs": rows}}})

    route = respx.post(PH).mock(side_effect=page)
    with httpx.Client() as client:
        assert len(phenom.fetch(phenom_company, client, ["director"], wants_body=lambda j: False)) == 250
        assert [json.loads(c.request.content)["from"] for c in route.calls] == [0, 100, 200]
        assert len(phenom.fetch(phenom_company, client, ["director"], max_pages=1, wants_body=lambda j: False)) == 100


@respx.mock
def test_phenom_a_broad_term_stops_at_the_cap_and_says_so(phenom_company, caplog):
    def page(request):
        start = json.loads(request.content)["from"]
        rows = [{"jobId": f"r{start + i}", "title": "Manager"} for i in range(100)]
        return httpx.Response(200, json={"refineSearch": {"totalHits": 19122, "data": {"jobs": rows}}})

    route = respx.post(PH).mock(side_effect=page)
    with httpx.Client() as client:
        jobs = phenom.fetch(phenom_company, client, ["manager"], wants_body=lambda j: False, max_per_term=250)
    assert len(jobs) == 250 and route.call_count == 3
    assert "'manager' has 19122 hits; kept the first 250" in caplog.text


@respx.mock
def test_phenom_an_empty_page_ends_the_listing(phenom_company):
    pages = [
        {"refineSearch": {"totalHits": 900, "data": {"jobs": [{"jobId": "r0", "title": "Director"}, {"jobId": "r1", "title": "Director"}]}}},
        {"refineSearch": {"totalHits": 900, "data": {"jobs": []}}},
    ]
    route = respx.post(PH).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    with httpx.Client() as client:
        jobs = phenom.fetch(phenom_company, client, ["director"], wants_body=lambda j: False)
    assert [j.external_id for j in jobs] == ["r0", "r1"]
    assert route.call_count == 2


@pytest.mark.parametrize("search", [
    {"data": [1]},
    {"data": "jobs"},
    {"data": {"jobs": {"jobId": "r0"}}},
    {"data": {"jobs": []}, "totalHits": {"value": 3}},
    {"data": {"jobs": []}, "totalHits": "many"},
])
def test_phenom_a_malformed_search_answer_is_a_value_error(search):
    with pytest.raises(ValueError):
        phenom.search_results({"refineSearch": search})


@respx.mock
def test_phenom_an_answer_without_search_results_is_an_error(phenom_company):
    respx.post(PH).mock(return_value=httpx.Response(200, json={"error": "bad request"}))
    with httpx.Client() as client, pytest.raises(ValueError, match="no search results"):
        phenom.fetch(phenom_company, client, ["director"])


@respx.mock
def test_phenom_a_missing_site_is_an_http_error(phenom_company):
    respx.post(PH).mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        phenom.fetch(phenom_company, client, ["director"])


@pytest.mark.parametrize("detail", [
    httpx.Response(500),
    httpx.Response(200, json={"jobDetail": {"data": {}}}),
    httpx.Response(200, json={"jobDetail": "error"}),
    httpx.Response(200, json={"jobDetail": {"data": "x"}}),
    httpx.Response(200, json={"jobDetail": {"data": {"job": {"description": {"a": 1}}}}}),
])
@respx.mock
def test_phenom_failed_detail_keeps_job_without_body(phenom_company, fixture_json, caplog, detail):
    _phenom_routes(fixture_json, detail=detail)
    with httpx.Client() as client:
        jobs = phenom.fetch(phenom_company, client, ["director"], wants_body=lambda j: j.external_id == "R1001")
    assert len(jobs) == 3 and jobs[0].body == ""
    assert jobs[0].remote is False  # the listing's Hybrid still counts
    assert "phenom careers.example.com/us/en: no description for R1001" in caplog.text


@respx.mock
def test_phenom_warns_about_a_board_with_no_postings(phenom_company, caplog):
    respx.post(PH).mock(return_value=httpx.Response(200, json={"refineSearch": {"totalHits": 0, "data": {"jobs": []}}}))
    with httpx.Client() as client, caplog.at_level("WARNING"):
        assert phenom.fetch(phenom_company, client, ["director", "vp"]) == []
    assert caplog.messages == [
        "phenom careers.example.com/us/en: 0 postings — check the slug (host/country/language, e.g. careers.example.com/us/en)"
    ]


@respx.mock
def test_phenom_descriptions_go_through_the_pool(phenom_company, fixture_json):
    route = _phenom_routes(fixture_json)
    used = []

    class Pool(ThreadPoolExecutor):
        def map(self, *a, **kw):
            used.append(True)
            return super().map(*a, **kw)

    with httpx.Client() as client, Pool(2) as pool:
        fetch_company(phenom_company, client, pool=pool, search=["director"])
    assert used and len(_sent(route, "jobDetail")) == 3


@pytest.mark.parametrize(
    ("raw", "detail", "expected"),
    [
        ({"RemoteType": "Remote"}, None, True),
        ({"RemoteType": "Fully Remote"}, None, True),
        ({"RemoteType": "Onsite Only"}, None, False),
        ({"RemoteType": "Hybrid"}, None, False),
        ({}, {"remote": "Remote"}, True),  # DaVita puts it in the detail
        ({}, {"remote": "Yes"}, True),
        ({}, {"remote": "No"}, False),  # Adobe
        ({"RemoteType": ""}, {"remote": None}, None),
        ({"location": "Remote, United States of America"}, None, True),  # no field: the location says so
        ({}, None, None),
    ],
)
def test_phenom_remote(raw, detail, expected):
    assert phenom._remote(raw, detail) is expected


@respx.mock
def test_a_phenom_site_without_a_locale_in_its_urls(fixture_json):
    """careers.davita.com has no /us/en in its URLs; its API still takes a country and language."""
    route = _phenom_routes(fixture_json)
    company = Company(name="Example Co", ats="phenom", slug="careers.example.com")
    with httpx.Client() as client:
        jobs = phenom.fetch(company, client, ["director"], wants_body=lambda j: False)
    assert (_sent(route, "refineSearch")[0]["lang"], _sent(route, "refineSearch")[0]["country"]) == ("en_us", "us")
    assert jobs[0].url == "https://careers.example.com/job/R1001"
    assert rate_group(company) == "careers.example.com"


@pytest.mark.parametrize(
    "slug",
    ["careers.example.com/us", "careers.example.com/us/en/x", "/us/en", "a b/us/en", "careers.example.com/u s/en"],
)
def test_a_phenom_slug_is_host_country_language(slug):
    with pytest.raises(ValueError, match="host/country/language"):
        Company(name="x", ats="phenom", slug=slug)


# ------------------------------------------------------------------ SuccessFactors (sitemap; HTML job pages)

SF = "https://careers.example.com"
SF_JOB1 = SF + "/job/Seattle-Director%2C-Platform-Engineering-WA-98101/1200000100/"
SF_JOB2 = SF + "/brand_two/job/Richmond-Senior-Manager%2C-Marketing-Strategy-VA-23230/1200000200/"


def _sf_routes(sitemap="successfactors_sitemap.xml", robots=None, pages=None):
    respx.get(SF + "/robots.txt").mock(return_value=robots or httpx.Response(404))
    feed = respx.get(SF + "/sitemap.xml").mock(
        return_value=httpx.Response(200, content=(FIXTURES / sitemap).read_bytes())
    )
    pages = pages if pages is not None else {
        SF_JOB1: httpx.Response(200, text=(FIXTURES / "successfactors_job.html").read_text()),
        SF_JOB2: httpx.Response(200, text=(FIXTURES / "successfactors_job_plain.html").read_text()),
    }
    job_routes = {url: respx.get(url).mock(return_value=resp) for url, resp in pages.items()}
    return feed, job_routes


@respx.mock
def test_successfactors_rss_feed_is_every_posting_in_one_request(sf_company):
    feed, pages = _sf_routes(sitemap="successfactors_feed.xml", pages={})
    asked = []
    with httpx.Client() as client:
        jobs = successfactors.fetch(sf_company, client, lambda j: asked.append(j) or True)
    assert feed.call_count == 1 and asked == []  # the feed has every description: nothing to ask
    assert [j.external_id for j in jobs] == ["1100000100", "1100000200", "1100000300"]
    first = jobs[0]
    assert (first.source, first.company, first.company_slug) == ("successfactors", "Example Co", "careers.example.com")
    assert first.title == "Director, Platform Engineering"  # the feed's "(Seattle, WA, US, 98101)" is the location
    assert first.location == "Seattle, WA, US, 98101"
    assert first.url == SF + "/job/Seattle-Director%2C-Platform-Engineering-WA-98101/1100000100/"
    assert first.body == "Lead the platform group.\n\nOwn reliability"
    assert first.remote is None and first.posted_at is None
    assert jobs[1].title == "Senior Manager, Data" and jobs[1].remote is True  # "Remote, US"
    assert jobs[1].url == SF + "/brand_two/job/Senior-Manager%2C-Data/1100000200/"
    assert jobs[2].title == "Plant Operator"  # no location in the title to strip


@respx.mock
def test_successfactors_sitemap_fetches_only_the_pages_wanted(sf_company):
    _, pages = _sf_routes()
    seen = []

    def wants(job):
        seen.append(job.title)
        return "Director" in job.title or "Manager" in job.title

    with httpx.Client() as client:
        jobs = successfactors.fetch(sf_company, client, wants)
    # titles from the URLs (location words and all) decide which pages to fetch; /content/ isn't a job
    assert seen[:3] == ["Seattle Director, Platform Engineering WA 98101",
                        "Richmond Senior Manager, Marketing Strategy VA 23230", "Tulsa Plant Operator OK 74101"]
    assert "Plant Operator" in seen  # a URL whose words fail is tried a run of its words at a time
    assert [j.external_id for j in jobs] == ["1200000100", "1200000200", "1200000300"]
    assert pages[SF_JOB1].call_count == pages[SF_JOB2].call_count == 1
    first, second, third = jobs
    assert first.title == "Director, Platform Engineering"
    assert first.location == "Seattle, WA, US, 98101"
    assert first.posted_at == "2026-09-14T00:00:00+00:00"
    assert first.url == SF_JOB1
    assert first.body.startswith("Lead the platform group at Example Co.") and "Grow the team" in first.body
    assert "Not part of the posting" not in first.body
    # a page with no location data: the URL's words around the title
    assert second.title == "Senior Manager, Marketing Strategy"
    assert second.location == "Richmond, VA 23230"
    assert second.body.startswith("About us") and "based in Richmond, VA." in second.body
    assert second.url == SF_JOB2 and second.posted_at is None
    # not wanted: kept with what its URL says
    assert (third.title, third.body, third.url) == ("Tulsa Plant Operator OK 74101", "", SF + "/job/Tulsa-Plant-Operator-OK-74101/1200000300/")


@pytest.mark.parametrize("page", [httpx.Response(500), httpx.Response(200, text="<html>no posting here</html>")])
@respx.mock
def test_successfactors_failed_page_keeps_the_posting_from_its_url(sf_company, caplog, page):
    _sf_routes(pages={SF_JOB1: page, SF_JOB2: page})
    with httpx.Client() as client:
        jobs = successfactors.fetch(sf_company, client, lambda j: "Director" in j.title)
    assert jobs[0].title == "Seattle Director, Platform Engineering WA 98101" and jobs[0].body == ""
    assert "successfactors careers.example.com: no description for 1200000100" in caplog.text


def _sf_one(path, page):
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(404))
    sitemap = f'<urlset xmlns="http://www.google.com/schemas/sitemap/0.9"><url><loc>{SF}{path}</loc></url></urlset>'
    respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap))
    return respx.get(SF + path).mock(return_value=httpx.Response(200, text=page))


@pytest.mark.parametrize(
    ("path", "title"),
    [
        # Career Site Builder writes "." as "_" (careers.hubbell.com/job/Shelton-Sr_-HR-Manager-CT-06484-4300/...)
        ("/job/Seattle-Sr_-Manager%2C-Platform-Engineering-WA-98101/1/", "Sr. Manager, Platform Engineering"),
        # and drops "/" (Manager/Director -> ManagerDirector)
        ("/job/Seattle-Senior-ManagerDirector%2C-Platform-Engineering-WA-98101/4/", "Senior Manager/Director, Platform Engineering"),
        # a dropped "/" between capitals (careers.hubbell.com writes WDK/Killark as WDKKillark)
        ("/job/Seattle-VPDirector%2C-Platform-Engineering-WA-98101/5/", "VP/Director, Platform Engineering"),
        ("/job/Seattle-AVPDirector%2C-Platform-WA-98101/6/", "AVP/Director, Platform"),
        ("/job/Seattle-SVPGM%2C-Platform-WA-98101/7/", "SVP/GM, Platform"),
        # place words that are excluded terms: Puerto Rico's "PR", Commerce, CA
        ("/job/San-Juan-Director%2C-Platform-Engineering-PR-00901/2/", "Director, Platform Engineering"),
        ("/job/Commerce-Director%2C-Platform-Engineering-CA-90040/3/", "Director, Platform Engineering"),
    ],
)
@respx.mock
def test_successfactors_fetches_every_page_whose_title_could_pass(sf_company, prefs, path, title):
    assert check_title(Job(source="successfactors", company="x", company_slug="x", external_id="1",
                           title=title, url=SF), prefs) is None  # the real title passes
    page = _sf_one(path, (FIXTURES / "successfactors_job.html").read_text())
    with httpx.Client() as client:
        [job] = successfactors.fetch(sf_company, client, lambda j: check_title(j, prefs) is None)  # cli's check
    assert page.call_count == 1 and job.title == "Director, Platform Engineering"


@pytest.mark.parametrize(
    "robots", [httpx.Response(200, text="User-agent: *\nDisallow: /job/\n"), None])  # None: the pages fail
@respx.mock
def test_successfactors_a_wanted_posting_without_its_page_keeps_the_reading_that_passed(sf_company, prefs, robots):
    paths = {"1": "/job/Seattle-Sr_-Manager%2C-Platform-Engineering-WA-98101/1/",
             "2": "/job/Seattle-VPDirector%2C-Platform-Engineering-WA-98101/2/",
             "3": "/job/Seattle-Plant-Operator-WA-98101/3/"}
    respx.get(SF + "/robots.txt").mock(return_value=robots or httpx.Response(404))
    locs = "".join(f"<url><loc>{SF}{p}</loc></url>" for p in paths.values())
    sitemap = f'<urlset xmlns="http://www.google.com/schemas/sitemap/0.9">{locs}</urlset>'
    respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap))
    for path in paths.values():
        respx.get(SF + path).mock(return_value=httpx.Response(500))
    with httpx.Client() as client:
        jobs = successfactors.fetch(sf_company, client, lambda j: check_title(j, prefs) is None)  # cli's check
    assert [(j.external_id, j.title) for j in jobs] == [
        ("1", "Seattle Sr. Manager, Platform Engineering WA 98101"),
        ("2", "Seattle VP Director, Platform Engineering WA 98101"),
        ("3", "Seattle Plant Operator WA 98101"),  # not wanted: what its URL says
    ]
    assert [check_title(j, prefs) is None for j in jobs] == [True, True, False]  # cli's final filter agrees
    assert all(j.body == "" for j in jobs)


@pytest.mark.parametrize(
    ("path", "location"),
    [
        # the URL drops the title's " - ": its words still match the title, token by token
        ("/job/Seattle-Senior-Manager-Platform-Engineering-WA-98101/9/", "Seattle, WA 98101"),
        # the title isn't in the URL at all: the URL's words, so the location filter still sees the place
        ("/job/Seattle-Head-of-Platform-WA-98101/9/", "Seattle Head of Platform WA 98101"),
        # the URL is just the title: no place
        ("/job/Senior-Manager-Platform-Engineering/9/", ""),
        # "_" is how the URL writes "." (careers.hubbell.com/job/St_-Louis-..., ...-D_C_-...)
        ("/job/St_-Louis-Senior-Manager-Platform-Engineering-MO-63101/9/", "St. Louis, MO 63101"),
        ("/job/Washington-Senior-Manager-Platform-Engineering-D_C_-20001/9/", "Washington, D.C. 20001"),
        ("/job/Remote-Senior-Manager-Platform-Engineering-U_S_/9/", "Remote, U.S."),
        ("/job/St_-Louis-Head-of-Platform-MO-63101/9/", "St. Louis Head of Platform MO 63101"),
    ],
)
@respx.mock
def test_successfactors_place_from_a_url_whose_title_differs(sf_company, path, location):
    page = (FIXTURES / "successfactors_job_plain.html").read_text().replace(
        "Senior Manager, Marketing Strategy\n", "Senior Manager - Platform Engineering\n")
    _sf_one(path, page)
    with httpx.Client() as client:
        [job] = successfactors.fetch(sf_company, client)
    assert job.title == "Senior Manager - Platform Engineering" and job.location == location


@respx.mock
def test_successfactors_robots_txt_can_rule_out_the_job_pages(sf_company, caplog):
    _, pages = _sf_routes(robots=httpx.Response(200, text="User-agent: *\nDisallow: /job/\nDisallow: /brand_two/job/\n"))
    with httpx.Client() as client:
        jobs = successfactors.fetch(sf_company, client)
    assert len(jobs) == 3 and all(j.body == "" for j in jobs)
    assert all(route.call_count == 0 for route in pages.values())
    assert caplog.text.count("robots.txt disallows") == 1


@pytest.mark.parametrize("robots", [httpx.Response(200, text="User-agent: *\nDisallow: /sitemap.xml\n"), httpx.Response(503)])
@respx.mock
def test_successfactors_robots_txt_can_rule_out_the_sitemap(sf_company, caplog, robots):
    feed, _ = _sf_routes(robots=robots)
    with httpx.Client() as client:
        assert successfactors.fetch(sf_company, client) == []
    assert feed.call_count == 0  # RFC 9309: a robots.txt that answers 5xx disallows everything
    assert "successfactors careers.example.com: robots.txt disallows /sitemap.xml" in caplog.text


@respx.mock
def test_successfactors_follows_a_robots_txt_redirect(sf_company):
    """RFC 9309: robots.txt redirects are followed (read unfollowed, a redirect allows everything)."""
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(301, headers={"location": SF + "/en/robots.txt"}))
    respx.get(SF + "/en/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /sitemap.xml\n"))
    feed = respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(200, content=b"<urlset/>"))
    with httpx.Client() as client:
        assert successfactors.fetch(sf_company, client) == []
    assert feed.call_count == 0


@respx.mock
def test_successfactors_follows_a_job_page_redirect_on_the_same_host(sf_company):
    """/job/.../1430811000/ redirects to /job/.../3365-en_US/ on the same host."""
    moved = SF + "/job/Seattle-Director/3365-en_US/"
    _sf_routes(pages={SF_JOB1: httpx.Response(301, headers={"location": moved}), SF_JOB2: httpx.Response(404)})
    respx.get(SF + "/job/Tulsa-Plant-Operator-OK-74101/1200000300/").mock(return_value=httpx.Response(404))
    respx.get(moved).mock(return_value=httpx.Response(200, text=(FIXTURES / "successfactors_job.html").read_text()))
    with httpx.Client(follow_redirects=True) as client:
        jobs = successfactors.fetch(sf_company, client)
    assert jobs[0].url == SF_JOB1 and jobs[0].body


@respx.mock
def test_successfactors_job_urls_on_another_host_are_ignored(sf_company):
    off = "https://elsewhere.example.net/job/Seattle-Director/1200000900/"
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(SF + "/sitemap.xml").mock(return_value=_urlset(off, SF_JOB1))
    elsewhere = respx.get(off).mock(return_value=httpx.Response(200, text=(FIXTURES / "successfactors_job.html").read_text()))
    respx.get(SF_JOB1).mock(return_value=httpx.Response(200, text=(FIXTURES / "successfactors_job.html").read_text()))
    with httpx.Client(follow_redirects=True) as client:
        jobs = successfactors.fetch(sf_company, client)
    assert [j.url for j in jobs] == [SF_JOB1]
    assert elsewhere.call_count == 0


@respx.mock
def test_successfactors_a_sitemap_redirect_to_another_host_is_an_error(sf_company):
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(301, headers={"location": "https://elsewhere.example.net/sitemap.xml"}))
    elsewhere = respx.get("https://elsewhere.example.net/sitemap.xml").mock(return_value=_urlset(SF_JOB1))
    with httpx.Client(follow_redirects=True) as client, pytest.raises(ValueError, match="redirects to another host"):
        successfactors.fetch(sf_company, client)
    assert elsewhere.call_count == 0


@respx.mock
def test_successfactors_a_posting_twice_in_the_feed_comes_once(sf_company):
    feed = (FIXTURES / "successfactors_feed.xml").read_text()
    first_item = feed[feed.index("<item>"):feed.index("</item>") + len("</item>")]
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(200, text=feed.replace("</channel>", first_item + "</channel>")))
    with httpx.Client() as client:
        jobs = successfactors.fetch(sf_company, client)
    assert [j.external_id for j in jobs] == ["1100000100", "1100000200", "1100000300"]


@respx.mock
def test_successfactors_page_without_a_title_itemprop_uses_og_title(sf_company):
    """jobs.amwater.com's newer template marks up the description but not the title."""
    plain = (FIXTURES / "successfactors_job_plain.html").read_text()
    page = plain.replace('itemprop="title" ', "").replace(
        "</head>", '<meta property="og:title" content="Senior Manager, Marketing &amp; Strategy" />\n</head>')
    _sf_routes(pages={SF_JOB1: httpx.Response(200, text=page), SF_JOB2: httpx.Response(200, text=page)})
    with httpx.Client() as client:
        jobs = successfactors.fetch(sf_company, client, lambda j: "Director" in j.title)
    assert jobs[0].title == "Senior Manager, Marketing & Strategy"
    assert jobs[0].body.startswith("About us")


@respx.mock
def test_successfactors_missing_sitemap_is_an_http_error(sf_company):
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        successfactors.fetch(sf_company, client)


@pytest.mark.parametrize("content", [b"<html>not a sitemap</html", b"<sitemapindex><sitemap/></sitemapindex>"])
@respx.mock
def test_successfactors_an_answer_that_isnt_a_sitemap_is_an_error(sf_company, content):
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(200, content=content))
    with httpx.Client() as client, pytest.raises(ValueError, match="sitemap"):
        successfactors.fetch(sf_company, client)


@respx.mock
def test_successfactors_warns_about_a_site_with_no_job_links(sf_company, caplog):
    """A careers site that isn't Career Site Builder (careers.netapp.com) lists other URLs."""
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(404))
    other = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://careers.example.com/job/cork/software-engineer/27600/101648738688</loc></url></urlset>'
    respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(200, content=other))
    with httpx.Client() as client, caplog.at_level("WARNING"):
        assert successfactors.fetch(sf_company, client) == []
    assert caplog.messages == ["successfactors careers.example.com: 0 postings — is it a Career Site Builder site?"]


@respx.mock
def test_successfactors_reads_a_sitemap_in_the_standard_namespace_too(sf_company):
    respx.get(SF + "/robots.txt").mock(return_value=httpx.Response(404))
    standard = (FIXTURES / "successfactors_sitemap.xml").read_bytes().replace(
        b"http://www.google.com/schemas/sitemap/0.9", b"http://www.sitemaps.org/schemas/sitemap/0.9"
    )
    respx.get(SF + "/sitemap.xml").mock(return_value=httpx.Response(200, content=standard))
    with httpx.Client() as client:
        assert len(successfactors.fetch(sf_company, client, lambda j: False)) == 3


@respx.mock
def test_successfactors_pages_go_through_the_pool(sf_company):
    _, pages = _sf_routes()
    used = []

    class Pool(ThreadPoolExecutor):
        def map(self, *a, **kw):
            used.append(True)
            return super().map(*a, **kw)

    respx.get(SF + "/job/Tulsa-Plant-Operator-OK-74101/1200000300/").mock(return_value=httpx.Response(404))
    with httpx.Client() as client, Pool(2) as pool:
        fetch_company(sf_company, client, pool=pool)
    assert used and pages[SF_JOB1].call_count == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Mon Sep 14 00:00:00 UTC 2026", "2026-09-14T00:00:00+00:00"), ("Wed Sep 09 07:00:00 UTC 2026", "2026-09-09T07:00:00+00:00"),
     ("", None), ("14/09/2026", None)],
)
def test_successfactors_posted_date(text, expected):
    assert successfactors._posted(text) == expected


@pytest.mark.parametrize("slug", ["careers.example.com/x", "careers", "a b.com", "https://careers.example.com"])
def test_a_successfactors_slug_is_a_host(slug):
    with pytest.raises(ValueError, match="successfactors slug is a careers site host"):
        Company(name="x", ats="successfactors", slug=slug)


# ------------------------------------------------------------------ Radancy and Paradox (sitemaps; JSON-LD job pages)

RD = "https://careers.example.com"
RD_JOB1 = RD + "/job/san-francisco/director-platform-engineering/45831/101650226640"
RD_JOB2 = RD + "/en/job/remote/senior-manager-data/45831/101650226656"
PX = "https://jobs.example.com"


def _radancy_routes(robots=None, page=None):
    respx.get(RD + "/robots.txt").mock(return_value=robots or httpx.Response(404))
    sitemap = respx.get(RD + "/sitemap.xml").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "radancy_sitemap.xml").read_bytes())
    )
    page = page or httpx.Response(200, text=(FIXTURES / "radancy_job.html").read_text())
    pages = respx.get(url__startswith=RD + "/").mock(return_value=page)
    return sitemap, pages


@respx.mock
def test_radancy_reads_the_sitemap_and_fetches_only_the_pages_wanted(radancy_company):
    _, pages = _radancy_routes()
    with httpx.Client() as client:
        jobs = radancy.fetch(radancy_company, client, lambda j: "director" in j.title.lower())
    # the Spanish copy of 101650226640 is the same posting; non-job URLs are skipped
    assert [j.external_id for j in jobs] == ["101650226640", "101650226656", "101650226672"]
    fetched = [str(c.request.url) for c in pages.calls if "/job/" in str(c.request.url)]
    assert fetched == [RD_JOB1]
    first, second, third = jobs
    assert (first.source, first.company, first.company_slug) == ("radancy", "Example Co", "careers.example.com")
    assert first.title == "Director, Platform Engineering" and first.url == RD_JOB1
    assert first.location == "San Francisco, California, United States of America; Seattle, United States of America"
    assert first.posted_at == "2026-10-07"  # the site writes 2026-10-7
    assert first.body.startswith("About this role") and "Own reliability" in first.body
    assert first.remote is None
    # not wanted: what the URL says (city and title words); "remote" in it is a hint
    assert (second.title, second.body, second.url, second.remote) == ("remote senior manager data", "", RD_JOB2, True)
    assert third.title == "tulsa plant operator"


@respx.mock
def test_radancy_page_without_a_job_posting_keeps_the_url_words(radancy_company, caplog):
    _radancy_routes(page=httpx.Response(200, text="<html><h1>Maintenance</h1></html>"))
    with httpx.Client() as client:
        jobs = radancy.fetch(radancy_company, client, lambda j: "director" in j.title.lower())
    assert jobs[0].title == "san francisco director platform engineering" and jobs[0].body == ""
    assert "radancy careers.example.com: no description for 101650226640 (no JobPosting on the page)" in caplog.text


@respx.mock
def test_radancy_robots_txt_can_rule_out_the_job_pages(radancy_company, caplog):
    _, pages = _radancy_routes(robots=httpx.Response(200, text="User-agent: *\nDisallow: /job/\nDisallow: /en/job/\n"))
    with httpx.Client() as client:
        jobs = radancy.fetch(radancy_company, client)
    assert len(jobs) == 3 and all(j.body == "" for j in jobs)
    assert not [c for c in pages.calls if "/job/" in str(c.request.url)]
    assert "robots.txt disallows 3 job pages" in caplog.text


@respx.mock
def test_radancy_missing_sitemap_is_an_http_error(radancy_company):
    respx.get(RD + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(RD + "/sitemap.xml").mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        radancy.fetch(radancy_company, client)


@respx.mock
def test_radancy_a_disallowed_sitemap_is_an_error(radancy_company):
    respx.get(RD + "/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /sitemap.xml\n"))
    sitemap = respx.get(RD + "/sitemap.xml").mock(return_value=httpx.Response(200, content=b"<urlset/>"))
    with httpx.Client() as client, pytest.raises(ValueError, match="robots.txt disallows it"):
        radancy.fetch(radancy_company, client)
    assert sitemap.call_count == 0


@respx.mock
def test_radancy_follows_a_sitemap_redirect_on_the_same_host(radancy_company):
    """careers.l3harris.com/sitemap.xml redirects to /en/sitemap.xml."""
    respx.get(RD + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(RD + "/sitemap.xml").mock(return_value=httpx.Response(301, headers={"location": RD + "/en/sitemap.xml"}))
    respx.get(RD + "/en/sitemap.xml").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "radancy_sitemap.xml").read_bytes()))
    with httpx.Client() as client:
        assert len(radancy.fetch(radancy_company, client, lambda j: False)) == 3


@respx.mock
def test_radancy_a_sitemap_redirect_to_another_host_is_an_error(radancy_company):
    respx.get(RD + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(RD + "/sitemap.xml").mock(return_value=httpx.Response(301, headers={"location": "https://elsewhere.example.net/sitemap.xml"}))
    elsewhere = respx.get("https://elsewhere.example.net/sitemap.xml").mock(return_value=httpx.Response(200, content=b"<urlset/>"))
    with httpx.Client() as client, pytest.raises(ValueError, match="redirects to another host"):
        radancy.fetch(radancy_company, client)
    assert elsewhere.call_count == 0


def _urlset(*urls):
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return httpx.Response(200, text=f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>')


@pytest.mark.parametrize(
    ("robots", "target", "error"),
    [
        (httpx.Response(404), "https://elsewhere.example.net/sitemap.xml", "redirects to another host"),
        (httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"), RD + "/private/sitemap.xml", "robots.txt disallows it"),
    ],
)
@respx.mock
def test_sitemap_redirects_are_checked_even_by_a_client_that_follows_them(radancy_company, robots, target, error):
    """cli._client follows redirects; the sitemap GET must still check each hop itself."""
    respx.get(RD + "/robots.txt").mock(return_value=robots)
    respx.get(RD + "/sitemap.xml").mock(return_value=httpx.Response(301, headers={"location": target}))
    moved = respx.get(target).mock(return_value=_urlset(RD_JOB1))
    with httpx.Client(follow_redirects=True) as client, pytest.raises(ValueError, match=error):
        radancy.fetch(radancy_company, client, lambda j: False)
    assert moved.call_count == 0


@respx.mock
def test_a_same_host_sitemap_redirect_is_followed_by_a_client_that_follows_them(radancy_company):
    respx.get(RD + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(RD + "/sitemap.xml").mock(return_value=httpx.Response(301, headers={"location": RD + "/en/sitemap.xml"}))
    respx.get(RD + "/en/sitemap.xml").mock(return_value=_urlset(RD_JOB1))
    with httpx.Client(follow_redirects=True) as client:
        assert len(radancy.fetch(radancy_company, client, lambda j: False)) == 1


@respx.mock
def test_job_urls_on_another_host_are_ignored(radancy_company):
    respx.get(RD + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(RD + "/sitemap.xml").mock(return_value=_urlset(
        "https://elsewhere.example.net/job/x/director/1/2", RD_JOB1))
    elsewhere = respx.get(url__startswith="https://elsewhere.example.net/").mock(return_value=httpx.Response(200))
    respx.get(RD_JOB1).mock(return_value=httpx.Response(200, text=(FIXTURES / "radancy_job.html").read_text()))
    with httpx.Client(follow_redirects=True) as client:
        jobs = radancy.fetch(radancy_company, client)
    assert [j.url for j in jobs] == [RD_JOB1]
    assert elsewhere.call_count == 0


@pytest.mark.parametrize(
    ("robots", "target", "error"),
    [
        (httpx.Response(404), "https://elsewhere.example.net/job/1", "it redirects to another host"),
        (httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"), RD + "/private/job/1", "robots.txt disallows it"),
    ],
)
@respx.mock
def test_a_job_page_redirect_off_the_host_or_into_robots_txt_is_not_followed(radancy_company, caplog, robots, target, error):
    respx.get(RD + "/robots.txt").mock(return_value=robots)
    respx.get(RD + "/sitemap.xml").mock(return_value=_urlset(RD_JOB1))
    respx.get(RD_JOB1).mock(return_value=httpx.Response(302, headers={"location": target}))
    moved = respx.get(target).mock(return_value=httpx.Response(200, text=(FIXTURES / "radancy_job.html").read_text()))
    with httpx.Client(follow_redirects=True) as client:
        [job] = radancy.fetch(radancy_company, client)
    assert moved.call_count == 0
    assert job.url == RD_JOB1 and job.body == ""
    assert f"radancy careers.example.com: no description for 101650226640 ({error}" in caplog.text


@respx.mock
def test_radancy_keeps_the_english_copy_whatever_its_region(radancy_company):
    fr, en = RD + "/fr-ca/job/montreal/directeur-plateforme/1/99", RD + "/en-ca/job/montreal/director-platform/1/99"
    respx.get(RD + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(RD + "/sitemap.xml").mock(return_value=_urlset(fr, en))
    pages = respx.get(url__startswith=RD + "/en-ca/").mock(return_value=httpx.Response(200, text=(FIXTURES / "radancy_job.html").read_text()))
    with httpx.Client() as client:
        [job] = radancy.fetch(radancy_company, client, lambda j: "director" in j.title.lower())
    assert job.url == en and job.title == "Director, Platform Engineering"
    assert pages.call_count == 1


@respx.mock
def test_radancy_warns_about_a_sitemap_with_no_job_urls(radancy_company, caplog):
    respx.get(RD + "/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(RD + "/sitemap.xml").mock(return_value=httpx.Response(
        200, content=b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://careers.example.com/</loc></url></urlset>'))
    with httpx.Client() as client, caplog.at_level("WARNING"):
        assert radancy.fetch(radancy_company, client) == []
    assert caplog.messages == ["radancy careers.example.com: 0 postings — no job URLs in its sitemaps"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (RD_JOB1, ("san francisco director platform engineering", "101650226640", 0)),
        (RD + "/en/job/huntsville/senior-associate/4832/101657038528", ("huntsville senior associate", "101657038528", 0)),
        (RD + "/es/job/madrid/director/4832/1", ("madrid director", "1", 1)),
        (RD + "/en-ca/job/toronto/director/4832/2", ("toronto director", "2", 0)),
        (RD + "/en-GB/job/london/director/4832/3", ("london director", "3", 0)),
        (RD + "/fr-ca/job/montreal/directeur/4832/2", ("montreal directeur", "2", 1)),
        (RD + "/job/new-york/sr%2C-manager/4832/7", ("new york sr, manager", "7", 0)),
        (RD + "/business/custom_fields.facility/45831/x", None),
        (RD + "/job/new-york/director/4832", None),
    ],
)
def test_radancy_job_url(url, expected):
    assert radancy.job_url(url) == expected


def _paradox_routes():
    robots = "User-agent: *\nAllow: /\nSitemap: https://jobs.example.com/sitemap_index.xml\n"
    respx.get(PX + "/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    for path, fixture in (("/sitemap_index.xml", "paradox_sitemap_index.xml"),
                          ("/en/jobs/sitemap.xml", "paradox_sitemap_jobs.xml"),
                          ("/sitemap-pages.xml", "paradox_sitemap_pages.xml")):
        respx.get(PX + path).mock(return_value=httpx.Response(200, content=(FIXTURES / fixture).read_bytes()))
    return respx.get(url__regex=PX + r"/(en/jobs/\d|courier)").mock(
        return_value=httpx.Response(200, text=(FIXTURES / "paradox_job.html").read_text())
    )


@respx.mock
def test_paradox_follows_robots_sitemaps_and_index_files(paradox_company):
    pages = _paradox_routes()
    with httpx.Client() as client:
        jobs = paradox.fetch(paradox_company, client, lambda j: "manager" in j.title.lower())
    # the English URL of 277916 wins over the Spanish one; the other host's sitemap isn't read
    assert [(j.external_id, j.url) for j in jobs] == [
        ("277916", PX + "/en/jobs/277916/district-sales-manager-enterprise/"),
        ("jr-202607049", PX + "/en/jobs/jr-202607049/senior-ai-ml-engineer/"),
        ("P25-359042-1", PX + "/courier-2/job/P25-359042-1"),  # FedEx's shape
    ]
    assert pages.call_count == 1
    first = jobs[0]
    assert first.source == "paradox"
    assert first.title == "District Sales Manager | Enterprise"  # from the escaped ld+json block
    assert first.body == "Example Co is hiring a Sales Manager."
    assert first.location == "Roseland, NJ, US"
    assert first.remote is True  # jobLocationType TELECOMMUTE
    assert first.posted_at == "2026-06-29"
    assert (jobs[1].title, jobs[2].title) == ("senior ai ml engineer", "courier 2")


@respx.mock
def test_paradox_a_later_sitemap_that_fails_is_skipped(paradox_company, caplog):
    _paradox_routes()
    respx.get(PX + "/sitemap-pages.xml").mock(return_value=httpx.Response(500))
    with httpx.Client() as client:
        jobs = paradox.fetch(paradox_company, client, lambda j: False)
    assert len(jobs) == 3
    assert "paradox jobs.example.com: skipped sitemap https://jobs.example.com/sitemap-pages.xml" in caplog.text


@respx.mock
def test_one_dead_sitemap_of_several_robots_txt_names_is_skipped(paradox_company, caplog):
    robots = f"User-agent: *\nSitemap: {PX}/old-sitemap.xml\nSitemap: {PX}/sitemap_index.xml\n"
    respx.get(PX + "/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    respx.get(PX + "/old-sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get(PX + "/sitemap_index.xml").mock(return_value=_urlset(PX + "/en/jobs/1/director/"))
    with httpx.Client() as client:
        jobs = paradox.fetch(paradox_company, client, lambda j: False)
    assert [j.external_id for j in jobs] == ["1"]
    assert "paradox jobs.example.com: skipped sitemap https://jobs.example.com/old-sitemap.xml" in caplog.text


@respx.mock
def test_a_site_whose_named_sitemaps_all_fail_is_gone(paradox_company):
    robots = f"User-agent: *\nSitemap: {PX}/a.xml\nSitemap: {PX}/b.xml\n"
    respx.get(PX + "/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    respx.get(PX + "/a.xml").mock(return_value=httpx.Response(404))
    respx.get(PX + "/b.xml").mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError) as e:
        paradox.fetch(paradox_company, client, lambda j: False)
    assert e.value.response.status_code == 404  # the first error, so the board counts as gone


@respx.mock
def test_sitemaps_robots_txt_names_on_another_host_are_not_read(paradox_company):
    robots = f"User-agent: *\nSitemap: https://elsewhere.example.net/sitemap.xml\nSitemap: {PX}/jobs.xml\n"
    respx.get(PX + "/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    elsewhere = respx.get("https://elsewhere.example.net/sitemap.xml").mock(return_value=_urlset(PX + "/en/jobs/2/x/"))
    respx.get(PX + "/jobs.xml").mock(return_value=_urlset(PX + "/en/jobs/1/director/"))
    with httpx.Client() as client:
        jobs = paradox.fetch(paradox_company, client, lambda j: False)
    assert [j.external_id for j in jobs] == ["1"]
    assert elsewhere.call_count == 0


@respx.mock
def test_only_sitemaps_on_another_host_named_falls_back_to_sitemap_xml(paradox_company):
    robots = "User-agent: *\nSitemap: https://elsewhere.example.net/sitemap.xml\n"
    respx.get(PX + "/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    elsewhere = respx.get("https://elsewhere.example.net/sitemap.xml").mock(return_value=_urlset(PX + "/en/jobs/2/x/"))
    respx.get(PX + "/sitemap.xml").mock(return_value=_urlset(PX + "/en/jobs/1/director/"))
    with httpx.Client() as client:
        jobs = paradox.fetch(paradox_company, client, lambda j: False)
    assert [j.external_id for j in jobs] == ["1"]
    assert elsewhere.call_count == 0


@respx.mock
def test_sitemaps_stop_at_the_limit(paradox_company, caplog, monkeypatch):
    monkeypatch.setattr(_sitemap, "MAX_SITEMAPS", 2)
    _paradox_routes()
    with httpx.Client() as client:
        jobs = paradox.fetch(paradox_company, client, lambda j: False)
    assert len(jobs) == 3  # the index and the jobs sitemap
    assert "paradox jobs.example.com: read 2 sitemaps; skipped 1 more" in caplog.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PX + "/en/jobs/277916/district-sales-manager/", ("district sales manager", "277916", 0)),
        (PX + "/jobs/r-1101033/ai-workflow-engineer/", ("ai workflow engineer", "r-1101033", 0)),
        (PX + "/fr-ca/emplois/jr-1/directeur/", ("directeur", "jr-1", 1)),
        (PX + "/en-ca/jobs/jr-1/director/", ("director", "jr-1", 0)),
        (PX + "/en-gb/jobs/jr-1/director/", ("director", "jr-1", 0)),
        (PX + "/courier-dot-1/job/P25-328782-6", ("courier dot 1", "P25-328782-6", 0)),
        (PX + "/jobs/saved-jobs/", None),
        (PX + "/jobs/apply-workday/completed/", None),
        (PX + "/en/jobs/", None),
        (PX + "/blog/our-culture/", None),
    ],
)
def test_paradox_job_url(url, expected):
    assert paradox.job_url(url) == expected


@respx.mock
def test_job_pages_go_through_the_pool(radancy_company):
    _, pages = _radancy_routes()
    used = []

    class Pool(ThreadPoolExecutor):
        def map(self, *a, **kw):
            used.append(True)
            return super().map(*a, **kw)

    with httpx.Client() as client, Pool(2) as pool:
        fetch_company(radancy_company, client, pool=pool)
    assert used and len([c for c in pages.calls if "/job/" in str(c.request.url)]) == 3


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-10-7", "2026-10-07"), ("2026-06-29", "2026-06-29"), ("2026-10-06T07:00:00+00:00", "2026-10-06T07:00:00+00:00"),
     ("2026-13-40", None), ("", None), (None, None), (20261007, None)],
)
def test_json_ld_date(value, expected):
    assert _sitemap.ld_date(value) == expected


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ({"@type": "Place", "address": "Remote - US"}, "Remote - US"),
        ([{"address": {"addressLocality": "Austin", "addressRegion": "TX", "addressCountry": "US"}}, {"address": {"addressLocality": "Austin", "addressRegion": "TX", "addressCountry": "US"}}], "Austin, TX, US"),
        ({"address": {"addressLocality": "", "addressCountry": {"name": "Canada"}}}, "Canada"),
        (None, ""),
        (["junk", 3], ""),
        # GM: each address a list of PostalAddress
        ([{"address": [{"addressLocality": "Remote", "addressRegion": "Washington", "addressCountry": "United States of America"}],
           "name": "Remote United States of America"},
          {"address": [{"addressLocality": "Sunnyvale", "addressRegion": "California", "addressCountry": "United States of America"}]}],
         "Remote, Washington, United States of America; Sunnyvale, California, United States of America"),
        ({"name": "Dearborn, MI", "address": {}}, "Dearborn, MI"),  # no address parts: the place's name
    ],
)
def test_json_ld_location(location, expected):
    assert _sitemap.ld_location({"jobLocation": location}) == expected


@pytest.mark.parametrize(
    "page",
    [
        '<script type="application/ld+json">[{"@type": "Organization"}, {"@type": "JobPosting", "title": "A"}]</script>',
        '<script type="application/ld+json">{"@graph": [{"@type": ["JobPosting"], "title": "A"}]}</script>',
        '<script type="application/ld+json">{bad json</script><script type="application/ld+json">{"@type": "JobPosting", "title": "A"}</script>',
    ],
)
def test_json_ld_job_postings_are_found_in_lists_graphs_and_past_bad_blocks(page):
    assert [p["title"] for p in _sitemap.job_postings(page)] == ["A"]


@pytest.mark.parametrize("ats", ["radancy", "paradox"])
@pytest.mark.parametrize("slug", ["careers.example.com/x", "careers", "https://careers.example.com"])
def test_a_sitemap_source_slug_is_a_host(ats, slug):
    with pytest.raises(ValueError, match=f"{ats} slug is a careers site host"):
        Company(name="x", ats=ats, slug=slug)


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
        caplog.clear()
        assert len(amazon.fetch(amazon_company, client, ["manager"], max_per_term=1)) == 1  # not a whole page
        assert "kept the first 1 " in caplog.text
        jobs = amazon.fetch(amazon_company, client, ["manager"], max_per_term=50000)
    assert len(jobs) == 9900  # offset + page must stay within Amazon's 10,000-result window
    assert max(int(c.request.url.params["offset"]) for c in route.calls) == 9800


@respx.mock
def test_eightfold_takes_its_cap_from_the_caller(eightfold_company, caplog):
    respx.get(EF + "/careers").mock(return_value=httpx.Response(200, text='"domain": "example.com"'))

    def page(request):
        start = int(request.url.params["start"])
        positions = [{"id": start + i + 1, "name": "M", "locations": []} for i in range(10)]
        return httpx.Response(200, json={"data": {"positions": positions, "count": 9999}})

    respx.get(EF + "/api/pcsx/search").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = eightfold.fetch(eightfold_company, client, ["manager"], wants_body=lambda j: False, max_per_term=30)
        assert len(jobs) == 30
        caplog.clear()
        jobs = eightfold.fetch(eightfold_company, client, ["manager"], wants_body=lambda j: False, max_per_term=25)
    assert len(jobs) == 25  # part of the last page, not all of it
    assert "kept the first 25" in caplog.text


@respx.mock
def test_oracle_takes_its_cap_from_the_caller(oracle_company, caplog):
    def page(request):
        offset = int(_finder(request)[1]["offset"])
        reqs = [{"Id": str(offset + i + 1), "Title": "M", "PrimaryLocation": "X"} for i in range(200)]
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 9999, "requisitionList": reqs}]})

    respx.get(OR + "/recruitingCEJobRequisitions").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = oracle.fetch(oracle_company, client, ["manager"], wants_body=lambda j: False, max_per_term=400)
        assert len(jobs) == 400
        caplog.clear()
        jobs = oracle.fetch(oracle_company, client, ["manager"], wants_body=lambda j: False, max_per_term=250)
    assert len(jobs) == 250  # part of the last page, not all of it
    assert "kept the first 250" in caplog.text


@respx.mock
def test_apple_takes_its_cap_from_the_caller(apple_company, caplog):
    def page(request):
        n = int(request.url.params["page"])
        rows = [{"id": f"r{n}-{i}", "postingTitle": "M", "transformedPostingTitle": "m", "locations": []} for i in range(20)]
        loader = {"search": {"searchResults": rows, "totalRecords": 9999}}
        return httpx.Response(200, text=f"<script>window.__staticRouterHydrationData = JSON.parse({json.dumps(json.dumps({'loaderData': loader}))});</script>")

    respx.get(AP + "/search").mock(side_effect=page)
    with httpx.Client() as client:
        jobs = apple.fetch(apple_company, client, ["manager"], wants_body=lambda j: False, max_per_term=60)
        assert len(jobs) == 60
        caplog.clear()
        jobs = apple.fetch(apple_company, client, ["manager"], wants_body=lambda j: False, max_per_term=30)
    assert len(jobs) == 30  # part of the last page, not all of it
    assert "kept the first 30" in caplog.text


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
        ("icims_careers_company", [("GET", IC, "icims_careers_jobs.json")]),
        ("gem_company", [("GET", GEM, "gem_job_posts.json")]),
        ("rippling_company", [("GET", RIP + "/", "rippling_job.json"), ("GET", RIP, "rippling_jobs.json")]),
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
    ph = Company(name="x", ats="phenom", slug="Careers.Example.com/us/en")  # each tenant is its own host
    assert rate_group(ph) == "careers.example.com" == request_group(httpx.URL("https://careers.example.com/widgets"))
    sf = Company(name="x", ats="successfactors", slug="Careers.Example.com")  # each site is its own host
    assert rate_group(sf) == "careers.example.com" == request_group(httpx.URL("https://careers.example.com/sitemap.xml"))
    for ats in ("radancy", "paradox"):  # each site is its own host
        board = Company(name="x", ats=ats, slug="Jobs.Example.com")
        assert rate_group(board) == "jobs.example.com" == request_group(httpx.URL("https://jobs.example.com/sitemap.xml"))
    # *.eightfold.ai tenants are one service: together they answered 405 to a fetch's burst
    for slug in ("Eaton.eightfold.ai", "nvidia.eightfold.ai"):
        assert rate_group(Company(name="x", ats="eightfold", slug=slug)) == "eightfold"
    assert request_group(httpx.URL("https://nvidia.eightfold.ai/api/pcsx/search")) == "eightfold"
    assert request_group(httpx.URL("https://x.eightfold.ai.evil.example/api")) == "x.eightfold.ai.evil.example"
    ef = Company(name="x", ats="eightfold", slug="Apply.Careers.Microsoft.com")  # its own host: its own group
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


# ------------------------------------------------------------------ iCIMS Career Sites


def _icims_pages(fixture_json, sizes, total=None):
    """Listing pages of these sizes, numbered from 1, all reporting ``total`` postings."""
    base = fixture_json("icims_careers_jobs.json")["jobs"][0]
    pages, n = [], 0
    for size in sizes:
        jobs = []
        for _ in range(size):
            n += 1
            data = {**base["data"], "slug": str(80000 + n), "req_id": str(80000 + n), "title": f"Role {n}"}
            jobs.append({"data": data})
        pages.append({"jobs": jobs, "totalCount": sum(sizes) if total is None else total})
    return pages


@respx.mock
def test_icims_careers_normalizes(icims_careers_company, fixture_json):
    route = respx.get(IC).mock(return_value=httpx.Response(200, json=fixture_json("icims_careers_jobs.json")))
    with httpx.Client() as client:
        jobs = fetch_company(icims_careers_company, client)
    assert route.calls[0].request.url.params["page"] == "1"
    assert route.calls[0].request.url.params["limit"] == "100"
    assert [j.title for j in jobs] == [
        "Director, Platform Engineering", "Senior Manager, Developer Experience", "Accountant"
    ]
    j = jobs[0]
    assert (j.source, j.company, j.company_slug, j.external_id) == (
        "icims_careers", "Example Corp", "careers.example.com", "70001"
    )
    assert j.key == "icims_careers:careers.example.com:70001"
    assert j.url == "https://careers.example.com/jobs/70001"  # the site redirects to its own path
    assert j.location == "Remote, United States" and j.remote is True
    assert jobs[1].location == "Seattle, Washington" and jobs[1].remote is None
    # the description, then the responsibilities and qualifications the API keeps apart, as text
    assert "Lead the & platform team." in j.body and "Own Kubernetes" in j.body
    assert "Run the SRE org." in j.body and "10 years leading infrastructure teams." in j.body
    assert "<" not in j.body
    assert j.posted_at.startswith("2026-") and j.posted_at.endswith("+00:00")  # ISO 8601, UTC


@respx.mock
def test_icims_careers_pages_until_the_total(icims_careers_company, fixture_json):
    pages = _icims_pages(fixture_json, [100, 100, 7])
    route = respx.get(IC).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    with httpx.Client() as client:
        jobs = icims_careers.fetch(icims_careers_company, client)
    assert len(jobs) == 207
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2", "3"]


@respx.mock
def test_icims_careers_stops_at_an_empty_page(icims_careers_company, fixture_json):
    pages = _icims_pages(fixture_json, [100, 0], total=500)  # the total overstates what's there
    route = respx.get(IC).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    with httpx.Client() as client:
        assert len(icims_careers.fetch(icims_careers_company, client)) == 100
    assert route.call_count == 2


@respx.mock
def test_icims_careers_stops_at_the_page_guard(icims_careers_company, fixture_json, monkeypatch, caplog):
    monkeypatch.setattr(icims_careers, "MAX_PAGES", 2)
    pages = _icims_pages(fixture_json, [100, 100, 100], total=300)
    route = respx.get(IC).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    with httpx.Client() as client:
        assert len(icims_careers.fetch(icims_careers_company, client)) == 200
    assert route.call_count == 2
    assert "kept the first 200 of 300" in caplog.text


@respx.mock
def test_icims_careers_dedupes_a_posting_that_shifts_between_pages(icims_careers_company, fixture_json):
    first, second = _icims_pages(fixture_json, [100, 100])
    second["jobs"][0] = first["jobs"][-1]  # a new posting pushed the last one onto page 2
    respx.get(IC).mock(side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)])
    with httpx.Client() as client:
        jobs = icims_careers.fetch(icims_careers_company, client)
    assert len(jobs) == 199 and len({j.key for j in jobs}) == 199


@respx.mock
def test_icims_careers_ids_are_the_requisition_id(icims_careers_company, fixture_json, caplog):
    page = fixture_json("icims_careers_jobs.json")
    page["jobs"][0]["data"]["slug"] = "director-platform-engineering"  # the id comes from req_id
    page["jobs"][1]["data"]["req_id"] = None  # no requisition id: skipped, whatever its slug
    respx.get(IC).mock(return_value=httpx.Response(200, json=page))
    with httpx.Client() as client:
        jobs = icims_careers.fetch(icims_careers_company, client)
    assert [j.external_id for j in jobs] == ["70001", "70003"]
    assert jobs[0].url == "https://careers.example.com/jobs/70001"
    assert "skipped a posting with no req_id" in caplog.text


@respx.mock
def test_icims_careers_404_raises(icims_careers_company):
    respx.get(IC).mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        icims_careers.fetch(icims_careers_company, client)


def test_icims_careers_slug_is_a_host():
    Company(name="x", ats="icims_careers", slug="careers.example.com")
    with pytest.raises(ValueError, match="careers site host"):
        Company(name="x", ats="icims_careers", slug="careers.example.com/careers-home")


def test_icims_careers_rate_group_is_the_sites_host():
    board = Company(name="x", ats="icims_careers", slug="Careers.Example.com")
    assert rate_group(board) == "careers.example.com" == request_group(httpx.URL(IC))


@respx.mock
def test_icims_careers_honors_the_sites_crawl_delay(icims_careers_company, fixture_json):
    # their robots.txt asks for 5 seconds between requests: a cap of 0.2 a second per site
    respx.get(IC).mock(return_value=httpx.Response(200, json=fixture_json("icims_careers_jobs.json")))
    transport = throttle.ThrottledTransport()
    with httpx.Client(transport=transport) as client:
        icims_careers.fetch(icims_careers_company, client)
    assert transport.limiter("careers.example.com").rate == pytest.approx(0.2)
    assert transport.limiter("lever").rate is None  # only these sites' requests carry it


@respx.mock
def test_a_configured_rate_overrides_the_crawl_delay(icims_careers_company, fixture_json):
    respx.get(IC).mock(return_value=httpx.Response(200, json=fixture_json("icims_careers_jobs.json")))
    transport = throttle.ThrottledTransport(max_rate={"careers.example.com": 0.1})
    with httpx.Client(transport=transport) as client:
        icims_careers.fetch(icims_careers_company, client)
    assert transport.limiter("careers.example.com").rate == pytest.approx(0.1)


# ------------------------------------------------------------------ Gem


@respx.mock
def test_gem_normalizes(gem_company, fixture_json):
    respx.get(GEM).mock(return_value=httpx.Response(200, json=fixture_json("gem_job_posts.json")))
    with httpx.Client() as client:
        jobs = fetch_company(gem_company, client)
    assert [j.title for j in jobs] == [
        "Director of Platform Engineering", "Head of Developer Experience", "Account Executive"
    ]
    j = jobs[0]
    assert (j.source, j.company, j.company_slug, j.external_id) == ("gem", "ExampleGem", "examplegem", "9001")
    assert j.key == "gem:examplegem:9001"
    assert j.url == "https://jobs.gem.com/examplegem/9001"
    assert j.location == "Remote, United States"
    assert "Lead the & platform team." in j.body and "Own Kubernetes" in j.body and "<" not in j.body
    assert j.posted_at == "2020-11-17T15:37:23.000Z"
    # remote from Gem's location_type: remote, then hybrid and in_office (explicitly not remote)
    assert [j.remote for j in jobs] == [True, False, False]


@pytest.mark.parametrize(
    "location_type, location, expected",
    [
        (None, "Remote, United States", True),
        ("other", "Remote - US", True),
        (None, "New York, United States", None),
        ("other", "New York, United States", None),
    ],
)
def test_gem_remote_falls_back_to_the_location(gem_company, fixture_json, location_type, location, expected):
    raw = fixture_json("gem_job_posts.json")[2]
    raw["location_type"] = location_type
    raw["location"] = {"name": location}
    assert gem.normalize(gem_company, raw).remote is expected


def test_gem_url_falls_back_when_absolute_url_is_missing(gem_company, fixture_json):
    raw = fixture_json("gem_job_posts.json")[0]
    del raw["absolute_url"]
    assert gem.normalize(gem_company, raw).url == "https://jobs.gem.com/examplegem/9001"


@respx.mock
def test_gem_unknown_board_404_raises(gem_company):
    respx.get(GEM).mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        gem.fetch(gem_company, client)


@respx.mock
def test_gem_skips_a_post_with_no_id(gem_company, fixture_json, caplog):
    posts = fixture_json("gem_job_posts.json")
    del posts[0]["id"]
    respx.get(GEM).mock(return_value=httpx.Response(200, json=posts))
    with httpx.Client() as client:
        assert [j.external_id for j in gem.fetch(gem_company, client)] == ["9002", "9003"]
    assert "skipped a posting with no id" in caplog.text


def test_gem_rate_group():
    board = Company(name="x", ats="gem", slug="examplegem")
    assert rate_group(board) == "gem" == request_group(httpx.URL(GEM))


# ------------------------------------------------------------------ Rippling

RIP_IDS = [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
]


def _rippling(fixture_json, details=None):
    respx.get(RIP).mock(return_value=httpx.Response(200, json=fixture_json("rippling_jobs.json")))
    return respx.get(url__startswith=RIP + "/").mock(
        return_value=details or httpx.Response(200, json=fixture_json("rippling_job.json"))
    )


@respx.mock
def test_rippling_lists_then_describes_only_what_passes(rippling_company, fixture_json):
    detail = _rippling(fixture_json)
    wanted = lambda job: "Director" in job.title  # noqa: E731
    with httpx.Client() as client:
        jobs = fetch_company(rippling_company, client, wants_body=wanted)
    assert [j.title for j in jobs] == [
        "Director, Platform Engineering", "Head of Developer Experience", "Account Executive"
    ]  # names come padded with spaces
    assert detail.call_count == 1 and str(detail.calls[0].request.url).endswith(RIP_IDS[0])
    j = jobs[0]
    assert (j.source, j.company, j.company_slug, j.external_id) == ("rippling", "Example Rip", "examplerip", RIP_IDS[0])
    assert j.key == f"rippling:examplerip:{RIP_IDS[0]}"
    assert j.url == f"https://ats.rippling.com/examplerip/jobs/{RIP_IDS[0]}"
    assert j.location == "Remote (United States)" and j.remote is True
    # the role first, then the company blurb, as text
    assert j.body.index("Lead the platform team.") < j.body.index("Example Rip makes & ships software.")
    assert "Own Kubernetes" in j.body and "<" not in j.body
    assert j.posted_at == "2026-01-27T23:24:57.958000+00:00"  # createdOn, in UTC
    assert jobs[1].body == "" and jobs[1].location == "Seattle, WA" and jobs[1].remote is None


@respx.mock
def test_rippling_keeps_a_posting_whose_description_fails(rippling_company, fixture_json, caplog):
    _rippling(fixture_json, details=httpx.Response(500))
    with httpx.Client() as client:
        jobs = rippling.fetch(rippling_company, client, wants_body=lambda job: True)
    assert len(jobs) == 3 and all(j.body == "" for j in jobs)
    assert "no description" in caplog.text


@respx.mock
def test_rippling_fetches_descriptions_on_a_pool(rippling_company, fixture_json):
    detail = _rippling(fixture_json)
    with httpx.Client() as client, ThreadPoolExecutor(3) as pool:
        jobs = rippling.fetch(rippling_company, client, wants_body=lambda job: True, pool=pool)
    assert detail.call_count == 3 and len(jobs) == 3


@respx.mock
def test_rippling_unknown_board_404_raises(rippling_company):
    respx.get(RIP).mock(return_value=httpx.Response(404))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        rippling.fetch(rippling_company, client)


@respx.mock
def test_rippling_skips_a_posting_with_no_uuid(rippling_company, fixture_json, caplog):
    listing = fixture_json("rippling_jobs.json")
    del listing[0]["uuid"]
    respx.get(RIP).mock(return_value=httpx.Response(200, json=listing))
    with httpx.Client() as client:
        jobs = rippling.fetch(rippling_company, client, wants_body=lambda job: False)
    assert [j.external_id for j in jobs] == RIP_IDS[1:]
    assert "skipped a posting with no uuid" in caplog.text


def test_rippling_rate_group():
    board = Company(name="x", ats="rippling", slug="examplerip")
    assert rate_group(board) == "rippling" == request_group(httpx.URL(RIP))


@respx.mock
def test_rippling_merges_a_posting_listed_once_per_location(rippling_company, fixture_json):
    listing = fixture_json("rippling_jobs.json")
    second = {**listing[1], "workLocation": {"label": "Remote (United States)", "id": "Remote (United States)"}}
    listing.insert(2, second)  # Rippling lists a posting once for each of its locations
    respx.get(RIP).mock(return_value=httpx.Response(200, json=listing))
    with httpx.Client() as client:
        jobs = rippling.fetch(rippling_company, client, wants_body=lambda job: False)
    assert [j.external_id for j in jobs] == RIP_IDS
    assert jobs[1].location == "Seattle, WA; Remote (United States)" and jobs[1].remote is True


@respx.mock
def test_rippling_detail_locations_replace_the_listing(rippling_company, fixture_json):
    detail = {**fixture_json("rippling_job.json"), "workLocations": ["Seattle, WA", "Remote (United States)"]}
    _rippling(fixture_json, details=httpx.Response(200, json=detail))
    with httpx.Client() as client:
        jobs = rippling.fetch(rippling_company, client, wants_body=lambda job: "Director" in job.title)
    assert jobs[0].location == "Seattle, WA; Remote (United States)"  # listed: "Remote (United States)"


@respx.mock
def test_rippling_keeps_a_posting_with_no_location(rippling_company, fixture_json):
    listing = fixture_json("rippling_jobs.json")
    listing[1]["workLocation"] = None
    del listing[2]["workLocation"]
    respx.get(RIP).mock(return_value=httpx.Response(200, json=listing))
    with httpx.Client() as client:
        jobs = rippling.fetch(rippling_company, client, wants_body=lambda job: False)
    assert [j.external_id for j in jobs] == RIP_IDS
    assert [j.location for j in jobs] == ["Remote (United States)", "", ""]


# ------------------------------------------------------------------ USAJOBS


def _usajobs(fixture_json, pages=None):
    return respx.get(UJ).mock(
        side_effect=pages or [httpx.Response(200, json=fixture_json("usajobs_search.json"))] * 10
    )


@respx.mock
def test_usajobs_searches_each_term_with_the_key_and_email(usajobs_company, fixture_json):
    route = _usajobs(fixture_json)
    with httpx.Client() as client:
        jobs = fetch_company(usajobs_company, client, search=["director", "head of"], usajobs_auth=UJ_AUTH)
    sent = [c.request for c in route.calls]
    assert [r.url.params["Keyword"] for r in sent] == ["director", "head of"]
    r = sent[0]
    assert r.headers["Authorization-Key"] == "test-key" and r.headers["User-Agent"] == "me@example.com"
    assert r.headers["Host"] == "data.usajobs.gov"
    params = dict(r.url.params)
    assert params["LocationName"] == "Springfield, Illinois" and params["Radius"] == "50"
    assert (params["ResultsPerPage"], params["Page"], params["Fields"]) == ("500", "1", "Full")
    assert "RemoteIndicator" not in params  # a place's search includes its remote jobs
    assert len(jobs) == 3  # the same postings from both terms, once each


@respx.mock
def test_usajobs_normalizes(usajobs_company, fixture_json):
    _usajobs(fixture_json)
    with httpx.Client() as client:
        jobs = usajobs.fetch(usajobs_company, client, ["director"], auth=UJ_AUTH)
    # two postings of one announcement (one PositionID) stay apart: the control number is the id
    assert [j.external_id for j in jobs] == ["900000001", "900000002", "900000003"]
    j = jobs[0]
    assert (j.source, j.company, j.company_slug) == ("usajobs", "Example Agency", "Springfield, Illinois/50")
    assert j.key == "usajobs:Springfield, Illinois/50:900000001"
    assert j.title == "Director, Cloud Infrastructure"
    assert j.url == "https://www.usajobs.gov/job/900000001"  # without the :443
    assert j.location == "Anywhere in the U.S. (remote job)" and j.remote is True
    assert jobs[1].remote is None and jobs[1].location == "Seattle, Washington"
    assert "Lead the & agency's cloud platform." in j.body and "Own the AWS landing zone" in j.body
    assert "Ten years leading infrastructure." in j.body and "<" not in j.body
    assert j.posted_at == "2026-09-30T00:00:00+00:00"


@respx.mock
def test_usajobs_a_remote_board_searches_remote_jobs_anywhere(fixture_json):
    route = _usajobs(fixture_json)
    board = Company(name="USAJOBS", ats="usajobs", slug="remote")
    with httpx.Client() as client:
        usajobs.fetch(board, client, ["director"], auth=UJ_AUTH)
    params = dict(route.calls[0].request.url.params)
    assert params["RemoteIndicator"] == "True" and "LocationName" not in params and "Radius" not in params


@respx.mock
def test_usajobs_a_place_without_a_radius_sends_none(fixture_json):
    route = _usajobs(fixture_json)
    board = Company(name="USAJOBS", ats="usajobs", slug="Springfield, Illinois")
    with httpx.Client() as client:
        usajobs.fetch(board, client, ["director"], auth=UJ_AUTH)
    params = dict(route.calls[0].request.url.params)
    assert params["LocationName"] == "Springfield, Illinois" and "Radius" not in params


@respx.mock
def test_usajobs_pages_through_the_results(usajobs_company, fixture_json):
    first = fixture_json("usajobs_search.json")
    first["SearchResult"]["UserArea"]["NumberOfPages"] = "2"
    second = fixture_json("usajobs_search.json")
    for item in second["SearchResult"]["SearchResultItems"]:
        item["MatchedObjectId"] = "8" + item["MatchedObjectId"][1:]
    route = _usajobs(fixture_json, pages=[httpx.Response(200, json=first), httpx.Response(200, json=second)])
    with httpx.Client() as client:
        jobs = usajobs.fetch(usajobs_company, client, ["director"], auth=UJ_AUTH)
    assert [c.request.url.params["Page"] for c in route.calls] == ["1", "2"]
    assert len(jobs) == 6


@respx.mock
def test_usajobs_without_a_key_or_email_is_skipped_with_a_warning(usajobs_company, caplog):
    route = respx.get(UJ)
    with httpx.Client() as client:
        assert usajobs.fetch(usajobs_company, client, ["director"], auth=None) == []
        assert usajobs.fetch(usajobs_company, client, ["director"], auth=("key", "")) == []
    assert route.call_count == 0
    assert "usajobs.api_key and usajobs.email" in caplog.text


@respx.mock
def test_usajobs_does_not_follow_a_redirect_with_the_key(usajobs_company):
    respx.get(UJ).mock(
        return_value=httpx.Response(302, headers={"Location": "https://other.example.com/steal"})
    )
    elsewhere = respx.get(url__startswith="https://other.example.com/")
    with httpx.Client(follow_redirects=True) as client, pytest.raises(httpx.HTTPStatusError):
        usajobs.fetch(usajobs_company, client, ["director"], auth=UJ_AUTH)
    assert elsewhere.call_count == 0  # the key and email never leave data.usajobs.gov


@respx.mock
def test_usajobs_a_term_that_hits_the_cap_is_truncated_and_logged(usajobs_company, fixture_json, caplog):
    _usajobs(fixture_json)
    with httpx.Client() as client:
        jobs = usajobs.fetch(usajobs_company, client, ["director"], max_per_term=2, auth=UJ_AUTH)
    assert [j.external_id for j in jobs] == ["900000001", "900000002"]
    assert "'director' has 3 hits; kept the first 2" in caplog.text


def test_usajobs_rate_group(usajobs_company):
    assert rate_group(usajobs_company) == "usajobs" == request_group(httpx.URL(UJ))
