"""Applications to jobs jobhunt found, and what came of them.

``data/applications.jsonl`` is an append-only log of events: ``applied`` (with the resume version,
whether there was a warm contact, and the job's score at the time) and ``outcome`` (one of
``STATUSES``). A correction is a newer event; nothing is rewritten. ``fold`` turns the log into one
``Application`` per job, and ``stats`` groups them, so "new resume vs. old" and "warm vs. cold"
are answered with counts rather than a feeling. Pure functions; the CLI does the I/O.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Status = Literal["no_response", "rejected", "screen", "interview", "offer", "withdrawn"]
STATUSES: tuple[Status, ...] = (
    "no_response", "rejected", "screen", "interview", "offer", "withdrawn"
)
REPLIES = frozenset({"rejected", "screen", "interview", "offer"})  # someone answered
STAGES: tuple[Status, ...] = ("screen", "interview", "offer")  # how far it got, in order


class Event(BaseModel):
    job_key: str
    kind: Literal["applied", "outcome"]
    date: str  # YYYY-MM-DD: the day it happened
    # applied only
    company: str = ""
    title: str = ""
    score: int | None = None  # the job's latest score when applied
    resume: str | None = None
    warm: bool | None = None
    # outcome only
    status: Status | None = None
    recorded_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @field_validator("date")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        return date.fromisoformat(value).isoformat()  # ValueError unless YYYY-MM-DD


class Application(BaseModel):
    job_key: str
    company: str
    title: str
    score: int | None
    applied: str
    resume: str | None
    warm: bool | None
    status: Status | None = None  # the latest outcome; None: nothing yet
    status_date: str | None = None  # the date of that outcome
    reached: Status | None = None  # the furthest stage (screen, interview, offer), if any
    first_reply: str | None = None  # the date of the first outcome that was a reply


class GroupStats(BaseModel):
    label: str
    applied: int
    replied: int  # any reply, a rejection included
    screened: int  # reached screen or further
    interviewed: int  # reached interview or further
    offers: int
    median_days: float | None  # applied to first reply, among those with one


def fold(events: Iterable[Event]) -> list[Application]:
    """One application per job, in the order applied. The latest ``applied`` event wins."""
    apps: dict[str, Application] = {}
    for event in events:
        current = apps.get(event.job_key)
        if event.kind == "applied":
            outcomes = {"status", "status_date", "reached", "first_reply"}
            kept = current.model_dump(include=outcomes) if current else {}
            apps[event.job_key] = Application(
                job_key=event.job_key,
                company=event.company,
                title=event.title,
                score=event.score,
                applied=event.date,
                resume=event.resume,
                warm=event.warm,
                **kept,
            )
        elif current is not None and event.status is not None:
            if current.status_date is None or event.date >= current.status_date:
                current.status, current.status_date = event.status, event.date  # a tie: the later
            if event.status in STAGES and not _at_least(current, event.status):
                current.reached = event.status
            first = current.first_reply
            if event.status in REPLIES and (first is None or event.date < first):
                current.first_reply = event.date
    return sorted(apps.values(), key=lambda a: a.applied)


def _at_least(app: Application, stage: Status) -> bool:
    return app.reached is not None and STAGES.index(app.reached) >= STAGES.index(stage)


def stats(apps: Iterable[Application], group: Callable[[Application], str]) -> list[GroupStats]:
    """Counts per group, groups in order of first appearance."""
    groups: dict[str, list[Application]] = {}
    for app in apps:
        groups.setdefault(group(app), []).append(app)
    result = []
    for label, members in groups.items():
        days = [
            (date.fromisoformat(a.first_reply) - date.fromisoformat(a.applied)).days
            for a in members
            if a.first_reply
        ]
        result.append(
            GroupStats(
                label=label,
                applied=len(members),
                replied=sum(a.first_reply is not None for a in members),
                screened=sum(_at_least(a, "screen") for a in members),
                interviewed=sum(_at_least(a, "interview") for a in members),
                offers=sum(a.reached == "offer" for a in members),
                median_days=statistics.median(days) if days else None,
            )
        )
    return result


def score_band(score: int | None) -> str:
    if score is None:
        return "unscored"
    return "1-4" if score <= 4 else "5-6" if score <= 6 else "7-10"


def warm_label(warm: bool | None) -> str:
    return "warm" if warm else "cold"
