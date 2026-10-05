"""Pre-filter rules. Pure functions; no I/O."""

from __future__ import annotations

import pytest

from jobhunt import filter as jfilter
from jobhunt.schema import Job


def _job(title: str, location: str = "", remote=None, body: str = "") -> Job:
    return Job(
        source="greenhouse",
        company="X",
        company_slug="x",
        external_id="1",
        title=title,
        location=location,
        remote=remote,
        url="https://example.com/1",
        body=body,
    )


def test_director_platform_in_seattle_passes(prefs):
    r = jfilter.evaluate(_job("Director of Platform Engineering", "Seattle, WA"), prefs)
    assert r.passed, r.reason


def test_ic_title_rejected(prefs):
    r = jfilter.evaluate(_job("Senior Software Engineer", "Seattle, WA", body="platform"), prefs)
    assert not r.passed
    assert "level" in r.reason


def test_sales_director_rejected(prefs):
    r = jfilter.evaluate(_job("Director of Sales Engineering", "Seattle, WA", body="infrastructure"), prefs)
    assert not r.passed
    assert "sales" in r.reason


def test_director_without_domain_keyword_rejected(prefs):
    r = jfilter.evaluate(_job("Director of Engineering", "Seattle, WA", body="Figma and brand."), prefs)
    assert not r.passed
    assert "domain" in r.reason


def test_remote_us_passes(prefs):
    r = jfilter.evaluate(_job("VP, Infrastructure", "Remote - US", remote=True), prefs)
    assert r.passed


def test_foreign_remote_rejected_even_if_remote(prefs):
    r = jfilter.evaluate(_job("VP, Infrastructure", "London (Remote - UK)", remote=True), prefs)
    assert not r.passed
    assert "rejected" in r.reason


def test_unknown_location_outside_region_rejected(prefs):
    r = jfilter.evaluate(_job("Director, SRE", "Austin, TX", remote=None, body="on-site only"), prefs)
    assert not r.passed
    assert "location" in r.reason


def test_apply_splits_passed_and_rejected(prefs):
    jobs = [
        _job("Director of Platform Engineering", "Seattle, WA"),
        _job("Director of Infrastructure (Intern)", "Seattle, WA"),
    ]
    passed, rejected = jfilter.apply(jobs, prefs)
    assert [j.title for j in passed] == ["Director of Platform Engineering"]
    assert len(rejected) == 1 and "intern" in rejected[0].reason


@pytest.mark.parametrize(
    ("term", "text"),
    [
        ("intern", "Director of Infrastructure (Intern)"),
        ("AI", "Head of AI Platform"),
        ("AI", "Director, AI/ML"),
        ("vp", "VP, Infrastructure"),
        ("fp&a", "Director, FP&A"),
        ("remote (us)", "Remote (US)"),
        ("head of product,", "Head of Product, Connect"),
        ("senior manager", "Senior  Manager, Electrical Engineering"),  # doubled space
        # plurals
        ("platform", "Vice President, Mission Data Platforms"),
        ("tax", "Director of Taxes"),
        # padding left over from substring days still means "the word wa"
        (" wa ", "Seattle WA USA"),
        ("wa,", "Seattle, WA, US"),
        # a trailing * matches any ending
        ("recruit*", "Director of Recruiting"),
        ("recruit*", "Head of Recruitment"),
    ],
)
def test_term_matches(term, text):
    assert jfilter._any_in([term], text) == term


@pytest.mark.parametrize(
    ("term", "text"),
    [
        ("intern", "Director, Internal Developer Platform"),
        ("AI", "Director, High Availability"),
        ("AI", "Senior Manager, Maintenance and Training"),
        ("tax", "Director, Taxonomy"),
        ("sales", "Director, Salesforce Platform"),
        ("india", "Indianapolis, Indiana"),
        ("vp", "SVP, Product"),
        ("wa", "Washington, DC"),
        ("recruit", "Director of Recruiting"),
        ("head of product,", "Head of Production, Maritime"),
        ("*", "Director of Anything"),
        ("", "Director of Anything"),
    ],
)
def test_term_does_not_match_inside_other_words(term, text):
    assert jfilter._any_in([term], text) is None


def test_internal_platform_director_not_mistaken_for_intern(prefs):
    r = jfilter.evaluate(_job("Director, Internal Developer Platform", "Seattle, WA"), prefs)
    assert r.passed, r.reason


def _onsite(prefs, *terms):
    loc = prefs.location.model_copy(update={"onsite_accept_any": list(terms)})
    return prefs.model_copy(update={"location": loc})


def test_onsite_role_outside_onsite_region_rejected(prefs):
    # "united states" is accepted in general, but this role is explicitly on-site in New York
    job = _job("Director of Platform Engineering", "New York, NY, United States", remote=False)
    assert jfilter.evaluate(job, _onsite(prefs)).passed  # rule off: unchanged behavior
    r = jfilter.evaluate(job, _onsite(prefs, "seattle", "bellevue"))
    assert not r.passed
    assert "on-site" in r.reason and "New York" in r.reason


def test_onsite_role_inside_onsite_region_passes(prefs):
    job = _job("Director of Platform Engineering", "Bellevue, WA, United States", remote=False)
    assert jfilter.evaluate(job, _onsite(prefs, "seattle", "bellevue")).passed


@pytest.mark.parametrize("remote", [True, None])
def test_onsite_rule_ignores_remote_and_unknown(prefs, remote):
    job = _job("Director of Platform Engineering", "Austin, TX, United States", remote=remote)
    assert jfilter.evaluate(job, _onsite(prefs, "seattle")).passed


def test_example_preferences_turn_the_onsite_rule_on(prefs):
    assert "seattle" in prefs.location.onsite_accept_any
