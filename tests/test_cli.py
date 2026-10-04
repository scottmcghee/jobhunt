"""End-to-end CLI path with mocked network and a fake model."""

from __future__ import annotations

import contextlib
import io

import httpx
import pytest
import respx

from jobhunt import cli, storage
from jobhunt.schema import Score, ScoredJob
from tests.conftest import make_completer


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
    monkeypatch.setattr(cli, "_completer", lambda: fake_score)
    rc = cli.main(["--data-dir", str(data_dir), "score"])
    assert rc == 0
    assert storage.load_scores(data_dir)[0].score.score == 8

    # letter with a fake model
    fake_letter = make_completer(
        {"custom_opening_sentence": "Specific opener.", "custom_closing_sentence": "Specific closer."}
    )
    monkeypatch.setattr(cli, "_completer", lambda: fake_letter)
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
    monkeypatch.setattr(cli, "_completer", lambda: lambda system, user, max_tokens: next(replies))

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
