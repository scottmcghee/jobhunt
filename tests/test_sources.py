"""ATS adapters: fixture-driven, network mocked with respx."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import respx

from jobhunt.schema import Company
from jobhunt.sources import (
    ashby,
    fetch_company,
    greenhouse,
    lever,
    rate_group,
    request_group,
    smartrecruiters,
    workday,
)
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
]


def _fetch_listing(source, company, method, url, data):
    with respx.mock:
        respx.route(method=method, url=url).mock(return_value=httpx.Response(200, json=data))
        with httpx.Client() as client:
            if source in (workday, smartrecruiters):
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
