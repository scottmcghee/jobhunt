"""Deterministic pre-filter. No network, no LLM. Pure functions over Job + Preferences.

The point of this stage is to make the expensive scoring step cheap by throwing
out the obvious misses first — and to do it in a way that is fully testable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache

from jobhunt.config import Preferences
from jobhunt.schema import Job


@dataclass(frozen=True)
class FilterResult:
    job: Job
    passed: bool
    reason: str  # empty when passed


@cache
def _pattern(term: str) -> re.Pattern[str] | None:
    """Compile a term to match as a whole word or phrase, never inside a longer word.

    A plural ending is allowed ("platform" finds "platforms"). A trailing ``*`` allows any
    ending ("recruit*" finds "recruiting"). A term with nothing to match compiles to None.
    """
    words = term.strip().rstrip("*").split()
    if not words:
        return None
    ending = r"\w*" if term.strip().endswith("*") else r"(?:e?s)?(?!\w)"
    return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + ending, re.I)


def _any_in(needles: list[str], haystack: str) -> str | None:
    """Return the first needle found in haystack as a word (case-insensitive), else None."""
    for n in needles:
        pattern = _pattern(n)
        if pattern and pattern.search(haystack):
            return n
    return None


def check_title(job: Job, prefs: Preferences, tags: Iterable[str] = ()) -> str | None:
    """``tags`` are the board's; a tag in ``include_for_tags`` adds target words."""
    rules = prefs.title
    targets = rules.targets(tags)
    if targets and not _any_in(targets, job.title):
        return "title lacks a target level keyword"
    hit = _any_in(rules.must_exclude_any, job.title)
    if hit:
        return f"title contains excluded term '{hit}'"
    return None


def check_domain(job: Job, prefs: Preferences, tags: Iterable[str] = ()) -> str | None:
    """``tags`` are the board's; one in ``title_exempt_tags`` skips the title rule."""
    rules = prefs.domain
    if rules.title_rule_applies(tags) and not _any_in(rules.title_must_include_any, job.title):
        return "no domain keyword in title"
    if not rules.must_include_any:
        return None
    text = f"{job.title}\n{job.body}"
    if not _any_in(rules.must_include_any, text):
        return "no domain keyword in title or body"
    return None


# Words that say a posting can be done remotely, for roles whose remote flag is unknown. The
# location ones are too common in a body ("our home", "team offsites", "virtual machines") to
# count there.
REMOTE_CUES = [
    "remote*",
    "work from home",
    "working from home",
    "works from home",
    "work-from-home",
    "telecommut*",
]
REMOTE_LOCATION_CUES = ["home", "home based", "home-based", "offsite", "virtual"]


def _probably_onsite(job: Job, prefs: Preferences) -> bool:
    """Remote unknown, nothing mentions remote, and the location names more than a country."""
    rules = prefs.location
    if job.remote is not None or not rules.unknown_remote_is_onsite:
        return False
    if rules.allow_remote and (
        _any_in(REMOTE_CUES, f"{job.title}\n{job.location}\n{job.body}")
        or _any_in(REMOTE_LOCATION_CUES, job.location)
    ):
        return False
    country_wide = {c.strip().lower() for c in rules.country_wide_any}
    return job.location.strip().lower() not in country_wide


def check_location(job: Job, prefs: Preferences) -> str | None:
    rules = prefs.location
    loc_text = f"{job.location} {job.title}".lower()

    hit = _any_in(rules.reject_any, loc_text)
    if hit and not _any_in(rules.accept_any, loc_text):
        return f"location matches rejected term '{hit}'"

    if rules.allow_remote and job.remote:
        return None
    if rules.onsite_accept_any and (job.remote is False or _probably_onsite(job, prefs)):
        # On-site or hybrid (or probably so): this list replaces accept_any, so a broad term
        # like "united states" isn't enough.
        if not _any_in(rules.onsite_accept_any, loc_text):
            where = job.location or "unknown"
            if job.remote is False:
                return f"on-site or hybrid role in '{where}', outside onsite_accept_any"
            why = "not mentioned" if rules.allow_remote else "remote roles not allowed"
            return f"remote unknown and {why}; location '{where}' outside onsite_accept_any"
        return None
    if _any_in(rules.accept_any, loc_text):
        return None
    if job.remote is None and "remote" in job.body.lower()[:2000] and rules.allow_remote:
        # Location unknown, but the body talks about remote near the top. Let it through;
        # the scorer will see the full text.
        return None
    return f"location '{job.location or 'unknown'}' not in accepted list"


def evaluate(job: Job, prefs: Preferences, tags: Iterable[str] = ()) -> FilterResult:
    tags = list(tags)
    checks = (
        lambda: check_title(job, prefs, tags),
        lambda: check_domain(job, prefs, tags),
        lambda: check_location(job, prefs),
    )
    for check in checks:  # in order, stopping at the first failure
        if reason := check():
            return FilterResult(job=job, passed=False, reason=reason)
    return FilterResult(job=job, passed=True, reason="")


def apply(
    jobs: list[Job], prefs: Preferences, tags: Iterable[str] = ()
) -> tuple[list[Job], list[FilterResult]]:
    """Return (passing jobs, rejected results with reasons)."""
    tags = list(tags)
    passed: list[Job] = []
    rejected: list[FilterResult] = []
    for j in jobs:
        r = evaluate(j, prefs, tags)
        (passed.append(j) if r.passed else rejected.append(r))
    return passed, rejected
