"""How well the scorer agrees with the candidate's own judgment.

The candidate labels a sample of scored jobs 1-10 (``jobhunt label``; labels in
``data/labels.jsonl``), and ``jobhunt eval`` compares those labels with the model's scores: rank
agreement, average error and bias, and above all the letter decision, a score at or above
``min_score_for_letter``. Pure functions; the CLI does the I/O.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from jobhunt.schema import ScoredJob

BANDS = ("7-10", "5-6", "3-4", "1-2")  # highest first: the few high scorers matter most


class Label(BaseModel):
    job_key: str
    score: int = Field(ge=1, le=10)
    note: str = ""
    labeled_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Pair(BaseModel):
    """One labeled job: the candidate's score and the model's."""

    job_key: str
    company: str
    title: str
    human: int
    model: int
    rationale: str


class Metrics(BaseModel):
    n: int
    mae: float  # mean absolute difference
    bias: float  # mean of model minus human: above 0, the model scores higher
    within_1: float  # share within one point
    spearman: float | None  # rank agreement, -1 to 1; None without spread
    threshold: int  # the letter line
    tp: int  # both at or above the line
    fn: int  # the candidate's yes, the model's no: a letter missed
    fp: int  # the model's yes, the candidate's no: a letter wasted
    tn: int
    precision: float | None  # of the model's yeses, the share the candidate agrees with
    recall: float | None  # of the candidate's yeses, the share the model found


def band(score: int) -> str:
    return "7-10" if score >= 7 else "5-6" if score >= 5 else "3-4" if score >= 3 else "1-2"


def sample(
    scored: Iterable[ScoredJob], labeled: set[str], n: int, seed: int = 0
) -> list[ScoredJob]:
    """Up to ``n`` unlabeled jobs, taking the score bands in turn, highest first.

    Real scores are mostly 1s and 2s, so a plain random sample would say little about the few
    jobs near the letter line. Within a band the order is a seeded shuffle, so a later run with the
    same seed continues the same sample.
    """
    rng = random.Random(seed)
    pools: dict[str, list[ScoredJob]] = {b: [] for b in BANDS}
    for s in sorted(scored, key=lambda s: s.job.key):
        if s.job.key not in labeled:
            pools[band(s.score.score)].append(s)
    for pool in pools.values():
        rng.shuffle(pool)
    picked: list[ScoredJob] = []
    while len(picked) < n and any(pools.values()):
        for b in BANDS:
            if pools[b] and len(picked) < n:
                picked.append(pools[b].pop(0))
    return picked


def _ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks, ties sharing the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman's rank correlation; None for fewer than two points or no spread on a side."""
    if len(xs) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if not vx or not vy:
        return None
    return cov / (vx * vy) ** 0.5


def metrics(pairs: Sequence[Pair], threshold: int) -> Metrics:
    if not pairs:
        raise ValueError("no labeled jobs to compare")
    diffs = [p.model - p.human for p in pairs]
    yes_h = [p.human >= threshold for p in pairs]
    yes_m = [p.model >= threshold for p in pairs]
    tp = sum(h and m for h, m in zip(yes_h, yes_m, strict=True))
    fn = sum(h and not m for h, m in zip(yes_h, yes_m, strict=True))
    fp = sum(m and not h for h, m in zip(yes_h, yes_m, strict=True))
    return Metrics(
        n=len(pairs),
        mae=sum(abs(d) for d in diffs) / len(pairs),
        bias=sum(diffs) / len(pairs),
        within_1=sum(abs(d) <= 1 for d in diffs) / len(pairs),
        spearman=spearman([p.human for p in pairs], [p.model for p in pairs]),
        threshold=threshold,
        tp=tp,
        fn=fn,
        fp=fp,
        tn=len(pairs) - tp - fn - fp,
        precision=tp / (tp + fp) if tp + fp else None,
        recall=tp / (tp + fn) if tp + fn else None,
    )


def disagreements(pairs: Iterable[Pair], k: int = 10) -> list[Pair]:
    """The ``k`` pairs furthest apart, biggest gap first."""
    return sorted(pairs, key=lambda p: (-abs(p.model - p.human), p.job_key))[:k]


def fingerprint(prompt: str) -> str:
    """A short hash of a prompt, so eval runs say which version of it they measured."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]
