"""Concurrent board runner. Gates and barriers prove concurrency; no test depends on timing."""

from __future__ import annotations

import _thread
import logging
import queue
import threading

import pytest

from jobhunt import runner as runner_module
from jobhunt.runner import BoardRunner

WAIT = 5  # seconds; only reached if a test is broken


def _run(items, work, group_of=lambda item: item[0], **kw):
    return BoardRunner(items, work, group_of, **kw)


class _Announcing(queue.Queue):
    """A result queue that sets ``queued`` once the result at ``index`` is in it."""

    def __init__(self, index):
        super().__init__()
        self.index, self.queued = index, threading.Event()

    def put(self, entry, *args, **kwargs):
        super().put(entry, *args, **kwargs)
        if entry[0] == self.index:
            self.queued.set()


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
    def work(item):
        if item == "slow":
            assert runner._results.queued.wait(WAIT)  # interrupt once fast's result is queued
            raise KeyboardInterrupt
        return item

    runner = BoardRunner(["slow", "fast"], work, group_of=lambda item: item, per_group=1)
    runner._results = _Announcing(index=1)
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


def test_a_refusal_that_finishes_after_the_stop_does_not_trip_the_breaker(caplog):
    caplog.set_level(logging.WARNING)
    fifth_started, g_done = threading.Event(), threading.Event()

    class Runner(BoardRunner):
        def _worker(self, group):
            try:
                super()._worker(group)
            finally:
                if group == "g":
                    g_done.set()

    def work(item):
        group, n = item
        if group == "other":
            assert fifth_started.wait(WAIT)
            raise KeyboardInterrupt
        if n == 4:
            fifth_started.set()
            assert runner._stop.wait(WAIT)  # the 5th refusal comes back after Ctrl-C
        return "refused"

    items = [("other", 0)] + [("g", n) for n in range(7)]
    runner = Runner(
        items,
        work,
        group_of=lambda item: item[0],
        per_group=1,
        refused=lambda r: r == "refused",
        skip=lambda item: "skipped",
    )
    with pytest.raises(KeyboardInterrupt):
        list(runner)
    assert g_done.wait(WAIT)
    assert "boards in a row refused" not in caplog.text


def test_a_success_resets_the_breaker():
    def work(item):
        return "ok" if item == "g2" else "refused"

    items = [f"g{i}" for i in range(8)]
    runner = _run(items, work, per_group=1, refused=lambda r: r == "refused", skip=lambda i: "skipped")
    assert "skipped" not in list(runner)  # never 5 refusals in a row


def test_a_worker_that_raises_ends_the_run_with_that_error():
    def work(item):
        if item == "a1":
            raise ValueError("boom")
        return item

    with pytest.raises(ValueError, match="boom"):
        list(_run(["a1", "b1"], work, per_group=1))


def test_a_skip_that_raises_ends_the_run_with_that_error():
    runner = _run(
        [f"g{i}" for i in range(7)],
        lambda item: "refused",
        per_group=1,
        refused=lambda r: r == "refused",
        skip=lambda item: 1 / 0,
    )
    with pytest.raises(ZeroDivisionError):
        list(runner)


def test_ctrl_c_while_workers_are_still_starting_stops_them(monkeypatch):
    interrupted, release = threading.Event(), threading.Event()
    started, worked = [], []

    class Starting(threading.Thread):
        def start(self):
            started.append(self)
            if len(started) == 2:
                assert interrupted.wait(WAIT)  # Ctrl-C lands while this thread is being started
            super().start()

    def work(item):
        if item == "a0":
            _thread.interrupt_main()
            interrupted.set()
            assert release.wait(WAIT)
        worked.append(item)
        return item

    monkeypatch.setattr(runner_module.threading, "Thread", Starting)
    runner = _run(["a0", "a1", "b0"], work, per_group=1)
    with pytest.raises(KeyboardInterrupt):
        list(runner)
    release.set()
    for thread in started:
        if thread.ident is not None:
            thread.join(WAIT)
    assert "a1" not in worked  # a's worker saw the stop and started nothing new


def test_the_skip_count_in_the_log_matches_the_boards_skipped(caplog):
    caplog.set_level(logging.WARNING)
    g1_popped = threading.Event()

    class Runner(BoardRunner):
        def _next(self, group):
            job = super()._next(group)
            if job and job[1] == "g1":
                g1_popped.set()
                assert tripped.wait(WAIT)  # the breaker trips between g1's pop and its work
            return job

    def refused(result):
        return result == "refused"

    def work(item):
        if item == "g0":
            assert g1_popped.wait(WAIT)
        return "refused"

    tripped = threading.Event()
    runner = Runner(
        [f"g{i}" for i in range(4)],
        work,
        group_of=lambda item: "g",
        per_group=2,
        refused=refused,
        skip=lambda item: "skipped",
        breaker=1,
    )
    real_count = runner._count

    def count(group, result):
        real_count(group, result)
        if group in runner._tripped:
            tripped.set()

    runner._count = count
    results = list(runner)
    assert results.count("skipped") == 2
    assert "skipping its other 2" in caplog.text


def test_no_items_no_results():
    assert list(_run([], lambda item: item, per_group=2)) == []
