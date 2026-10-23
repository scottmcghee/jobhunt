"""Run per-board work concurrently, at most ``per_group`` at once per rate-limit group, results in
input order.

Each group (a Workday datacenter or an API host, see ``sources.rate_group``) gets its own queue.
One pool of up to ``max_threads`` workers serves them all: a worker takes the next item from any
group that has items and fewer than ``per_group`` running, taking groups in turn, so groups never
wait on each other (a datacenter with 1,000 boards doesn't hold up the API hosts) and thousands of
one-board groups (each company-hosted careers site is its own) don't mean thousands of threads.
Without ``max_threads`` there are as many workers as could ever run at once. How many requests are
actually in flight is up to the transport's limiters (``throttle.py``); the threads only bound it
from above.

Results are released in input order as soon as every earlier one is done, so output stays the
same as a serial run while work finishes in any order. Workers only call ``work``; the caller
consumes results on its own thread, so all bookkeeping stays single-threaded.

A circuit breaker protects hosts that push back: after ``breaker`` refusals in a row in one group
(as judged by ``refused``), that group's remaining items get ``skip(item)`` instead of work. Whether
an item is skipped is decided when it is taken off the queue, so the logged count is exact;
``skipped_by_group()`` says how many each tripped group skipped, for an end-of-run summary.

Ctrl-C, or a ``KeyboardInterrupt`` raised inside ``work``, stops the run: iteration raises
``KeyboardInterrupt``, workers start no new items, and ``finished_out_of_order()`` returns the
results that were done before the stop but not yet released; a result that comes back after it is
dropped. Any other exception from ``work``, ``refused`` or
``skip`` stops the run the same way and is re-raised by the iteration. ``stopping`` tells work
still in flight that the run is over.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from collections.abc import Callable, Hashable, Iterator, Sequence
from typing import Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

_POLL = 0.25  # seconds; how often the main thread wakes, so Ctrl-C is noticed promptly


class _Failed:
    """Sentinel a worker queues when ``work``, ``refused`` or ``skip`` raised."""

    def __init__(self, error: BaseException):
        self.error = error


class BoardRunner(Generic[T, R]):
    def __init__(
        self,
        items: Sequence[T],
        work: Callable[[T], R],
        group_of: Callable[[T], Hashable],
        per_group: int,
        refused: Callable[[R], bool] = lambda result: False,
        skip: Callable[[T], R] | None = None,
        breaker: int = 5,
        max_threads: int | None = None,
    ):
        self._items = list(items)
        self._work, self._refused, self._skip = work, refused, skip
        self._per_group, self._breaker = max(1, per_group), breaker
        self._queues: dict[Hashable, deque[tuple[int, T]]] = {}
        for index, item in enumerate(self._items):
            self._queues.setdefault(group_of(item), deque()).append((index, item))
        wanted = sum(min(self._per_group, len(q)) for q in self._queues.values())
        self._threads = min(wanted, max(1, max_threads)) if max_threads else wanted
        self._ready: deque[Hashable] = deque(self._queues)  # groups with items and a free slot
        self._running: dict[Hashable, int] = dict.fromkeys(self._queues, 0)
        self._results: queue.Queue[tuple[int, object]] = queue.Queue()
        self._pending: dict[int, R] = {}  # finished, waiting for earlier ones
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._freed = threading.Condition(self._lock)  # a group's slot came free
        self._refusals: dict[Hashable, int] = {}
        self._tripped: set[Hashable] = set()
        self._skipped: dict[Hashable, int] = {}

    @property
    def stopping(self) -> bool:
        """True once the run has been interrupted or has failed."""
        return self._stop.is_set()

    def __iter__(self) -> Iterator[R]:
        next_index = 0
        try:
            for _ in range(self._threads):
                threading.Thread(target=self._worker, daemon=True).start()
            while next_index < len(self._items):
                try:
                    index, result = self._results.get(timeout=_POLL)
                except queue.Empty:
                    continue
                if isinstance(result, _Failed):
                    raise result.error
                self._pending[index] = result  # type: ignore[assignment]
                while next_index in self._pending:
                    yield self._pending.pop(next_index)
                    next_index += 1
        except BaseException:
            self._stop.set()
            raise

    def skipped_by_group(self) -> dict[Hashable, int]:
        """How many items each group's tripped breaker skipped; groups that skipped none are
        left out."""
        with self._lock:
            return dict(self._skipped)

    def finished_out_of_order(self) -> list[R]:
        """After an interrupt: results done before it but not yet released, in input order."""
        while True:
            try:
                index, result = self._results.get_nowait()
            except queue.Empty:
                break
            if not isinstance(result, _Failed):
                self._pending[index] = result  # type: ignore[assignment]
        return [self._pending.pop(i) for i in sorted(self._pending)]

    def _take(self) -> tuple[Hashable, int, T, bool] | None:
        """The next item from the next group with a free slot, and whether to skip it (decided
        under the lock with the pop); waits for a slot while items remain, None when none do."""
        with self._freed:
            while not self._stop.is_set():
                if self._ready:
                    group = self._ready.popleft()
                    index, item = self._queues[group].popleft()
                    self._running[group] += 1
                    if self._queues[group] and self._running[group] < self._per_group:
                        self._ready.append(group)  # its next turn comes after the others'
                    skip = group in self._tripped and self._skip is not None
                    if skip:
                        self._skipped[group] = self._skipped.get(group, 0) + 1
                    return group, index, item, skip
                if not any(self._queues.values()):
                    return None
                self._freed.wait(_POLL)  # every group with items is at per_group
            return None

    def _release(self, group: Hashable) -> None:
        """An item of the group is done: its slot is free for the group's next item."""
        with self._freed:
            self._running[group] -= 1
            if self._queues[group] and group not in self._ready:
                self._ready.append(group)
            self._freed.notify()

    def _worker(self) -> None:
        while (job := self._take()) is not None:
            group, index, item, skip = job
            try:
                if skip:
                    self._results.put((index, self._skip(item)))  # type: ignore[misc]
                    self._release(group)
                    continue
                result = self._work(item)
                if self._stop.is_set():  # finished after the stop: the run no longer wants it
                    return
                self._count(group, result)
            except BaseException as e:  # queued so iteration raises it rather than waiting forever
                self._results.put((index, _Failed(e)))
                return
            self._results.put((index, result))
            self._release(group)

    def _count(self, group: Hashable, result: R) -> None:
        with self._lock:
            if not self._refused(result):
                self._refusals[group] = 0
                return
            self._refusals[group] = self._refusals.get(group, 0) + 1
            if self._refusals[group] == self._breaker and self._skip is not None:
                self._tripped.add(group)
                log.warning(
                    "%s: %d boards in a row refused (429/403); skipping its other %d this run",
                    group,
                    self._breaker,
                    len(self._queues[group]),
                )
