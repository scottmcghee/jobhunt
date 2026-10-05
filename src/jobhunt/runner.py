"""Run per-board work concurrently, one worker pool per rate-limit group, results in input order.

Each group (a Workday datacenter or an API host, see ``sources.rate_group``) gets its own queue
and up to ``per_group`` worker threads. Groups never wait on each other: a datacenter with 1,000
boards doesn't hold up the API hosts. How many requests are actually in flight is up to the
transport's limiters (``throttle.py``); the threads only bound it from above.

Results are released in input order as soon as every earlier one is done, so output stays the
same as a serial run while work finishes in any order. Workers only call ``work``; the caller
consumes results on its own thread, so all bookkeeping stays single-threaded.

A circuit breaker protects hosts that push back: after ``breaker`` refusals in a row in one group
(as judged by ``refused``), that group's remaining items get ``skip(item)`` instead of work. Whether
an item is skipped is decided when it is taken off the queue, so the logged count is exact.

Ctrl-C, or a ``KeyboardInterrupt`` raised inside ``work``, stops the run: iteration raises
``KeyboardInterrupt``, workers start no new items, and ``finished_out_of_order()`` returns the
results that were done but not yet released. Any other exception from ``work``, ``refused`` or
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
    ):
        self._items = list(items)
        self._work, self._refused, self._skip = work, refused, skip
        self._per_group, self._breaker = max(1, per_group), breaker
        self._queues: dict[Hashable, deque[tuple[int, T]]] = {}
        for index, item in enumerate(self._items):
            self._queues.setdefault(group_of(item), deque()).append((index, item))
        self._results: queue.Queue[tuple[int, object]] = queue.Queue()
        self._pending: dict[int, R] = {}  # finished, waiting for earlier ones
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._refusals: dict[Hashable, int] = {}
        self._tripped: set[Hashable] = set()

    @property
    def stopping(self) -> bool:
        """True once the run has been interrupted or has failed."""
        return self._stop.is_set()

    def __iter__(self) -> Iterator[R]:
        next_index = 0
        try:
            for group, work_queue in self._queues.items():
                for _ in range(min(self._per_group, len(work_queue))):
                    threading.Thread(target=self._worker, args=(group,), daemon=True).start()
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

    def finished_out_of_order(self) -> list[R]:
        """After an interrupt: results that were done but not yet released, in input order."""
        while True:
            try:
                index, result = self._results.get_nowait()
            except queue.Empty:
                break
            if not isinstance(result, _Failed):
                self._pending[index] = result  # type: ignore[assignment]
        return [self._pending.pop(i) for i in sorted(self._pending)]

    def _next(self, group: Hashable) -> tuple[int, T, bool] | None:
        """The group's next item, and whether to skip it (decided under the lock with the pop)."""
        with self._lock:
            work_queue = self._queues[group]
            if not work_queue or self._stop.is_set():
                return None
            index, item = work_queue.popleft()
            return index, item, group in self._tripped and self._skip is not None

    def _worker(self, group: Hashable) -> None:
        while (job := self._next(group)) is not None:
            index, item, skip = job
            try:
                if skip:
                    self._results.put((index, self._skip(item)))  # type: ignore[misc]
                    continue
                result = self._work(item)
                self._count(group, result)
            except BaseException as e:  # queued so iteration raises it rather than waiting forever
                self._results.put((index, _Failed(e)))
                return
            self._results.put((index, result))

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
