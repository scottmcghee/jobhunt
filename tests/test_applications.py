"""Applications and their outcomes, folded from an append-only event log, and grouped stats."""

from __future__ import annotations

import pytest

from jobhunt import applications as ap


def _applied(key="greenhouse:acme:1", date="2026-10-01", resume="v1", warm=False, score=7, **kw):
    return ap.Event(
        job_key=key, kind="applied", date=date, resume=resume, warm=warm, score=score,
        company=kw.get("company", "Acme"), title=kw.get("title", "Director"),
    )


def _outcome(key="greenhouse:acme:1", status="screen", date="2026-10-05"):
    return ap.Event(job_key=key, kind="outcome", date=date, status=status)


def test_an_application_without_outcomes_is_pending():
    (app,) = ap.fold([_applied()])
    assert (app.job_key, app.applied, app.resume, app.warm, app.score) == ("greenhouse:acme:1", "2026-10-01", "v1", False, 7)
    assert app.status is None and app.reached is None and app.first_reply is None


def test_the_latest_outcome_is_the_status_and_the_furthest_stage_is_kept():
    events = [
        _applied(),
        _outcome(status="screen", date="2026-10-05"),
        _outcome(status="interview", date="2026-10-12"),
        _outcome(status="rejected", date="2026-10-20"),
    ]
    (app,) = ap.fold(events)
    assert app.status == "rejected"  # where it stands now
    assert app.reached == "interview"  # how far it got
    assert app.first_reply == "2026-10-05"


def test_no_response_and_withdrawn_are_not_replies():
    (app,) = ap.fold([_applied(), _outcome(status="no_response"), _outcome(status="withdrawn", date="2026-10-09")])
    assert app.status == "withdrawn" and app.first_reply is None and app.reached is None


def test_a_rejection_is_a_reply_but_no_stage():
    (app,) = ap.fold([_applied(), _outcome(status="rejected", date="2026-10-03")])
    assert app.first_reply == "2026-10-03" and app.reached is None


def test_a_later_applied_event_corrects_the_earlier_one_and_keeps_outcomes():
    events = [_applied(resume="v1"), _outcome(status="screen"), _applied(resume="v2", date="2026-10-02")]
    (app,) = ap.fold(events)
    assert (app.resume, app.applied, app.reached) == ("v2", "2026-10-02", "screen")


def test_outcomes_for_a_job_never_applied_to_are_ignored():
    assert ap.fold([_outcome()]) == []


def test_applications_come_in_the_order_they_were_applied():
    events = [_applied(key="b", date="2026-10-03"), _applied(key="a", date="2026-10-01")]
    assert [a.job_key for a in ap.fold(events)] == ["a", "b"]


def test_stats_group_counts_and_median_days_to_first_reply():
    events = [
        _applied(key="a", resume="v1"), _outcome(key="a", status="rejected", date="2026-10-03"),
        _applied(key="b", resume="v1"),
        _applied(key="c", resume="v2"), _outcome(key="c", status="screen", date="2026-10-05"),
        _outcome(key="c", status="offer", date="2026-10-30"),
        _applied(key="d", resume="v2"), _outcome(key="d", status="interview", date="2026-10-11"),
    ]
    groups = {g.label: g for g in ap.stats(ap.fold(events), lambda a: a.resume)}
    v1, v2 = groups["v1"], groups["v2"]
    assert (v1.applied, v1.replied, v1.screened, v1.interviewed, v1.offers) == (2, 1, 0, 0, 0)
    assert v1.median_days == 2
    assert (v2.applied, v2.replied, v2.screened, v2.interviewed, v2.offers) == (2, 2, 2, 2, 1)
    assert v2.median_days == 7  # 4 and 10 days


def test_stats_without_replies_have_no_median():
    (group,) = ap.stats(ap.fold([_applied()]), lambda a: "all")
    assert group.median_days is None


@pytest.mark.parametrize(
    ("score", "band"), [(None, "unscored"), (1, "1-4"), (4, "1-4"), (5, "5-6"), (6, "5-6"), (7, "7-10"), (10, "7-10")]
)
def test_score_bands(score, band):
    assert ap.score_band(score) == band


def test_warm_label():
    assert ap.warm_label(True) == "warm" and ap.warm_label(False) == "cold"


def test_an_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        ap.Event(job_key="k", kind="outcome", date="2026-10-01", status="ghosted")


def test_a_date_must_be_iso():
    with pytest.raises(ValueError):
        ap.Event(job_key="k", kind="applied", date="10/01/2026")
