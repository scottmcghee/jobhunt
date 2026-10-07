"""Shared fixtures. Everything here is offline.

Tests read the committed templates in config.example/, never your personal config/, so they pass
on a fresh clone and don't break when you tune your own search.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunt import config, settings
from jobhunt.schema import Company, Job, Score, ScoredJob

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config.example"
LOCAL_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(autouse=True)
def _use_example_config(monkeypatch):
    """Code paths that fall back to the default config dir get the templates too."""
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", CONFIG_DIR)


@pytest.fixture(autouse=True)
def _no_settings_env(monkeypatch):
    """Settings from your shell (JOBHUNT_FETCH_WORKERS=...) don't leak in; a test sets its own."""
    for name in settings.env_names():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fixture_json():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text())

    return _load


@pytest.fixture
def gh_company() -> Company:
    return Company(name="ExampleCorp", ats="greenhouse", slug="examplecorp")


@pytest.fixture
def lever_company() -> Company:
    return Company(name="ExampleLever", ats="lever", slug="examplelever")


@pytest.fixture
def ashby_company() -> Company:
    return Company(name="ExampleAshby", ats="ashby", slug="exampleashby")


@pytest.fixture
def workday_company() -> Company:
    return Company(name="ExampleCorp", ats="workday", slug="examplecorp/External", datacenter="wd5")


@pytest.fixture
def smartrecruiters_company() -> Company:
    return Company(name="ExampleCorp", ats="smartrecruiters", slug="ExampleCorp")


@pytest.fixture
def workable_company() -> Company:
    return Company(name="ExampleCorp", ats="workable", slug="examplecorp")


@pytest.fixture
def bamboohr_company() -> Company:
    return Company(name="ExampleCorp", ats="bamboohr", slug="examplecorp")


@pytest.fixture
def amazon_company() -> Company:
    return Company(name="Amazon", ats="amazon", slug="USA")


@pytest.fixture
def eightfold_company() -> Company:
    return Company(name="ExampleCorp", ats="eightfold", slug="example.eightfold.ai")


@pytest.fixture
def oracle_company() -> Company:
    return Company(name="ExampleCorp", ats="oracle", slug="example.fa.us2.oraclecloud.com/CX_1")


@pytest.fixture
def apple_company() -> Company:
    return Company(name="Apple", ats="apple", slug="united-states-USA")


@pytest.fixture
def phenom_company() -> Company:
    return Company(name="Example Co", ats="phenom", slug="careers.example.com/us/en")


@pytest.fixture
def sf_company() -> Company:
    return Company(name="Example Co", ats="successfactors", slug="careers.example.com")


@pytest.fixture
def prefs() -> config.Preferences:
    return config.load_preferences(CONFIG_DIR / "preferences.yaml")


@pytest.fixture
def kit() -> config.Kit:
    return config.load_kit(CONFIG_DIR / "kit")


@pytest.fixture
def profile() -> str:
    return config.load_profile(CONFIG_DIR / "profile.md")


@pytest.fixture
def platform_director_job() -> Job:
    return Job(
        source="greenhouse",
        company="ExampleCorp",
        company_slug="examplecorp",
        external_id="1001",
        title="Director of Platform Engineering",
        location="Seattle, WA or Remote (US)",
        remote=True,
        url="https://boards.greenhouse.io/examplecorp/jobs/1001",
        body=(
            "Lead our infrastructure, developer experience, and SRE teams. "
            "Own AWS infrastructure and cloud cost. Lead a team of 20."
        ),
    )


@pytest.fixture
def scored_job(platform_director_job: Job) -> ScoredJob:
    return ScoredJob(
        job=platform_director_job,
        score=Score(
            score=8,
            rationale="Strong level and domain match; no Kubernetes mentioned.",
            strengths=["platform leadership", "AWS cost"],
            gaps=["none major"],
            suggested_modules=["build_infra_devx", "sre_from_nothing"],
            model="test-model",
        ),
    )


def make_completer(payload: dict):
    """Return a fake Completer that always answers with the given JSON payload."""
    calls: list[tuple[str, str]] = []

    def complete(system: str, user: str, max_tokens: int = 0) -> str:
        calls.append((system, user))
        return "Here you go:\n```json\n" + json.dumps(payload) + "\n```"

    complete.calls = calls  # type: ignore[attr-defined]
    return complete
