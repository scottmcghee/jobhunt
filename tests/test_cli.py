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
    monkeypatch.setattr(cli, "_completer", lambda: lambda system, user, max_tokens: next(replies))

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

    monkeypatch.setattr(cli, "_completer", lambda: complete)
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

    monkeypatch.setattr(cli, "_completer", lambda: complete)
    assert _letter(tmp_path) == 0 and len(calls) == 2
    assert len(list((tmp_path / "out").glob("*.md"))) == 2
    assert storage.lettered_job_keys(tmp_path / "out") == {scored_job.job.key, second.job.key}
    assert _letter(tmp_path) == 0 and len(calls) == 2  # nothing regenerated


def test_letter_skips_a_lettered_job_whose_key_has_a_space(tmp_path, scored_job, monkeypatch):
    spaced = scored_job.model_copy(update={"job": scored_job.job.model_copy(update={"source": "ashby", "company_slug": "Some Co"})})
    storage.append_jsonl(tmp_path / "data" / "scores.jsonl", spaced.model_dump())
    monkeypatch.setattr(cli, "_completer", lambda: lambda system, user, max_tokens: LETTER_REPLY)
    assert _letter(tmp_path) == 0
    monkeypatch.setattr(cli, "_completer", lambda: pytest.fail)  # must not be called
    assert _letter(tmp_path) == 0


def test_letter_for_one_job_respects_existing_letters(tmp_path, scored_job, monkeypatch, capsys):
    first, _ = _two_scored_jobs(tmp_path, scored_job)
    monkeypatch.setattr(cli, "_completer", lambda: lambda system, user, max_tokens: LETTER_REPLY)
    _letter(tmp_path, "--job", first.job.key)
    capsys.readouterr()
    monkeypatch.setattr(cli, "_completer", lambda: pytest.fail)  # must not be called
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
def test_a_throttled_board_is_retried_not_skipped(tmp_path, fixture_json):
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
    args = cli.build_parser().parse_args([cmd])
    assert (args.workers, args.per_host) == (32, 6)
    args = cli.build_parser().parse_args([cmd, "--workers", "1", "--per-host", "1"])
    assert (args.workers, args.per_host) == (1, 1)


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
    import threading

    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n"
        "  - name: Slow\n    ats: greenhouse\n    slug: slow\n\n"
        "  - name: ExampleLever\n    ats: lever\n    slug: examplelever\n"
    )
    respx.get("https://api.lever.co/v0/postings/examplelever").mock(
        return_value=httpx.Response(200, json=fixture_json("lever_postings.json"))
    )
    lever_done = threading.Event()
    real = cli.fetch_company

    def fetch(company, client, **kwargs):
        if company.slug == "slow":
            assert lever_done.wait(5)
            raise KeyboardInterrupt  # Ctrl-C while the first board is still running
        jobs = real(company, client, **kwargs)
        lever_done.set()
        return jobs

    monkeypatch.setattr(cli, "fetch_company", fetch)
    assert _fetch(tmp_path, p) == 130
    out = capsys.readouterr().out
    assert "finished out of order" in out and "ExampleLever" in out
    assert [j.company for j in storage.load_jobs(tmp_path / "data")] == ["ExampleLever"]
