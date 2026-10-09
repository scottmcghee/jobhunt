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
    assert "hybrid" in r.reason  # remote=False also covers hybrid postings


def test_onsite_list_replaces_accept_any_for_onsite_roles(prefs):
    # Renton is only in the on-site list, not accept_any; for an on-site role that is enough
    assert "renton" not in prefs.location.accept_any
    job = _job("Director of Platform Engineering", "Renton", remote=False)
    assert not jfilter.evaluate(job, _onsite(prefs)).passed  # rule off: accept_any decides
    assert jfilter.evaluate(job, _onsite(prefs, "renton")).passed


def test_reject_any_wins_over_onsite_rule(prefs):
    assert "india" in prefs.location.reject_any
    job = _job("Director of Platform Engineering", "Bengaluru, India", remote=False)
    r = jfilter.evaluate(job, _onsite(prefs, "india"))
    assert not r.passed
    assert "rejected term" in r.reason


def test_an_onsite_role_in_an_accepted_place_and_a_rejected_one_passes(prefs):
    job = _job("Director of Platform Engineering", "Seattle, WA / India", remote=False)
    assert jfilter.evaluate(job, _onsite(prefs, "seattle")).passed  # Seattle is one of its places


def test_onsite_role_inside_onsite_region_passes(prefs):
    job = _job("Director of Platform Engineering", "Bellevue, WA, United States", remote=False)
    assert jfilter.evaluate(job, _onsite(prefs, "seattle", "bellevue")).passed


@pytest.mark.parametrize("remote", [True, None])
def test_onsite_rule_ignores_remote_and_unknown(prefs, remote):
    job = _job("Director of Platform Engineering", "Austin, TX, United States", remote=remote)
    assert jfilter.evaluate(job, _unknown_is_onsite(_onsite(prefs, "seattle"), False)).passed


def test_example_preferences_turn_the_onsite_rule_on(prefs):
    assert "seattle" in prefs.location.onsite_accept_any


def _unknown_is_onsite(prefs, on=True):
    loc = prefs.location.model_copy(update={"unknown_remote_is_onsite": on})
    return prefs.model_copy(update={"location": loc})


def test_unknown_remote_in_a_named_city_is_treated_as_onsite(prefs):
    # Remote status unknown, a city far from the on-site list, and nothing says remote:
    # "united states" in accept_any shouldn't be enough.
    job = _job("Director of Platform Engineering", "New York, New York, United States")
    assert jfilter.evaluate(job, _unknown_is_onsite(prefs, False)).passed  # rule off: unchanged
    r = jfilter.evaluate(job, _unknown_is_onsite(prefs))
    assert not r.passed
    assert "unknown" in r.reason and "New York" in r.reason


def test_unknown_remote_inside_the_onsite_region_passes(prefs):
    job = _job("Director of Platform Engineering", "Bellevue, WA, United States")
    assert jfilter.evaluate(job, _unknown_is_onsite(prefs)).passed


@pytest.mark.parametrize(
    ("location", "body"),
    [
        ("New York, NY, United States", "This role is fully remote within the US."),
        ("New York, NY, United States", "x" * 5000 + " Remote candidates welcome."),  # past 2,000
        ("New York, NY, United States", "You can work from home."),
        ("New York, NY, United States", "You'll be working from home."),
        ("New York, NY, United States", "The team works from home."),
        ("New York, NY, United States", "Work-from-home eligible."),
        ("New York, NY, United States", "Telecommuting is available."),
        ("New York, NY, United States", "You may work remotely."),
        ("New York, NY, United States", "This job can be performed remotely."),
        ("New York, NY, United States (Remote)", ""),
        ("AMER - United States - Washington - Offsite/Home", ""),
        ("United States - Home Based", ""),
        ("Texas, United States of America (Virtual)", ""),
    ],
)
def test_unknown_remote_that_mentions_remote_anywhere_passes(prefs, location, body):
    job = _job("Director of Platform Engineering", location, body=f"platform. {body}")
    assert jfilter.evaluate(job, _unknown_is_onsite(prefs)).passed


@pytest.mark.parametrize("location", ["United States", "USA", " united states of america "])
def test_unknown_remote_with_only_a_country_is_left_to_accept_any(prefs, location):
    assert "usa" in prefs.location.country_wide_any
    job = _job("Director of Platform Engineering", location)
    assert jfilter.evaluate(job, _unknown_is_onsite(prefs)).passed


def test_a_country_inside_a_longer_location_is_not_country_wide(prefs):
    job = _job("Director of Platform Engineering", "Tampa Florida United States; USA")
    assert not jfilter.evaluate(job, _unknown_is_onsite(prefs)).passed


def test_home_counts_as_remote_only_in_the_location(prefs):
    body = "platform. Our home is New York, and our offsite is in June."
    job = _job("Director of Platform Engineering", "New York, NY, United States", body=body)
    assert not jfilter.evaluate(job, _unknown_is_onsite(prefs)).passed


def test_unknown_remote_with_remote_in_the_title_passes(prefs):
    job = _job("Director of Platform Engineering (Remote)", "New York, NY, United States")
    assert jfilter.evaluate(job, _unknown_is_onsite(prefs)).passed


def test_virtual_counts_as_remote_only_in_the_location(prefs):
    body = "platform. You will run virtual machines at scale."
    job = _job("Director of Platform Engineering", "New York, NY, United States", body=body)
    assert not jfilter.evaluate(job, _unknown_is_onsite(prefs)).passed


def test_remote_words_dont_help_when_remote_isnt_allowed(prefs):
    loc = prefs.location.model_copy(update={"unknown_remote_is_onsite": True, "allow_remote": False})
    job = _job("Director of Platform Engineering", "New York, NY, United States", body="remote ok")
    r = jfilter.evaluate(job, prefs.model_copy(update={"location": loc}))
    assert not r.passed
    assert "not mentioned" not in r.reason
    assert "remote roles not allowed" in r.reason and "location 'New York" in r.reason


def test_unknown_remote_rule_needs_an_onsite_list(prefs):
    job = _job("Director of Platform Engineering", "New York, New York, United States")
    assert jfilter.evaluate(job, _unknown_is_onsite(_onsite(prefs))).passed


def test_unknown_remote_rule_leaves_known_remote_alone(prefs):
    job = _job("Director of Platform Engineering", "New York, NY, United States", remote=True)
    assert jfilter.evaluate(job, _unknown_is_onsite(prefs)).passed


def test_example_preferences_turn_the_unknown_remote_rule_on(prefs):
    assert prefs.location.unknown_remote_is_onsite


def test_unknown_remote_rule_is_off_by_default():
    from jobhunt.config import LocationRules

    rules = LocationRules()
    assert rules.unknown_remote_is_onsite is False and rules.country_wide_any == []


# ------------------------------------------------------------------ extra target words by tag

def test_a_tagged_board_accepts_its_extra_target_words(prefs):
    assert prefs.title.include_for_tags == {"big-tech": ["manager"]}
    job = _job("Observability SRE Manager - Services Engineering", "Seattle, WA", body="platform")
    assert jfilter.check_title(job, prefs) == "title lacks a target level keyword"
    assert jfilter.check_title(job, prefs, tags=["big-tech"]) is None
    assert jfilter.check_title(job, prefs, tags=["Big-Tech"]) is None  # tags compare without case
    assert jfilter.check_title(job, prefs, tags=["saas"]) == "title lacks a target level keyword"


def test_exclusions_still_apply_to_extra_target_words(prefs):
    job = _job("Senior Product Manager, Platform", "Seattle, WA", body="platform")
    assert "product manager" in jfilter.check_title(job, prefs, tags=["big-tech"])


def test_targets_are_the_include_words_plus_the_tags_extras(prefs):
    base = prefs.title.must_include_any
    assert prefs.title.targets() == base
    assert prefs.title.targets(["saas", "big-tech"]) == [*base, "manager"]
    rules = prefs.title.model_copy(update={"include_for_tags": {"a": ["manager", "director"], "b": ["manager"]}})
    assert rules.targets(["a", "b"]) == [*base, "manager"]  # no repeats
    rules = prefs.title.model_copy(update={"include_for_tags": {"Big-Tech": ["manager"]}})
    assert rules.targets(["big-tech"]) == [*base, "manager"]  # the key side ignores case too


def test_extras_never_narrow_an_empty_include_list(prefs):
    """No include words means any title passes; a tag's extras must not turn that into a filter."""
    rules = prefs.title.model_copy(update={"must_include_any": [], "include_for_tags": {"big-tech": ["manager"]}})
    assert rules.targets(["big-tech"]) == []  # so search sources still run their unfiltered search
    open_prefs = prefs.model_copy(update={"title": rules})
    job = _job("Principal Engineer", "Seattle, WA", body="platform")
    assert jfilter.check_title(job, open_prefs, tags=["big-tech"]) is None


def test_evaluate_and_apply_pass_the_tags_on(prefs):
    job = _job("Observability SRE Manager", "Seattle, WA", body="platform")
    assert not jfilter.evaluate(job, prefs).passed
    assert jfilter.evaluate(job, prefs, tags=["big-tech"]).passed
    passed, rejected = jfilter.apply([job], prefs, tags=["big-tech"])
    assert passed == [job] and rejected == []


def _domain(prefs, *terms, exempt=()):
    dom = prefs.domain.model_copy(
        update={"title_must_include_any": list(terms), "title_exempt_tags": list(exempt)}
    )
    return prefs.model_copy(update={"domain": dom})


def test_title_must_name_a_function_when_the_rule_is_set(prefs):
    job = _job("Director of Operations", "Seattle, WA", body="We run on cloud infrastructure.")
    assert jfilter.evaluate(job, _domain(prefs)).passed  # rule off: the body's domain words are enough
    r = jfilter.evaluate(job, _domain(prefs, "engineering", "software"))
    assert not r.passed and "title" in r.reason and "domain" in r.reason
    ok = _job("Director of Software Engineering", "Seattle, WA", body="cloud infrastructure")
    assert jfilter.evaluate(ok, _domain(prefs, "engineering", "software")).passed


def test_boards_with_an_exempt_tag_skip_the_title_function_rule(prefs):
    job = _job("Director of Operations", "Seattle, WA", body="cloud infrastructure")
    strict = _domain(prefs, "engineering", exempt=["public-sector"])
    assert not jfilter.evaluate(job, strict, tags=["big-tech"]).passed
    assert jfilter.evaluate(job, strict, tags=["Public-Sector"]).passed  # tags match in any case


def test_the_body_domain_rule_still_applies_with_the_title_rule(prefs):
    job = _job("Director of Engineering", "Seattle, WA", body="Figma and brand.")
    r = jfilter.evaluate(job, _domain(prefs, "engineering"))
    assert not r.passed and "body" in r.reason


def _reject(prefs, *terms):
    loc = prefs.location.model_copy(update={"reject_any": list(terms)})
    return prefs.model_copy(update={"location": loc})


def test_a_rejected_place_is_kept_when_the_location_also_names_an_accepted_one(prefs):
    p = _reject(prefs, "canada", "london")
    both = _job("VP, Infrastructure", "Remote, Canada; Remote, United States", remote=True)
    assert jfilter.evaluate(both, p).passed
    only = _job("VP, Infrastructure", "Remote, Canada", remote=True)
    r = jfilter.evaluate(only, p)
    assert not r.passed and "canada" in r.reason
    assert not jfilter.evaluate(_job("VP, Infrastructure", "London", remote=True), p).passed
