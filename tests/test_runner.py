"""Concurrent board runner. Gates and barriers prove concurrency; no test depends on timing."""

from __future__ import annotations

import _thread
import threading

import pytest

from jobhunt.runner import BoardRunner

WAIT = 5  # seconds; only reached if a test is broken


def _run(items, work, group_of=lambda item: item[0], **kw):
    return BoardRunner(items, work, group_of, **kw)


def test_results_come_back_in_input_order_even_when_later_ones_finish_first():
    second_done = threading.Event()

    def work(item):
        if item == "a1":
            assert second_done.wait(WAIT)  # a1 finishes only after b1
        if item == "b1":
            second_done.set()
        return item.upper()

    assert list(_run(["a1", "b1", "c1"], work, per_group=1)) == ["A1", "B1", "C1"]


def test_each_group_runs_at_most_per_group_items_at_once():
    lock, running, peak = threading.Lock(), [0], [0]
    two_in = threading.Barrier(2, timeout=WAIT)

    def work(item):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        if item in ("g0", "g1"):
            two_in.wait()  # the first two are in flight together
        with lock:
            running[0] -= 1
        return item

    items = [f"g{i}" for i in range(6)]
    assert list(_run(items, work, per_group=2)) == items
    assert peak[0] == 2


def test_different_groups_run_in_parallel():
    both = threading.Barrier(2, timeout=WAIT)  # breaks (fails) unless a and b overlap

    def work(item):
        both.wait()
        return item

    assert list(_run(["a", "b"], work, per_group=1)) == ["a", "b"]


def test_an_interrupt_inside_a_worker_stops_the_run():
    def work(item):
        if item == "a2":
            raise KeyboardInterrupt
        return item

    runner = _run(["a1", "a2", "a3"], work, per_group=1)
    got = []
    with pytest.raises(KeyboardInterrupt):
        for result in runner:
            got.append(result)
    assert got == ["a1"]


def test_ctrl_c_in_the_main_thread_stops_the_run():
    release = threading.Event()

    def work(item):
        if item == "a":
            _thread.interrupt_main()  # what Ctrl-C does
            release.wait(WAIT)
        return item

    runner = _run(["a", "b"], work, per_group=1)
    with pytest.raises(KeyboardInterrupt):
        list(runner)
    release.set()


def test_boards_that_finished_out_of_order_are_kept_after_an_interrupt():
    fast_done = threading.Event()

    def work(item):
        if item == "slow":
            assert fast_done.wait(WAIT)
            raise KeyboardInterrupt
        if item == "fast":
            fast_done.set()
        return item

    runner = BoardRunner(["slow", "fast"], work, group_of=lambda item: item, per_group=1)
    with pytest.raises(KeyboardInterrupt):
        list(runner)
    assert runner.finished_out_of_order() == ["fast"]


def test_a_group_that_keeps_refusing_is_skipped_for_the_rest_of_the_run(caplog):
    calls = []

    def work(item):
        calls.append(item)
        return ("refused", item)

    items = [f"g{i}" for i in range(8)]
    runner = _run(
        items,
        work,
        per_group=1,
        refused=lambda r: r[0] == "refused",
        skip=lambda item: ("skipped", item),
        breaker=5,
    )
    results = list(runner)
    assert calls == items[:5]
    assert [r[0] for r in results] == ["refused"] * 5 + ["skipped"] * 3
    assert "g: 5 boards in a row refused" in caplog.text and "skipping its other 3" in caplog.text


def test_a_success_resets_the_breaker():
    def work(item):
        return "ok" if item == "g2" else "refused"

    items = [f"g{i}" for i in range(8)]
    runner = _run(items, work, per_group=1, refused=lambda r: r == "refused", skip=lambda i: "skipped")
    assert "skipped" not in list(runner)  # never 5 refusals in a row


def test_no_items_no_results():
    assert list(_run([], lambda item: item, per_group=2)) == []
