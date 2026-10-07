"""End-to-end CLI path with mocked network and a fake model."""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import respx

from jobhunt import cli, storage, throttle
from jobhunt.schema import Company, Score, ScoredJob
from tests.conftest import CONFIG_DIR, make_completer


@pytest.fixture(autouse=True)
def _no_transient_backoff(monkeypatch):
    """Transient retries still happen here, without their real 1-2 s waits (test_throttle checks those)."""
    monkeypatch.setattr(throttle.ThrottledTransport, "_backoff", lambda self, attempt: None)


@respx.mock
def test_fetch_then_score_then_letter(tmp_path, fixture_json, monkeypatch):
    # Point the CLI at a one-company config so the test is hermetic.
    companies_yaml = tmp_path / "companies.yaml"
    companies_yaml.write_text(
        "companies:\n  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n"
    )
    real_load = cli.config.load_companies
    monkeypatch.setattr(cli.config, "load_companies", lambda path=None: real_load(companies_yaml))

    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    data_dir, out_dir = tmp_path / "data", tmp_path / "out"

    # fetch: 4 postings, 1 passes the filter
    rc = cli.main(["--data-dir", str(data_dir), "fetch"])
    assert rc == 0
    jobs = storage.load_jobs(data_dir)
    assert [j.title for j in jobs] == ["Director of Platform Engineering"]

    # second fetch is a no-op (idempotent)
    cli.main(["--data-dir", str(data_dir), "fetch"])
    assert len(storage.load_jobs(data_dir)) == 1

    # score with a fake model
    fake_score = make_completer(
        {"score": 8, "rationale": "fits", "strengths": [], "gaps": [],
         "suggested_modules": ["build_infra_devx", "sre_from_nothing"]}
    )
    monkeypatch.setattr(cli, "_completer", lambda _=None: fake_score)
    rc = cli.main(["--data-dir", str(data_dir), "score"])
    assert rc == 0
    assert storage.load_scores(data_dir)[0].score.score == 8

    # letter with a fake model
    fake_letter = make_completer(
        {"custom_opening_sentence": "Specific opener.", "custom_closing_sentence": "Specific closer."}
    )
    monkeypatch.setattr(cli, "_completer", lambda _=None: fake_letter)
    rc = cli.main(["--data-dir", str(data_dir), "--output-dir", str(out_dir), "letter"])
    assert rc == 0
    files = list(out_dir.glob("*.md"))
    assert len(files) == 1
    assert "Specific opener." in files[0].read_text()


def test_fetch_unknown_company_errors(tmp_path):
    rc = cli.main(["--data-dir", str(tmp_path), "fetch", "--company", "Nope Inc"])
    assert rc == 2


def test_score_skips_unusable_reply_and_keeps_going(
    tmp_path, platform_director_job, monkeypatch, caplog
):
    first = platform_director_job
    second = first.model_copy(update={"external_id": "second"})
    for j in (first, second):
        storage.append_jsonl(tmp_path / "jobs.jsonl", j.model_dump())

    good = make_completer({"score": 6, "rationale": "ok", "suggested_modules": []})
    replies = iter(["I cannot score this one.", good("", "")])
    monkeypatch.setattr(cli, "_completer", lambda _=None: lambda system, user, max_tokens: next(replies))

    rc = cli.main(["--data-dir", str(tmp_path), "score"])

    assert rc == 0
    assert [s.job.key for s in storage.load_scores(tmp_path)] == [second.key]
    assert first.key in caplog.text  # left unscored, so the next run retries it


GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"


def _two_company_config(tmp_path):
    p = tmp_path / "companies.yaml"
    p.write_text(
        "# keep me\ncompanies:\n"
        "  - name: Live\n    ats: greenhouse\n    slug: live\n\n"
        "  - name: Dead\n    ats: greenhouse\n    slug: dead\n"
    )
    return p


def _fetch(tmp_path, companies, *extra):
    return cli.main(["--data-dir", str(tmp_path / "data"), "--companies", str(companies), "fetch", *extra])


def _misses(tmp_path):
    return storage.MissLedger(tmp_path / "data" / "misses.json")


@respx.mock
def test_board_removed_after_three_consecutive_404s(tmp_path, fixture_json):
    companies = _two_company_config(tmp_path)
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))

    for expected in (1, 2):
        assert _fetch(tmp_path, companies) == 0
        assert [c.name for c in cli.config.load_companies(companies)] == ["Live", "Dead"]
        assert _misses(tmp_path).counts == {"greenhouse:dead": expected}

    assert _fetch(tmp_path, companies) == 0
    assert [c.name for c in cli.config.load_companies(companies)] == ["Live"]
    assert companies.read_text().startswith("# keep me\n")
    assert _misses(tmp_path).counts == {}


@respx.mock
def test_successful_fetch_clears_404_count(tmp_path, fixture_json):
    companies = _two_company_config(tmp_path)
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    dead = respx.get(GH.format("dead"))

    dead.mock(return_value=httpx.Response(404))
    _fetch(tmp_path, companies)
    _fetch(tmp_path, companies)
    dead.mock(return_value=httpx.Response(200, json={"jobs": []}))
    _fetch(tmp_path, companies)
    assert _misses(tmp_path).counts == {}

    dead.mock(return_value=httpx.Response(404))
    _fetch(tmp_path, companies)
    assert _misses(tmp_path).counts == {"greenhouse:dead": 1}
    assert len(cli.config.load_companies(companies)) == 2


@respx.mock
def test_other_errors_neither_count_nor_reset(tmp_path, fixture_json):
    companies = _two_company_config(tmp_path)
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    dead = respx.get(GH.format("dead"))

    dead.mock(return_value=httpx.Response(404))
    _fetch(tmp_path, companies)
    dead.mock(return_value=httpx.Response(500))
    _fetch(tmp_path, companies)
    dead.mock(side_effect=httpx.ConnectTimeout("slow"))
    _fetch(tmp_path, companies)
    assert _misses(tmp_path).counts == {"greenhouse:dead": 1}


WD = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/{}/jobs"


def _workday_config(tmp_path):
    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n"
        "  - name: acme\n    ats: workday\n    slug: acme/Open\n    datacenter: wd5\n\n"
        "  - name: acme\n    ats: workday\n    slug: acme/Gone\n    datacenter: wd5\n"
    )
    return p


def _workday_error(status, code):
    return httpx.Response(status, json={"errorCode": code, "httpStatus": status, "message": ""})


@pytest.mark.parametrize(
    "response",
    [_workday_error(422, "HTTP_422"), _workday_error(403, "S22")],
    ids=["422-removed-site", "403-S22-closed-site"],
)
@respx.mock
def test_a_dead_workday_site_counts_like_a_404(tmp_path, capsys, response):
    companies = _workday_config(tmp_path)
    respx.post(WD.format("Open")).mock(return_value=httpx.Response(200, json={"total": 0, "jobPostings": []}))
    respx.post(WD.format("Gone")).mock(return_value=response)
    for expected in (1, 2):
        _fetch(tmp_path, companies)
        assert _misses(tmp_path).counts == {"workday:acme/Gone": expected}
    _fetch(tmp_path, companies)
    assert [c.slug for c in cli.config.load_companies(companies)] == ["acme/Open"]
    out = capsys.readouterr().out
    assert "removed acme (acme/Gone) from companies.yaml: gone 3 fetches in a row" in out


@pytest.mark.parametrize(
    "response",
    [
        _workday_error(403, "S99"),  # a 403 about something else
        httpx.Response(403, text="<html>blocked</html>"),  # a block page: the host, not the site
        _workday_error(400, "HTTP_400"),
        _workday_error(422, "S22"),  # each code counts only with its own status
        _workday_error(403, "HTTP_422"),
        httpx.Response(403, json=[{"errorCode": "S22"}]),  # JSON, but not an object
        httpx.Response(422, json=[{"errorCode": "HTTP_422"}]),
    ],
)
@respx.mock
def test_other_workday_errors_dont_count(tmp_path, response):
    companies = _workday_config(tmp_path)
    respx.post(WD.format("Open")).mock(return_value=httpx.Response(200, json={"total": 0, "jobPostings": []}))
    respx.post(WD.format("Gone")).mock(return_value=response)
    assert _fetch(tmp_path, companies) == 0  # the run completes
    assert _misses(tmp_path).counts == {}


def _redirect(status: int, location: str) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


@pytest.mark.parametrize(
    ("response", "counts"),
    [
        (_redirect(302, "https://www.bamboohr.com/"), 1),
        (_redirect(302, "https://www.bamboohr.com/login"), 1),
        (_redirect(302, "https://bamboohr.com/"), 1),
        (_redirect(302, "https://acme.example.com/jobs"), 0),
        (_redirect(500, "https://www.bamboohr.com/"), 0),  # not a redirect, whatever its Location
        (_redirect(500, "https://www.bamboohr.com:abc/"), 0),  # an unparseable Location doesn't abort the run
        # httpx itself rejects the Location (RemoteProtocolError), so the board is skipped, not counted
        (_redirect(302, "https://www.bamboohr.com:abc/"), 0),
    ],
)
@respx.mock
def test_a_bamboohr_tenant_that_redirects_to_bamboohr_counts_like_a_404(tmp_path, response, counts):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  - name: Live\n    ats: greenhouse\n    slug: live\n\n"
        "  - name: acme\n    ats: bamboohr\n    slug: acme\n"
    )
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    respx.get("https://acme.bamboohr.com/careers/list").mock(return_value=response)
    assert _fetch(tmp_path, companies) == 0
    assert _misses(tmp_path).counts == ({"bamboohr:acme": 1} if counts else {})


def test_a_bamboohr_redirect_to_an_unparseable_location_is_not_gone():
    company = Company(name="a", ats="bamboohr", slug="a")
    response = httpx.Response(302, headers={"Location": "https://www.bamboohr.com:abc/"})
    assert cli._board_gone(company, response) is False


@respx.mock
def test_a_422_from_another_ats_doesnt_count(tmp_path):
    companies = _two_company_config(tmp_path)
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(422, json={"errorCode": "HTTP_422"}))
    _fetch(tmp_path, companies)
    assert _misses(tmp_path).counts == {}


@respx.mock
def test_warnings_name_the_board_and_fit_on_one_line(tmp_path, caplog):
    companies = _workday_config(tmp_path)
    respx.post(WD.format("Open")).mock(side_effect=httpx.ConnectError("boom\nFor more information: x"))
    respx.post(WD.format("Gone")).mock(return_value=_workday_error(500, "HTTP_500"))
    _fetch(tmp_path, companies)
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "acme (acme/Open): boom For more information: x" in lines
    assert "acme (acme/Gone): HTTP 500 — check slug/ATS" in lines


@respx.mock
def test_a_board_with_malformed_data_is_skipped_on_one_line(tmp_path, caplog, monkeypatch):
    companies = _two_company_config(tmp_path)
    real_fetch = cli.fetch_company

    def fetch(company, client, **kw):
        if company.slug == "dead":
            raise ValueError("bad posting\nat line 2")
        return real_fetch(company, client, **kw)

    monkeypatch.setattr(cli, "fetch_company", fetch)
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    assert _fetch(tmp_path, companies) == 0
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "Dead: skipped, ValueError: bad posting at line 2" in lines


@respx.mock
def test_a_board_whose_slug_is_its_name_is_named_once(tmp_path, caplog):
    companies = _two_company_config(tmp_path)
    companies.write_text(companies.read_text().replace("name: Dead", "name: dead"))
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))
    _fetch(tmp_path, companies)
    assert "dead: HTTP 404 — check slug/ATS" in [r.getMessage() for r in caplog.records]


@respx.mock
def test_a_transient_failure_is_retried_before_the_board_is_skipped(tmp_path, fixture_json):
    companies = _two_company_config(tmp_path)
    respx.get(GH.format("live")).mock(
        side_effect=[httpx.ConnectError("[Errno 9] Bad file descriptor"),
                     httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))]
    )
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    assert _fetch(tmp_path, companies) == 0
    assert len(storage.load_jobs(tmp_path / "data")) == 1


@respx.mock
def test_fetch_passes_the_title_terms_as_search_terms(tmp_path, monkeypatch, fixture_json):
    companies = _two_company_config(tmp_path)
    seen = []
    real = cli.fetch_company

    def fetch(company, client, **kw):
        seen.append(kw.get("search"))
        return real(company, client, **kw)

    monkeypatch.setattr(cli, "fetch_company", fetch)
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    assert _fetch(tmp_path, companies, "--dry-run") == 0
    terms = cli.config.load_preferences().title.must_include_any
    assert seen and all(s == terms for s in seen)


def _manager_job(slug):
    return {"jobs": [{"id": 7, "title": "Observability SRE Manager", "location": {"name": "Seattle, WA"},
                      "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/7", "content": "Run our platform."}]}


@respx.mock
def test_a_tagged_board_takes_its_extra_target_words(tmp_path, monkeypatch):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  - name: Big\n    ats: greenhouse\n    slug: big\n    tags: [big-tech]\n\n"
        "  - name: Small\n    ats: greenhouse\n    slug: small\n"
    )
    respx.get(GH.format("big")).mock(return_value=httpx.Response(200, json=_manager_job("big")))
    respx.get(GH.format("small")).mock(return_value=httpx.Response(200, json=_manager_job("small")))
    seen = {}
    real = cli.fetch_company

    def fetch(company, client, **kw):
        job = cli.Job(source="greenhouse", company=company.name, company_slug=company.slug, external_id="x",
                      title="Observability SRE Manager", location="Seattle, WA", url="https://x")
        seen[company.slug] = (kw["search"], kw["wants_body"](job))
        return real(company, client, **kw)

    monkeypatch.setattr(cli, "fetch_company", fetch)
    assert _fetch(tmp_path, companies) == 0
    assert [j.company_slug for j in storage.load_jobs(tmp_path / "data")] == ["big"]
    base = cli.config.load_preferences().title.must_include_any
    assert seen["big"] == ([*base, "manager"], True)  # searched, and its description is wanted
    assert seen["small"] == (base, False)


@respx.mock
def test_search_caps_come_from_settings(tmp_path, monkeypatch):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  - name: Amazon\n    ats: amazon\n    slug: USA\n\n"
        "  - name: Live\n    ats: greenhouse\n    slug: live\n"
    )
    monkeypatch.setenv("JOBHUNT_FETCH_MAX_PER_TERM", '{"amazon": 7}')
    seen = {}

    def fetch(company, client, **kw):
        seen[company.ats] = kw.get("max_per_term")
        return []

    monkeypatch.setattr(cli, "fetch_company", fetch)
    assert _fetch(tmp_path, companies, "--dry-run") == 0
    assert seen == {"amazon": 7, "greenhouse": None}  # each board gets its own source's cap


@respx.mock
def test_dry_run_does_not_count_404s(tmp_path, fixture_json):
    companies = _two_company_config(tmp_path)
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))

    for _ in range(3):
        _fetch(tmp_path, companies, "--dry-run")
    assert _misses(tmp_path).counts == {}
    assert len(cli.config.load_companies(companies)) == 2


def _render(*argv):
    """Help text for a command, with argparse's line wrapping collapsed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        cli.main([*argv, "--help"])
    return " ".join(out.getvalue().split())


def test_main_help_describes_the_tool():
    text = _render()
    assert "Command-line entry point" not in text
    assert "cover letter" in text and "score" in text


def test_letter_help_explains_job_keys():
    text = _render("letter")
    assert "source:company_slug:external_id" in text  # the Job field names, as in scores.jsonl
    assert "greenhouse:huntress:7777533003" in text
    assert "jobhunt list" in text


def test_fetch_help_says_company_matches_name_not_slug():
    text = _render("fetch")
    assert "name" in text and "not the slug" in text


def test_list_shows_job_keys(tmp_path, platform_director_job, capsys):
    sj = ScoredJob(job=platform_director_job, score=Score(score=7, rationale="r", model="t"))
    storage.append_jsonl(tmp_path / "scores.jsonl", sj.model_dump())
    assert cli.main(["--data-dir", str(tmp_path), "list"]) == 0
    assert platform_director_job.key in capsys.readouterr().out


def test_missing_config_is_a_friendly_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "DEFAULT_CONFIG_DIR", tmp_path / "config")
    assert cli.main(["--data-dir", str(tmp_path), "list"]) == 0  # list needs no config
    assert cli.main(["--data-dir", str(tmp_path), "fetch"]) == 2
    assert "cp -R config.example config" in capsys.readouterr().err


@respx.mock
def test_fetch_workday_fetches_bodies_for_title_matches_only(tmp_path, fixture_json):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  - name: ExampleCorp\n    ats: workday\n    slug: examplecorp/External\n    datacenter: wd5\n"
    )
    base = "https://examplecorp.wd5.myworkdayjobs.com/wday/cxs/examplecorp/External"
    respx.post(base + "/jobs").mock(return_value=httpx.Response(200, json=fixture_json("workday_jobs.json")))
    detail = respx.get(base + "/job/Seattle-WA/Director-of-Platform-Engineering_R1001").mock(
        return_value=httpx.Response(200, json=fixture_json("workday_job.json"))
    )

    assert _fetch(tmp_path, companies) == 0
    assert detail.call_count == 1  # "Senior Software Engineer" and "Director of Sales" fail the title check
    jobs = storage.load_jobs(tmp_path / "data")
    assert [j.key for j in jobs] == ["workday:examplecorp/External:Director-of-Platform-Engineering_R1001"]


@respx.mock
def test_fetch_smartrecruiters_fetches_bodies_for_title_matches_only(tmp_path, fixture_json):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - name: ExampleCorp\n    ats: smartrecruiters\n    slug: ExampleCorp\n")
    base = "https://api.smartrecruiters.com/v1/companies/ExampleCorp/postings"
    respx.get(base).mock(return_value=httpx.Response(200, json=fixture_json("smartrecruiters_postings.json")))
    detail = respx.get(base + "/744000000001001").mock(
        return_value=httpx.Response(200, json=fixture_json("smartrecruiters_posting.json"))
    )

    assert _fetch(tmp_path, companies) == 0
    assert detail.call_count == 1  # "Senior Software Engineer" and "Director of Sales" fail the title check
    jobs = storage.load_jobs(tmp_path / "data")
    assert [j.key for j in jobs] == ["smartrecruiters:ExampleCorp:744000000001001"]


def _two_boards(tmp_path):
    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n"
        "  - name: Broken\n    ats: greenhouse\n    slug: broken\n\n"
        "  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n"
    )
    return p


def _failing_for(slug, error, monkeypatch):
    real = cli.fetch_company

    def fetch(company, client, **kwargs):
        if company.slug == slug:
            raise error
        return real(company, client, **kwargs)

    monkeypatch.setattr(cli, "fetch_company", fetch)


@respx.mock
def test_fetch_skips_a_board_that_fails_unexpectedly(tmp_path, fixture_json, monkeypatch, caplog):
    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    _failing_for("broken", KeyError("externalPath"), monkeypatch)

    assert _fetch(tmp_path, _two_boards(tmp_path)) == 0
    assert "Broken: skipped, KeyError: 'externalPath'" in caplog.text
    assert [j.title for j in storage.load_jobs(tmp_path / "data")] == ["Director of Platform Engineering"]
    assert len(_misses(tmp_path).counts) == 0  # not a 404


@respx.mock
def test_fetch_interrupted_saves_what_it_has(tmp_path, fixture_json, monkeypatch, capsys):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n"
        "  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n\n"
        "  - name: Later\n    ats: greenhouse\n    slug: later\n"
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    _failing_for("later", KeyboardInterrupt(), monkeypatch)

    assert _fetch(tmp_path, companies, "--per-host", "1") == 130  # one worker: deterministic order
    assert "interrupted" in capsys.readouterr().err
    jobs = storage.load_jobs(tmp_path / "data")
    assert [j.title for j in jobs] == ["Director of Platform Engineering"]
    assert jobs[0].key in storage.SeenSet(tmp_path / "data" / "seen.json")


@respx.mock
def test_fetch_interrupted_dry_run_writes_nothing(tmp_path, fixture_json, monkeypatch):
    respx.get("https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs").mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    _failing_for("broken", KeyboardInterrupt(), monkeypatch)
    assert _fetch(tmp_path, _two_boards(tmp_path), "--dry-run", "--per-host", "1") == 130
    assert not (tmp_path / "data").exists()


def _two_scored_jobs(tmp_path, scored_job):
    second = scored_job.model_copy(update={"job": scored_job.job.model_copy(update={"external_id": "2002", "title": "VP of Infrastructure"})})
    for s in (scored_job, second):
        storage.append_jsonl(tmp_path / "data" / "scores.jsonl", s.model_dump())
    return scored_job, second


def _letter(tmp_path, *extra):
    return cli.main(["--data-dir", str(tmp_path / "data"), "--output-dir", str(tmp_path / "out"), "letter", *extra])


LETTER_REPLY = '{"custom_opening_sentence": "Open.", "custom_closing_sentence": "Close."}'


def test_letter_skips_an_unusable_reply_and_keeps_going(tmp_path, scored_job, monkeypatch, caplog, capsys):
    first, second = _two_scored_jobs(tmp_path, scored_job)
    replies = iter(["I cannot write this one.", LETTER_REPLY])
    monkeypatch.setattr(cli, "_completer", lambda _=None: lambda system, user, max_tokens: next(replies))

    assert _letter(tmp_path) == 0
    assert storage.lettered_job_keys(tmp_path / "out") == {second.job.key}
    assert first.job.key in caplog.text  # no letter, so the next run tries it again
    assert "1 letter(s) skipped" in capsys.readouterr().out


def test_letter_skips_jobs_that_already_have_one(tmp_path, scored_job, monkeypatch, capsys):
    _two_scored_jobs(tmp_path, scored_job)
    calls = []

    def complete(system, user, max_tokens):
        calls.append(user)
        return LETTER_REPLY

    monkeypatch.setattr(cli, "_completer", lambda _=None: complete)
    assert _letter(tmp_path) == 0 and len(calls) == 2
    capsys.readouterr()

    assert _letter(tmp_path) == 0 and len(calls) == 2  # nothing regenerated
    assert "2 job(s) already have a letter" in capsys.readouterr().out

    assert _letter(tmp_path, "--force") == 0 and len(calls) == 4


def test_letter_keeps_one_letter_per_posting_with_the_same_title(tmp_path, scored_job, monkeypatch):
    # The same company and title posted twice (e.g. two locations) must not share a file.
    second = scored_job.model_copy(update={"job": scored_job.job.model_copy(update={"external_id": "2002"})})
    for s in (scored_job, second):
        storage.append_jsonl(tmp_path / "data" / "scores.jsonl", s.model_dump())
    calls = []

    def complete(system, user, max_tokens):
        calls.append(user)
        return LETTER_REPLY

    monkeypatch.setattr(cli, "_completer", lambda _=None: complete)
    assert _letter(tmp_path) == 0 and len(calls) == 2
    assert len(list((tmp_path / "out").glob("*.md"))) == 2
    assert storage.lettered_job_keys(tmp_path / "out") == {scored_job.job.key, second.job.key}
    assert _letter(tmp_path) == 0 and len(calls) == 2  # nothing regenerated


def test_letter_skips_a_lettered_job_whose_key_has_a_space(tmp_path, scored_job, monkeypatch):
    spaced = scored_job.model_copy(update={"job": scored_job.job.model_copy(update={"source": "ashby", "company_slug": "Some Co"})})
    storage.append_jsonl(tmp_path / "data" / "scores.jsonl", spaced.model_dump())
    monkeypatch.setattr(cli, "_completer", lambda _=None: lambda system, user, max_tokens: LETTER_REPLY)
    assert _letter(tmp_path) == 0
    monkeypatch.setattr(cli, "_completer", lambda _=None: pytest.fail)  # must not be called
    assert _letter(tmp_path) == 0


def test_letter_for_one_job_respects_existing_letters(tmp_path, scored_job, monkeypatch, capsys):
    first, _ = _two_scored_jobs(tmp_path, scored_job)
    monkeypatch.setattr(cli, "_completer", lambda _=None: lambda system, user, max_tokens: LETTER_REPLY)
    _letter(tmp_path, "--job", first.job.key)
    capsys.readouterr()
    monkeypatch.setattr(cli, "_completer", lambda _=None: pytest.fail)  # must not be called
    assert _letter(tmp_path, "--job", first.job.key) == 0
    assert "already have a letter" in capsys.readouterr().out


def test_run_accepts_force():
    args = cli.build_parser().parse_args(["run", "--force"])
    assert args.force is True


@respx.mock
def test_fetch_board_only_reports_and_record_keeps_the_books(tmp_path):
    # the worker half touches no shared state, so it can later run in a thread
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))
    board = cli.Company(name="Dead", ats="greenhouse", slug="dead")
    with httpx.Client() as client:
        outcome = cli._fetch_board(board, client, lambda job: True, verbose=False)
    assert outcome.jobs is None and outcome.status == 404

    misses, dead, new_jobs = _misses(tmp_path), set(), []
    prefs = cli.config.load_preferences()
    seen = storage.SeenSet(tmp_path / "seen.json")
    cli._record(outcome, prefs, seen, misses, dead, new_jobs, verbose=False)
    assert misses.counts == {"greenhouse:dead": 1} and not dead and not new_jobs


@respx.mock
def test_a_throttled_board_is_retried_not_skipped(tmp_path, fixture_json, monkeypatch):
    # the real throttling on a fake clock, so its 1 s backoff takes no time (test_throttle
    # checks the waits themselves)
    now = [0.0]

    def instant(fetch, workers, per_host):
        return throttle.ThrottledTransport(
            clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s), jitter=lambda: 0.0
        )

    monkeypatch.setattr(cli, "_transport", instant)
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n")
    route = respx.get(GH.format("examplecorp")).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=fixture_json("greenhouse_jobs.json")),
        ]
    )
    assert _fetch(tmp_path, companies) == 0
    assert route.call_count == 2
    assert [j.title for j in storage.load_jobs(tmp_path / "data")] == ["Director of Platform Engineering"]
    assert _misses(tmp_path).counts == {}



@pytest.mark.parametrize("cmd", ["fetch", "run"])
def test_concurrency_flags(cmd):
    defaults = cli.settings.Settings()
    args = cli._resolve(cli.build_parser().parse_args([cmd]), defaults)
    assert (args.workers, args.per_host) == (32, 6)  # unset flags fall back to settings
    args = cli._resolve(cli.build_parser().parse_args([cmd, "--workers", "1", "--per-host", "1"]), defaults)
    assert (args.workers, args.per_host) == (1, 1)


@pytest.mark.parametrize("cmd", ["fetch", "run"])
@pytest.mark.parametrize("flag", ["--workers", "--per-host"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_concurrency_flags_must_be_at_least_one(cmd, flag, value, capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([cmd, flag, value])
    assert "must be at least 1" in capsys.readouterr().err


@respx.mock
def test_a_group_that_keeps_refusing_is_skipped(tmp_path, caplog):
    p = tmp_path / "companies.yaml"
    p.write_text("companies:\n" + "".join(f"  - name: B{i}\n    ats: greenhouse\n    slug: b{i}\n" for i in range(7)))
    route = respx.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/b\d/jobs").mock(
        return_value=httpx.Response(403)
    )
    assert _fetch(tmp_path, p, "--per-host", "1") == 0
    assert route.call_count == 5  # the breaker trips after five refusals in a row
    assert "skipping its other 2" in caplog.text
    assert _misses(tmp_path).counts == {}  # refused or skipped isn't a 404


@respx.mock
def test_interrupt_keeps_a_board_that_finished_out_of_order(tmp_path, fixture_json, monkeypatch, capsys):
    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n"
        "  - name: Slow\n    ats: greenhouse\n    slug: slow\n\n"
        "  - name: ExampleLever\n    ats: lever\n    slug: examplelever\n"
    )
    respx.get("https://api.lever.co/v0/postings/examplelever").mock(
        return_value=httpx.Response(200, json=fixture_json("lever_postings.json"))
    )
    lever_queued = threading.Event()
    real = cli.fetch_company

    class Runner(cli.BoardRunner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._results = _Announcing(index=1, queued=lever_queued)

    def fetch(company, client, **kwargs):
        if company.slug == "slow":
            assert lever_queued.wait(5)
            raise KeyboardInterrupt  # Ctrl-C while the first board is still running
        return real(company, client, **kwargs)

    monkeypatch.setattr(cli, "fetch_company", fetch)
    monkeypatch.setattr(cli, "BoardRunner", Runner)
    assert _fetch(tmp_path, p) == 130
    out = capsys.readouterr().out
    assert "finished out of order" in out and "ExampleLever" in out
    assert [j.company for j in storage.load_jobs(tmp_path / "data")] == ["ExampleLever"]


class _Announcing(queue.Queue):
    """A runner result queue that sets ``queued`` once the result at ``index`` is in it."""

    def __init__(self, index, queued):
        super().__init__()
        self.index, self.queued = index, queued

    def put(self, entry, *args, **kwargs):
        super().put(entry, *args, **kwargs)
        if entry[0] == self.index:
            self.queued.set()


@respx.mock
def test_a_board_still_running_after_an_interrupt_logs_nothing(tmp_path, monkeypatch, caplog):
    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n"
        "  - name: Slow\n    ats: greenhouse\n    slug: slow\n\n"
        "  - name: Big\n    ats: lever\n    slug: big\n"
    )
    big_in, fetch_returned, big_finished = threading.Event(), threading.Event(), threading.Event()
    real_board = cli._fetch_board

    def fetch(company, client, **kwargs):
        if company.slug == "slow":
            assert big_in.wait(5)
            raise KeyboardInterrupt
        big_in.set()
        assert fetch_returned.wait(5)  # a long board still going after the client was closed
        client.get("https://api.lever.co/v0/postings/big")  # its next request

    def board(company, *args, **kwargs):
        try:
            return real_board(company, *args, **kwargs)
        finally:
            if company.slug == "big":
                big_finished.set()

    monkeypatch.setattr(cli, "fetch_company", fetch)
    monkeypatch.setattr(cli, "_fetch_board", board)
    assert _fetch(tmp_path, p) == 130
    fetch_returned.set()
    assert big_finished.wait(5)
    assert not [r for r in caplog.records if r.levelname == "WARNING" and "Big" in r.getMessage()]


@respx.mock
def test_a_404_that_comes_back_after_an_interrupt_is_not_counted(tmp_path, monkeypatch, capsys, caplog):
    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n"
        "  - name: Slow\n    ats: greenhouse\n    slug: slow\n\n"
        "  - name: Gone\n    ats: lever\n    slug: gone\n"
    )
    respx.get("https://api.lever.co/v0/postings/gone").mock(return_value=httpx.Response(404))
    gone_started, gone_done = threading.Event(), threading.Event()
    runners, gone_thread = [], []
    real = cli.fetch_company

    class Runner(cli.BoardRunner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            runners.append(self)

        def _worker(self, group):
            try:
                super()._worker(group)
            finally:
                if gone_thread == [threading.get_ident()]:
                    gone_done.set()

        def finished_out_of_order(self):
            assert gone_done.wait(5)  # drain only once Gone's worker has finished
            return super().finished_out_of_order()

    def fetch(company, client, **kwargs):
        if company.slug == "slow":
            assert gone_started.wait(5)
            raise KeyboardInterrupt
        gone_thread.append(threading.get_ident())
        gone_started.set()
        assert runners[0]._stop.wait(5)  # the 404 comes back just after Ctrl-C
        return real(company, client, **kwargs)

    monkeypatch.setattr(cli, "fetch_company", fetch)
    monkeypatch.setattr(cli, "BoardRunner", Runner)
    assert _fetch(tmp_path, p) == 130
    assert _misses(tmp_path).counts == {}  # unwarned, so it doesn't count toward pruning either
    assert "Gone" not in capsys.readouterr().out
    assert not [r for r in caplog.records if "Gone" in r.getMessage()]


@respx.mock
def test_no_out_of_order_header_when_no_late_board_prints_a_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOBHUNT_FETCH_MAX_RETRIES", "0")
    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n  - name: Slow\n    ats: lever\n    slug: slow\n"
        + "".join(f"  - name: B{i}\n    ats: greenhouse\n    slug: b{i}\n" for i in range(7))
    )
    respx.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/b\d/jobs").mock(
        return_value=httpx.Response(403)
    )
    last_queued = threading.Event()
    real = cli.fetch_company

    class Runner(cli.BoardRunner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._results = _Announcing(index=7, queued=last_queued)

    def fetch(company, client, **kwargs):
        if company.slug == "slow":
            assert last_queued.wait(5)  # every other board refused or skipped, all out of order
            raise KeyboardInterrupt
        return real(company, client, **kwargs)

    monkeypatch.setattr(cli, "fetch_company", fetch)
    monkeypatch.setattr(cli, "BoardRunner", Runner)
    assert _fetch(tmp_path, p, "--per-host", "1") == 130
    assert "finished out of order" not in capsys.readouterr().out


def _seven_boards(tmp_path, ats="greenhouse"):
    p = tmp_path / "companies.yaml"
    if ats == "workday":
        boards = "".join(
            f"  - name: B{i}\n    ats: workday\n    slug: t{i}/External\n    datacenter: wd5\n" for i in range(7)
        )
    else:
        boards = "".join(f"  - name: B{i}\n    ats: greenhouse\n    slug: b{i}\n" for i in range(7))
    p.write_text("companies:\n" + boards)
    return p


@respx.mock
def test_a_closed_workday_site_is_not_a_refusal(tmp_path, caplog):
    closed = {"errorCode": "S22", "httpStatus": 403, "message": "permission denied"}
    route = respx.post(url__regex=r"https://t\d\.wd5\.myworkdayjobs\.com/wday/cxs/t\d/External/jobs").mock(
        return_value=httpx.Response(403, json=closed)
    )
    assert _fetch(tmp_path, _seven_boards(tmp_path, "workday"), "--per-host", "1") == 0
    assert route.call_count == 7  # a board-level 403 doesn't feed the breaker
    assert "skipping" not in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="<html>Just a moment...</html>", headers={"Content-Type": "text/html"}),
        httpx.Response(403, json={"message": "forbidden"}),
        httpx.Response(429, headers={"Retry-After": "0"}),
    ],
    ids=["403-html", "403-json-without-errorCode", "429"],
)
@respx.mock
def test_a_host_level_refusal_trips_the_breaker(tmp_path, monkeypatch, response):
    monkeypatch.setenv("JOBHUNT_FETCH_MAX_RETRIES", "0")  # one request per board
    route = respx.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/b\d/jobs").mock(
        return_value=response
    )
    assert _fetch(tmp_path, _seven_boards(tmp_path), "--per-host", "1") == 0
    assert route.call_count == 5  # the breaker trips after five in a row


@respx.mock
def test_verbose_fetch_prints_per_group_stats(tmp_path, fixture_json, capsys):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n")
    respx.get(GH.format("examplecorp")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    assert _fetch(tmp_path, companies, "--dry-run") == 0
    assert "requests by host" not in capsys.readouterr().err
    assert cli.main(["-v", "--data-dir", str(tmp_path / "data"), "--companies", str(companies), "fetch", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "requests by host" in err and "greenhouse" in err and "1 requests" in err


@respx.mock
def test_fetch_gives_each_rate_group_its_own_pool(tmp_path, fixture_json, monkeypatch):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n"
        "  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n"
        "  - name: Other\n    ats: greenhouse\n    slug: other\n"
        "  - name: ExampleLever\n    ats: lever\n    slug: examplelever\n"
    )
    for slug in ("examplecorp", "other"):
        respx.get(GH.format(slug)).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    respx.get("https://api.lever.co/v0/postings/examplelever").mock(
        return_value=httpx.Response(200, json=fixture_json("lever_postings.json"))
    )
    pools = {}
    real = cli.fetch_company

    def fetch(company, client, **kwargs):
        pools[company.slug] = kwargs.get("pool")
        return real(company, client, **kwargs)

    monkeypatch.setattr(cli, "fetch_company", fetch)
    assert _fetch(tmp_path, companies, "--workers", "8", "--per-host", "3") == 0
    assert pools["examplecorp"] is pools["other"]  # one host, one queue
    assert pools["examplelever"] is not pools["examplecorp"]
    assert {pool._max_workers for pool in pools.values()} == {3}


def _throttled_transport(handler):
    """Stands in for cli._transport: the real throttling, over a fake network."""
    return lambda fetch, workers=32, per_host=6: throttle.ThrottledTransport(
        inner=httpx.MockTransport(handler), ceiling=per_host, max_in_flight=workers
    )


def _workday_page(request, total):
    offset = json.loads(request.content)["offset"]
    postings = [{"title": f"J{offset + i}", "externalPath": f"/job/X/J_{offset + i}", "locationsText": "X"}
                for i in range(20)]
    return httpx.Response(200, json={"total": total if offset == 0 else 0, "jobPostings": postings})


def test_one_hosts_pooled_pages_do_not_hold_up_anothers(tmp_path, monkeypatch):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n"
        "  - name: BigWorkday\n    ats: workday\n    slug: big/External\n    datacenter: wd5\n"
        "  - name: SmallSR\n    ats: smartrecruiters\n    slug: smallsr\n"
    )
    workday_stuck, small_done = threading.Event(), threading.Event()
    waited = []

    def handler(request):
        if "myworkdayjobs" in request.url.host:
            if json.loads(request.content)["offset"] == 20:  # a later page that hangs...
                workday_stuck.set()
                waited.append(small_done.wait(5))  # ...until the other host's board is done
            return _workday_page(request, total=60)
        offset = int(request.url.params["offset"])
        if offset == 0:  # queue SmallSR's later pages only once BigWorkday's are stuck
            assert workday_stuck.wait(5)
        postings = [{"id": str(offset + i), "name": f"S{offset + i}", "location": {}} for i in range(100)]
        return httpx.Response(200, json={"totalFound": 200, "content": postings})

    real = cli.fetch_company

    def fetch(company, client, **kwargs):
        jobs = real(company, client, **kwargs)
        if company.slug == "smallsr":
            small_done.set()
        return jobs

    monkeypatch.setattr(cli, "_transport", _throttled_transport(handler))
    monkeypatch.setattr(cli, "fetch_company", fetch)
    assert _fetch(tmp_path, companies, "--dry-run", "--workers", "2", "--per-host", "1") == 0
    assert waited == [True]


def test_an_interrupt_stops_pooled_requests_waiting_out_a_429(tmp_path, monkeypatch):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n"
        "  - name: Slow\n    ats: greenhouse\n    slug: slow\n"
        "  - name: Big\n    ats: workday\n    slug: big/External\n    datacenter: wd5\n"
    )
    throttled = threading.Event()
    later_pages = []

    def handler(request):
        if json.loads(request.content)["offset"] != 0:
            later_pages.append(request)
            if len(later_pages) == 1:  # pauses the datacenter; the other pages wait it out
                throttled.set()
                return httpx.Response(429, headers={"Retry-After": "60"})
        return _workday_page(request, total=2000)

    real = cli.fetch_company

    def fetch(company, client, **kwargs):
        if company.slug == "slow":
            assert throttled.wait(5)
            raise KeyboardInterrupt  # Ctrl-C while Big's pages wait out the pause
        return real(company, client, **kwargs)

    pools = []

    class Pool(ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pools.append(self)

    monkeypatch.setattr(cli, "_transport", _throttled_transport(handler))
    monkeypatch.setattr(cli, "fetch_company", fetch)
    monkeypatch.setattr(cli, "ThreadPoolExecutor", Pool)
    assert _fetch(tmp_path, companies, "--dry-run", "--workers", "8", "--per-host", "6") == 130
    sent = len(later_pages)
    for pool in pools:  # every pool thread must finish now, not when the pause ends
        joiner = threading.Thread(target=pool.shutdown, kwargs={"wait": True}, daemon=True)
        joiner.start()
        joiner.join(5)
        assert not joiner.is_alive()
    assert len(later_pages) == sent == 1


# ------------------------------------------------------------------ settings reach the commands


def _settings_file(monkeypatch, tmp_path, text):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text(text)
    for name in ("preferences.yaml", "profile.md"):
        (cfg / name).write_text((CONFIG_DIR / name).read_text())
    shutil.copytree(CONFIG_DIR / "kit", cfg / "kit")
    monkeypatch.setattr(cli.config, "DEFAULT_CONFIG_DIR", cfg)


@respx.mock
def test_fetch_concurrency_comes_from_settings_unless_a_flag_says_otherwise(tmp_path, monkeypatch, fixture_json):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n")
    respx.get(GH.format("examplecorp")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    seen = []
    real = cli._transport
    monkeypatch.setattr(cli, "_transport", lambda fetch, **kw: seen.append((kw, fetch)) or real(fetch, **kw))
    monkeypatch.setenv("JOBHUNT_FETCH_WORKERS", "7")
    _settings_file(monkeypatch, tmp_path, "fetch:\n  per_host: 3\n")
    assert _fetch(tmp_path, companies, "--dry-run") == 0
    assert seen[-1][0] == {"workers": 7, "per_host": 3}
    assert _fetch(tmp_path, companies, "--dry-run", "--workers", "2") == 0
    assert seen[-1][0] == {"workers": 2, "per_host": 3}


@respx.mock
def test_prune_threshold_comes_from_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNT_FETCH_PRUNE_AFTER_404S", "1")
    respx.get(GH.format("live")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))
    companies = _two_company_config(tmp_path)
    assert _fetch(tmp_path, companies) == 0
    assert "Dead" not in companies.read_text()  # gone after one 404, not three


def test_data_dir_comes_from_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOBHUNT_PATHS_DATA_DIR", str(tmp_path / "elsewhere"))
    assert cli.main(["list"]) == 0
    assert "0 scored job(s)" in capsys.readouterr().out
    args = cli.build_parser().parse_args(["list"])
    assert cli._resolve(args, cli.settings.load()).data_dir == tmp_path / "elsewhere"


def test_invalid_settings_are_a_friendly_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOBHUNT_FETCH_WORKERS", "lots")
    assert cli.main(["--data-dir", str(tmp_path), "list"]) == 2
    assert "JOBHUNT_FETCH_WORKERS" in capsys.readouterr().err


@respx.mock
def test_fetch_tuning_comes_from_settings(tmp_path, monkeypatch, fixture_json):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n")
    route = respx.get(GH.format("examplecorp")).mock(return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json")))
    for key, value in {"START_PER_HOST": "1", "MAX_RETRY_AFTER": "7", "COOLDOWN": "0.5", "BREAKER": "2",
                       "TIMEOUT": "9", "USER_AGENT": "test-agent/1", "TRANSIENT_RETRIES": "1",
                       "MAX_RATE": '{"greenhouse": 4}'}.items():
        monkeypatch.setenv(f"JOBHUNT_FETCH_{key}", value)
    transports, runners = [], []
    real_transport, real_runner = throttle.ThrottledTransport, cli.BoardRunner
    monkeypatch.setattr(throttle, "ThrottledTransport", lambda **kw: transports.append(kw) or real_transport(**kw))
    monkeypatch.setattr(cli, "BoardRunner", lambda *a, **kw: runners.append(kw) or real_runner(*a, **kw))
    assert _fetch(tmp_path, companies, "--dry-run") == 0
    (kw,) = transports
    assert (kw["start"], kw["max_retry_after"], kw["cooldown"]) == (1, 7, 0.5)
    assert kw["transient_retries"] == 1
    assert kw["max_rate"] == {"greenhouse": 4.0}
    assert runners[0]["breaker"] == 2
    request = route.calls.last.request
    assert request.headers["User-Agent"] == "test-agent/1"
    assert request.extensions["timeout"]["read"] == 9


def _one_job(tmp_path, job):
    storage.append_jsonl(tmp_path / "jobs.jsonl", job.model_dump())


def test_score_token_budget_and_body_length_come_from_settings(tmp_path, platform_director_job, monkeypatch):
    monkeypatch.setenv("JOBHUNT_LLM_SCORE_MAX_TOKENS", "123")
    monkeypatch.setenv("JOBHUNT_LLM_BODY_CHARS", "40")
    _one_job(tmp_path, platform_director_job)
    reply = make_completer({"score": 6, "rationale": "ok", "suggested_modules": []})
    calls = []

    def complete(system, user, max_tokens):
        calls.append((user, max_tokens))
        return reply(system, user)

    monkeypatch.setattr(cli, "_completer", lambda _=None: complete)
    assert cli.main(["--data-dir", str(tmp_path), "score"]) == 0
    ((user, max_tokens),) = calls
    body = platform_director_job.body
    assert max_tokens == 123
    assert body[:40] in user and body[:41] not in user


def test_letter_token_budget_body_length_and_output_dir_come_from_settings(tmp_path, scored_job, monkeypatch):
    monkeypatch.setenv("JOBHUNT_LLM_LETTER_MAX_TOKENS", "77")
    monkeypatch.setenv("JOBHUNT_LLM_BODY_CHARS", "40")
    monkeypatch.setenv("JOBHUNT_PATHS_OUTPUT_DIR", str(tmp_path / "letters"))
    storage.append_jsonl(tmp_path / "data" / "scores.jsonl", scored_job.model_dump())
    calls = []

    def complete(system, user, max_tokens):
        calls.append((user, max_tokens))
        return LETTER_REPLY

    monkeypatch.setattr(cli, "_completer", lambda _=None: complete)
    assert cli.main(["--data-dir", str(tmp_path / "data"), "letter"]) == 0
    ((user, max_tokens),) = calls
    body = scored_job.job.body
    assert max_tokens == 77
    assert body[:40] in user and body[:41] not in user
    assert storage.lettered_job_keys(tmp_path / "letters") == {scored_job.job.key}


def test_a_settings_edit_mid_run_does_not_change_the_recorded_model(tmp_path, platform_director_job, monkeypatch):
    _settings_file(monkeypatch, tmp_path, "llm:\n  backend: claude-code\n  model: opus\n")
    for job in (platform_director_job, platform_director_job.model_copy(update={"external_id": "second"})):
        _one_job(tmp_path, job)
    # the user edits settings.yaml for the next run while this one is still scoring
    edits = iter(["llm:\n  backend: claude-code\n  model: haiku\n", "llm:\n  model: [oops\n"])
    reply = make_completer({"score": 6, "rationale": "ok", "suggested_modules": []})

    def complete(system, user, max_tokens):
        (tmp_path / "cfg" / "settings.yaml").write_text(next(edits))
        return reply(system, user)

    given = []
    monkeypatch.setattr(cli, "_completer", lambda llm_settings: given.append(llm_settings) or complete)
    assert cli.main(["--data-dir", str(tmp_path), "score"]) == 0
    assert given[0].model == "opus"  # the completer is built from the run's settings
    assert [s.score.model for s in storage.load_scores(tmp_path)] == ["claude-code:opus"] * 2


def test_a_settings_edit_mid_run_does_not_change_the_letters_model(tmp_path, scored_job, monkeypatch):
    _settings_file(monkeypatch, tmp_path, "llm:\n  backend: claude-code\n  model: opus\n")
    _two_scored_jobs(tmp_path, scored_job)
    # the user edits settings.yaml for the next run while this one is still writing letters
    edits = iter(["llm:\n  backend: claude-code\n  model: haiku\n", "llm:\n  model: [oops\n"])

    def complete(system, user, max_tokens):
        (tmp_path / "cfg" / "settings.yaml").write_text(next(edits))
        return LETTER_REPLY

    monkeypatch.setattr(cli, "_completer", lambda llm_settings: complete)
    assert _letter(tmp_path) == 0
    headers = [p.read_text().splitlines()[0] for p in (tmp_path / "out").glob("*.md")]
    assert len(headers) == 2
    assert all(" | claude-code:opus | " in h for h in headers)


# --------------------------------------------------------------------------- saving as it goes


def _three_boards(tmp_path):
    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n"
        "  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n\n"
        "  - name: Dead\n    ats: greenhouse\n    slug: dead\n\n"
        "  - name: Last\n    ats: greenhouse\n    slug: last\n"
    )
    return p


def _progress(tmp_path):
    return tmp_path / "data" / "fetch_progress.txt"


@respx.mock
def test_fetch_saves_each_board_as_it_finishes(tmp_path, fixture_json, monkeypatch):
    respx.get(GH.format("examplecorp")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))
    data = tmp_path / "data"
    on_disk = {}

    def fetch(company, client, **kwargs):  # Last: look at the disk while the run is still going
        assert company.slug == "last"
        for _ in range(500):  # the main thread records the earlier boards meanwhile
            if _progress(tmp_path).exists() and len(_progress(tmp_path).read_text().split()) == 2:
                break
            time.sleep(0.01)
        on_disk["jobs"] = [j.title for j in storage.load_jobs(data)]
        on_disk["seen"] = len(storage.SeenSet(data / "seen.json"))
        on_disk["progress"] = _progress(tmp_path).read_text().split()
        return []

    real = cli.fetch_company
    monkeypatch.setattr(
        cli, "fetch_company",
        lambda company, client, **kw: (fetch if company.slug == "last" else real)(company, client, **kw),
    )
    assert _fetch(tmp_path, _three_boards(tmp_path), "--per-host", "1") == 0
    assert on_disk == {
        "jobs": ["Director of Platform Engineering"],
        "seen": 1,
        "progress": ["greenhouse:examplecorp", "greenhouse:dead"],  # a gone board counts as done
    }
    assert not _progress(tmp_path).exists()  # a finished run leaves no progress behind
    assert len(storage.load_jobs(data)) == 1  # recorded once, not again at the end


@respx.mock
def test_fetch_saves_a_boards_misses_before_marking_it_done(tmp_path, fixture_json, monkeypatch):
    # After a hard kill, --resume skips the boards marked done, so their misses must be on disk.
    respx.get(GH.format("examplecorp")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))
    misses = _misses(tmp_path)
    misses.counts = {"greenhouse:examplecorp": 2}
    misses.save()
    on_disk = {}

    def fetch(company, client, **kwargs):  # Last: look at the disk while the run is still going
        for _ in range(500):
            if _progress(tmp_path).exists() and len(_progress(tmp_path).read_text().split()) == 2:
                break
            time.sleep(0.01)
        on_disk["misses"] = _misses(tmp_path).counts
        return []

    real = cli.fetch_company
    monkeypatch.setattr(
        cli, "fetch_company",
        lambda company, client, **kw: (fetch if company.slug == "last" else real)(company, client, **kw),
    )
    assert _fetch(tmp_path, _three_boards(tmp_path), "--per-host", "1") == 0
    assert on_disk == {"misses": {"greenhouse:dead": 1}}  # the clear and the miss, both saved


@respx.mock
def test_an_interrupted_fetch_keeps_its_progress_and_resume_skips_those_boards(
    tmp_path, fixture_json, monkeypatch, capsys
):
    first = respx.get(GH.format("examplecorp")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    dead = respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))
    last = respx.get(GH.format("last")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    companies = _three_boards(tmp_path)
    real = cli.fetch_company
    _failing_for("last", KeyboardInterrupt(), monkeypatch)

    assert _fetch(tmp_path, companies, "--per-host", "1") == 130
    assert "jobhunt fetch --resume" in capsys.readouterr().err
    assert _progress(tmp_path).read_text().split() == ["greenhouse:examplecorp", "greenhouse:dead"]
    assert _misses(tmp_path).counts == {"greenhouse:dead": 1}

    monkeypatch.setattr(cli, "fetch_company", real)
    assert _fetch(tmp_path, companies, "--resume") == 0
    assert (first.call_count, dead.call_count, last.call_count) == (1, 1, 1)
    assert _misses(tmp_path).counts == {"greenhouse:dead": 1}  # not counted twice in one run
    assert not _progress(tmp_path).exists()
    assert len(storage.load_jobs(tmp_path / "data")) == 1


@respx.mock
def test_resume_with_nothing_to_resume_fetches_everything(tmp_path, fixture_json, capsys):
    route = respx.get(GH.format("examplecorp")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n")
    assert _fetch(tmp_path, companies, "--resume") == 0
    assert route.call_count == 1
    assert "nothing to resume" in capsys.readouterr().err


@respx.mock
def test_a_fresh_fetch_forgets_an_earlier_interrupted_one(tmp_path, fixture_json):
    route = respx.get(GH.format("examplecorp")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n")
    _progress(tmp_path).parent.mkdir(parents=True)
    _progress(tmp_path).write_text("greenhouse:examplecorp\n")
    assert _fetch(tmp_path, companies) == 0
    assert route.call_count == 1


def test_resume_cannot_be_combined_with_company(tmp_path, capsys):
    assert _fetch(tmp_path, _three_boards(tmp_path), "--resume", "--company", "Dead") == 2
    assert "--resume" in capsys.readouterr().err


@respx.mock
def test_a_one_company_fetch_leaves_an_interrupted_run_alone(tmp_path, fixture_json):
    respx.get(GH.format("examplecorp")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    _progress(tmp_path).parent.mkdir(parents=True)
    _progress(tmp_path).write_text("greenhouse:dead\n")
    assert _fetch(tmp_path, _three_boards(tmp_path), "--company", "ExampleCorp") == 0
    assert _progress(tmp_path).read_text() == "greenhouse:dead\n"


@respx.mock
def test_a_dry_run_resume_reads_progress_but_writes_none(tmp_path, fixture_json):
    route = respx.get(GH.format("examplecorp")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    respx.get(GH.format("last")).mock(return_value=httpx.Response(200, json={"jobs": []}))
    _progress(tmp_path).parent.mkdir(parents=True)
    _progress(tmp_path).write_text("greenhouse:examplecorp\ngreenhouse:dead\n")
    assert _fetch(tmp_path, _three_boards(tmp_path), "--resume", "--dry-run") == 0
    assert route.call_count == 0
    assert _progress(tmp_path).read_text() == "greenhouse:examplecorp\ngreenhouse:dead\n"


@pytest.mark.parametrize("cmd", ["fetch", "run"])
def test_resume_flag(cmd):
    assert cli.build_parser().parse_args([cmd, "--resume"]).resume is True
    assert cli.build_parser().parse_args([cmd]).resume is False


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM])
def test_hangup_and_terminate_interrupt_like_ctrl_c(signum):
    before = signal.getsignal(signum)
    with pytest.raises(KeyboardInterrupt), cli._interrupt_on_signals():
        os.kill(os.getpid(), signum)
        for _ in range(1000):  # the handler runs on the main thread between bytecodes
            pass
    assert signal.getsignal(signum) == before


def test_after_a_hangup_output_goes_nowhere_until_the_end(capsys):
    stdout = sys.stdout
    with contextlib.suppress(KeyboardInterrupt), cli._interrupt_on_signals():
        try:
            os.kill(os.getpid(), signal.SIGHUP)
            for _ in range(1000):
                pass
        except KeyboardInterrupt:
            assert sys.stdout is not stdout  # the terminal is gone: printing must not fail
            print("lost")
            raise
    assert sys.stdout is stdout
    assert "lost" not in capsys.readouterr().out


def test_a_second_signal_does_not_interrupt_the_save():
    saved = False
    with pytest.raises(KeyboardInterrupt), cli._interrupt_on_signals():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(1000):
                pass
        except KeyboardInterrupt:
            os.kill(os.getpid(), signal.SIGTERM)  # while saving: ignored
            for _ in range(1000):
                pass
            saved = True
            raise
    assert saved


def test_after_ctrl_c_a_hangup_does_not_interrupt_the_save(capsys):
    stdout = sys.stdout
    before = signal.getsignal(signal.SIGINT)
    saved = False
    with pytest.raises(KeyboardInterrupt), cli._interrupt_on_signals():
        try:
            os.kill(os.getpid(), signal.SIGINT)
            for _ in range(1000):
                pass
        except KeyboardInterrupt:
            os.kill(os.getpid(), signal.SIGHUP)  # terminal closed while saving: ignored
            for _ in range(1000):
                pass
            assert sys.stdout is not stdout  # but the terminal is still gone
            print("lost")
            saved = True
            raise
    assert saved
    assert sys.stdout is stdout
    assert "lost" not in capsys.readouterr().out
    assert signal.getsignal(signal.SIGINT) == before


def test_an_ignored_hangup_stays_ignored():
    # `nohup jobhunt fetch` must keep running when the terminal closes.
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        finished = False
        with cli._interrupt_on_signals():
            os.kill(os.getpid(), signal.SIGHUP)
            for _ in range(1000):
                pass
            finished = True
        assert finished
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)


@respx.mock
def test_a_hangup_mid_fetch_saves_and_keeps_progress(tmp_path, fixture_json, monkeypatch):
    respx.get(GH.format("examplecorp")).mock(
        return_value=httpx.Response(200, json=fixture_json("greenhouse_jobs.json"))
    )
    respx.get(GH.format("dead")).mock(return_value=httpx.Response(404))
    release = threading.Event()
    real = cli.fetch_company

    def fetch(company, client, **kwargs):
        if company.slug == "last":
            for _ in range(500):  # once the earlier boards are saved, so the test is deterministic
                if _progress(tmp_path).exists() and len(_progress(tmp_path).read_text().split()) == 2:
                    break
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGHUP)  # VS Code closed under the run
            release.wait(5)
            return []
        return real(company, client, **kwargs)

    monkeypatch.setattr(cli, "fetch_company", fetch)
    try:
        assert _fetch(tmp_path, _three_boards(tmp_path), "--per-host", "1") == 130
    finally:
        release.set()
    assert [j.title for j in storage.load_jobs(tmp_path / "data")] == ["Director of Platform Engineering"]
    assert _progress(tmp_path).read_text().split() == ["greenhouse:examplecorp", "greenhouse:dead"]
    assert _misses(tmp_path).counts == {"greenhouse:dead": 1}


def test_a_posting_listed_twice_on_one_board_is_recorded_once(tmp_path, platform_director_job, monkeypatch):
    # Workable lists a posting once per location, every copy with the same shortcode.
    elsewhere = platform_director_job.model_copy(update={"location": "Austin, Texas, United States"})
    monkeypatch.setattr(cli, "fetch_company", lambda company, client, **kw: [platform_director_job, elsewhere])
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - name: ExampleCorp\n    ats: greenhouse\n    slug: examplecorp\n")
    assert _fetch(tmp_path, companies) == 0
    assert [j.location for j in storage.load_jobs(tmp_path / "data")] == [platform_director_job.location]
