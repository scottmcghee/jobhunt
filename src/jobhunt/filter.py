"""Deterministic pre-filter. No network, no LLM. Pure functions over Job + Preferences.

The point of this stage is to make the expensive scoring step cheap by throwing
out the obvious misses first — and to do it in a way that is fully testable.
"""

from __future__ import annotations

import re
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


def check_title(job: Job, prefs: Preferences) -> str | None:
    rules = prefs.title
    if rules.must_include_any and not _any_in(rules.must_include_any, job.title):
        return "title lacks a target level keyword"
    hit = _any_in(rules.must_exclude_any, job.title)
    if hit:
        return f"title contains excluded term '{hit}'"
    return None


def check_domain(job: Job, prefs: Preferences) -> str | None:
    rules = prefs.domain
    if not rules.must_include_any:
        return None
    text = f"{job.title}\n{job.body}"
    if not _any_in(rules.must_include_any, text):
        return "no domain keyword in title or body"
    return None


def check_location(job: Job, prefs: Preferences) -> str | None:
    rules = prefs.location
    loc_text = f"{job.location} {job.title}".lower()

    hit = _any_in(rules.reject_any, loc_text)
    if hit:
        return f"location matches rejected term '{hit}'"

    if rules.allow_remote and job.remote:
        return None
    if job.remote is False and rules.onsite_accept_any:
        # Explicitly on-site: a broad accept term like "united states" isn't enough.
        if not _any_in(rules.onsite_accept_any, loc_text):
            return f"on-site role in '{job.location or 'unknown'}', outside onsite_accept_any"
        return None
    if _any_in(rules.accept_any, loc_text):
        return None
    if job.remote is None and "remote" in job.body.lower()[:2000] and rules.allow_remote:
        # Location unknown, but the body talks about remote near the top. Let it through;
        # the scorer will see the full text.
        return None
    return f"location '{job.location or 'unknown'}' not in accepted list"


def evaluate(job: Job, prefs: Preferences) -> FilterResult:
    for check in (check_title, check_domain, check_location):
        reason = check(job, prefs)
        if reason:
            return FilterResult(job=job, passed=False, reason=reason)
    return FilterResult(job=job, passed=True, reason="")


def apply(jobs: list[Job], prefs: Preferences) -> tuple[list[Job], list[FilterResult]]:
    """Return (passing jobs, rejected results with reasons)."""
    passed: list[Job] = []
    rejected: list[FilterResult] = []
    for j in jobs:
        r = evaluate(j, prefs)
        (passed.append(j) if r.passed else rejected.append(r))
    return passed, rejected
