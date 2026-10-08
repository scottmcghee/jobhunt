"""Scorer evaluation: sampling jobs to label, and agreement between human labels and model scores."""

from __future__ import annotations

from collections import Counter

import pytest

from jobhunt import evaluate as ev
from jobhunt.schema import Score, ScoredJob


def _scored(platform_director_job, n, score):
    job = platform_director_job.model_copy(update={"external_id": str(n), "title": f"Role {n}"})
    return ScoredJob(job=job, score=Score(score=score, rationale=f"why {n}", model="m"))


def _pair(key, human, model):
    return ev.Pair(job_key=key, company="Acme", title=key, human=human, model=model, rationale="r")


def test_a_label_is_one_to_ten():
    ev.Label(job_key="k", score=1)
    ev.Label(job_key="k", score=10)
    with pytest.raises(ValueError):
        ev.Label(job_key="k", score=11)


def test_the_sample_spreads_over_score_bands(platform_director_job):
    # mostly 1s and 2s, as in real data: a plain random sample would be nearly all of those
    scored = [_scored(platform_director_job, i, 1) for i in range(40)]
    scored += [_scored(platform_director_job, 100 + i, s) for i, s in enumerate([3, 4, 5, 6, 7, 8])]
    picked = ev.sample(scored, labeled=set(), n=8, seed=1)
    bands = Counter(ev.band(s.score.score) for s in picked)
    assert bands == {"7-10": 2, "5-6": 2, "3-4": 2, "1-2": 2}
    assert len({s.job.key for s in picked}) == 8


def test_the_sample_is_shown_in_mixed_order_not_band_by_band(platform_director_job):
    # the position of a job must not give away the scorer's band
    scored = [_scored(platform_director_job, i, 1 + i % 10) for i in range(40)]
    orders = [[ev.band(s.score.score) for s in ev.sample(scored, set(), n=8, seed=k)] for k in range(10)]
    assert len({tuple(o[:4]) for o in orders}) > 1
    assert any(o[:4] != ["7-10", "5-6", "3-4", "1-2"] for o in orders)
    assert ev.sample(scored, set(), n=8, seed=4) == ev.sample(scored, set(), n=8, seed=4)


def test_labeling_over_several_sessions_continues_the_same_sample(platform_director_job):
    scored = [_scored(platform_director_job, i, 1) for i in range(40)]
    scored += [_scored(platform_director_job, 100 + i, 2 + i % 9) for i in range(30)]
    whole = ev.sample(scored, labeled=set(), n=20, seed=0)
    labeled: set[str] = set()
    while len(labeled) < 20:  # three a session, the rest skipped
        todo = ev.sample(scored, labeled=labeled, n=20 - len(labeled), seed=0)
        labeled |= {s.job.key for s in todo[:3]}
    assert labeled == {s.job.key for s in whole}
    by_key = {s.job.key: s for s in scored}
    assert Counter(ev.band(by_key[k].score.score) for k in labeled) == Counter(
        ev.band(s.score.score) for s in whole
    )


def test_the_sample_skips_labeled_jobs_and_is_repeatable(platform_director_job):
    scored = [_scored(platform_director_job, i, 1 + i % 10) for i in range(30)]
    first = ev.sample(scored, labeled=set(), n=5, seed=3)
    labeled = {first[0].job.key}
    again = ev.sample(scored, labeled=labeled, n=5, seed=3)
    assert first[0].job.key not in {s.job.key for s in again}
    assert ev.sample(scored, labeled=set(), n=5, seed=3) == first


def test_the_sample_is_at_most_what_there_is(platform_director_job):
    scored = [_scored(platform_director_job, i, 5) for i in range(3)]
    assert len(ev.sample(scored, labeled=set(), n=10)) == 3


@pytest.mark.parametrize(("score", "band"), [(1, "1-2"), (2, "1-2"), (3, "3-4"), (6, "5-6"), (7, "7-10"), (10, "7-10")])
def test_bands(score, band):
    assert ev.band(score) == band


def test_spearman():
    assert ev.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert ev.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert ev.spearman([1, 2, 2, 3], [1, 2, 3, 3]) == pytest.approx(0.833, abs=0.001)  # ties: average ranks
    assert ev.spearman([5, 5, 5], [1, 2, 3]) is None  # no spread: undefined
    assert ev.spearman([1], [1]) is None


def test_metrics():
    pairs = [_pair("a", 8, 7), _pair("b", 7, 4), _pair("c", 3, 3), _pair("d", 2, 7)]
    m = ev.metrics(pairs, threshold=7)
    assert m.n == 4
    assert m.mae == pytest.approx((1 + 3 + 0 + 5) / 4)
    assert m.bias == pytest.approx((-1 - 3 + 0 + 5) / 4)  # model minus human
    assert m.within_1 == pytest.approx(2 / 4)
    # at the letter line (7): a is a hit, b a miss, d a false alarm, c rightly left out
    assert (m.tp, m.fn, m.fp, m.tn) == (1, 1, 1, 1)
    assert m.precision == pytest.approx(0.5) and m.recall == pytest.approx(0.5)


def test_metrics_with_nothing_on_one_side_of_the_line():
    m = ev.metrics([_pair("a", 3, 2), _pair("b", 2, 3)], threshold=7)
    assert m.precision is None and m.recall is None


def test_metrics_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        ev.metrics([], threshold=7)


def test_disagreements_biggest_first():
    pairs = [_pair("a", 8, 7), _pair("b", 7, 2), _pair("c", 3, 3), _pair("d", 2, 6)]
    assert [p.job_key for p in ev.disagreements(pairs, k=2)] == ["b", "d"]


def test_the_prompt_fingerprint_changes_with_the_prompt():
    assert ev.fingerprint("a") == ev.fingerprint("a")
    assert ev.fingerprint("a") != ev.fingerprint("b")
    assert len(ev.fingerprint("a")) == 12
