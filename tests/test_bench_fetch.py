"""The fetch benchmark's sampling (scripts/bench_fetch.py). No network."""

from __future__ import annotations

import collections
import importlib.util
from pathlib import Path

from jobhunt.schema import Company
from jobhunt.sources import rate_group

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_fetch.py"
_spec = importlib.util.spec_from_file_location("bench_fetch", _SCRIPT)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def _boards():
    wd = [Company(name=f"t{i}", ats="workday", slug=f"t{i}/s", datacenter="wd1") for i in range(60)]
    wd += [Company(name=f"u{i}", ats="workday", slug=f"u{i}/s", datacenter="wd5") for i in range(30)]
    gh = [Company(name=f"g{i}", ats="greenhouse", slug=f"g{i}") for i in range(9)]
    return wd + gh + [Company(name="only", ats="lever", slug="only")]


def test_sample_keeps_each_groups_share():
    sample = bench.stratified_sample(_boards(), 20, seed=1)
    assert len(sample) == 20
    counts = collections.Counter(rate_group(c) for c in sample)
    # one per group first, then the other 16 by size (60/30/9/1): 9.6/4.8/1.4/0.2, largest remainders
    assert counts == {"workday:wd1": 11, "workday:wd5": 6, "greenhouse": 2, "lever": 1}


def test_with_fewer_slots_than_groups_the_biggest_groups_win():
    sample = bench.stratified_sample(_boards(), 2, seed=1)
    assert {rate_group(c) for c in sample} == {"workday:wd1", "workday:wd5"}


def test_sample_is_deterministic_and_in_config_order():
    boards = _boards()
    a, b = bench.stratified_sample(boards, 20, seed=7), bench.stratified_sample(boards, 20, seed=7)
    assert a == b
    assert a == sorted(a, key=boards.index)
    assert bench.stratified_sample(boards, 20, seed=8) != a


def test_asking_for_more_than_there_are_returns_them_all():
    assert bench.stratified_sample(_boards(), 500, seed=1) == _boards()
