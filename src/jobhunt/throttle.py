"""Polite HTTP for ``jobhunt fetch``: per-group concurrency that adapts to 429s, and retries.

Every request goes through ``ThrottledTransport``. It finds the request's rate-limit group
(``sources.request_group``: a Workday datacenter or an API host) and:

- waits in the ``Dispatcher`` until it can take a slot in its group's ``GroupLimiter`` and one
  of the ``max_in_flight`` global slots, both at once: the group must be under its limit, past
  any pause a 429 set and, with a rate cap (``max_rate``), past the gap since its last send.
  Waiting requests hold no slot. When global slots are scarce, the share classes
  (``sources.share_class``: the rate group, but every Oracle tenant together) take turns, so a
  crowd of small groups can't starve a big one;
- retries a 429, or a 503 that carries Retry-After, after Retry-After seconds or a 1/2/4 s
  backoff, up to ``MAX_RETRIES`` times (a Retry-After under a second, like Cloudflare's "0" on a
  rate-limit ban, counts as none, so it gets the backoff rather than instant retries); the last
  response is then returned as is, so the caller's ``raise_for_status`` handles it like any other
  error. A Retry-After over
  ``MAX_RETRY_AFTER`` is not retried: the group pauses for the cap and the response is returned;
- retries a transient failure (a 500, 502 or 504, a connection error, or a timeout, also while
  reading the body, which is read here for that reason) after a 1/2 s backoff, up to
  ``TRANSIENT_RETRIES`` times, holding no slot while it waits; the last response is returned,
  or the last error raised. These leave the group's limit alone;
- passes everything else through untouched.

``GroupLimiter`` is AIMD: each success raises the limit by 1/limit, up to the ceiling; a throttle
halves it, never below 1, at most once per ``COOLDOWN``, so a burst of 429s from requests that
were already in flight counts as one signal.

The default inner transport also retries a failed connection once, at once (httpx's default
client: never; ``connect_retries`` sets it); the backoff above is for failures that outlast that,
like a burst of DNS errors.

``stop()`` (or ``close()``) ends the run: requests waiting for a slot, out a pause, or for the
rate cap wake and raise ``Stopped``, and no new request is sent; one already sent finishes.
"""

from __future__ import annotations

import email.utils
import math
import random
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import UTC
from typing import TYPE_CHECKING

import httpx

from jobhunt.sources import request_group, share_class

if TYPE_CHECKING:
    from jobhunt.settings import FetchSettings

MAX_RETRIES = 3
MAX_RETRY_AFTER = 120.0  # seconds; a server asking for more gets a skipped board instead
COOLDOWN = 5.0  # seconds between two halvings of one group's limit
TRANSIENT_RETRIES = 2
TRANSIENT_STATUSES = frozenset({500, 502, 504})
_POLL = 5.0  # seconds; a waiting request's safety net: it is woken when anything changes
# Failures that a retry a moment later usually gets past. Not, say, UnsupportedProtocol.
TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


class Stopped(Exception):
    """The transport was stopped, so the request wasn't sent. Not an httpx.HTTPError, on purpose."""


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or an HTTP date), or None."""
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():  # not "²", which isdigit() accepts but float() doesn't
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # a "-0000" zone; it is still UTC, not local time
        when = when.replace(tzinfo=UTC)
    now = time.time() if now is None else now
    return max(when.timestamp() - now, 0.0)


class GroupLimiter:
    """How many requests one group may have in flight, and when it may send again.

    The ``Dispatcher`` takes its slots (``try_take``); the transport gives them back (``release``).
    """

    def __init__(
        self,
        start: int = 2,
        ceiling: int = 6,
        clock: Callable[[], float] = time.monotonic,
        cooldown: float | None = None,
        rate: float | None = None,
    ):
        self.ceiling = ceiling
        self.rate = rate  # most requests sent per second; None: no cap
        self._next_start = 0.0
        self._cooldown = COOLDOWN if cooldown is None else cooldown
        self.limit = float(min(start, ceiling))
        self.in_flight = 0
        self.pause_until = 0.0
        self.requests = 0
        self.throttles = 0
        self.max_in_flight = 0
        self._cooldown_until = 0.0
        self._clock = clock
        self._lock = threading.Lock()

    def ready_at(self) -> float:
        """When the group may send next, slots allowing: after any pause, and its rate's gap."""
        with self._lock:
            return max(self.pause_until, self._next_start if self.rate else 0.0)

    def try_take(self, now: float) -> bool:
        """Take a slot if the group is under its limit and ready at ``now``."""
        with self._lock:
            if now < self.pause_until or (self.rate and now < self._next_start):
                return False
            if self.in_flight >= int(self.limit):
                return False
            self.in_flight += 1
            self.requests += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            if self.rate:  # from now, not from the last gap's end, so idle time isn't a burst
                self._next_start = now + 1 / self.rate
            return True

    def release(self, throttled: bool = False, retry_after: float = 0.0, neutral: bool = False):
        """Free a slot. A success grows the limit, a throttle shrinks it, ``neutral`` neither."""
        with self._lock:
            self.in_flight -= 1
            now = self._clock()
            if throttled:
                self.throttles += 1
                if now >= self._cooldown_until:
                    self.limit = max(1.0, self.limit / 2)
                    self._cooldown_until = now + self._cooldown
                self.pause_until = max(self.pause_until, now + retry_after)
            elif not neutral:
                self.limit = min(float(self.ceiling), self.limit + 1 / self.limit)


class Waiter:
    """One request waiting in the ``Dispatcher``; ``granted`` once it holds both its slots."""

    __slots__ = ("limiter", "share", "granted", "wake")

    def __init__(self, limiter: GroupLimiter, share: str):
        self.limiter, self.share, self.granted = limiter, share, False
        # set on its grant, so a grant wakes only its own waiter; or when a gap or pause begins
        self.wake = threading.Event()


class Dispatcher:
    """Hands each request its group's slot and a global slot together, sharing them fairly.

    Waiting requests queue by share class. Whenever slots may have come free, the classes take
    turns, round robin, each granting its first waiter whose group can take a slot now; so a
    class gets the same turns whether it has one waiter or a thousand. ``max_in_flight`` None
    means no global cap, and only the groups' limits apply.
    """

    def __init__(
        self,
        max_in_flight: int | None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        stop: threading.Event | None = None,
    ):
        self.max_in_flight = max_in_flight
        self.free: float = max_in_flight if max_in_flight else math.inf
        self._clock = clock
        self._stop = stop or threading.Event()
        self._sleep = sleep or self._stop.wait  # by default a wait ends early on a stop
        self._queues: dict[str, deque[Waiter]] = {}  # in the order classes first appeared
        self._turn = 0  # index into _queues of the class whose turn is next
        self._lock = threading.Lock()

    def acquire(self, limiter: GroupLimiter, share: str) -> None:
        """Wait until the request holds both its slots; raises ``Stopped`` on ``stop``."""
        waiter = self.enqueue(limiter, share)
        while True:
            with self._lock:
                if waiter.granted:
                    return
                if self._stop.is_set():
                    self._queues[share].remove(waiter)
                    raise Stopped
                wait = limiter.ready_at() - self._clock()
            if wait > 0:  # out a pause or a rate gap; then nobody else may hand out its slot
                self._sleep(wait)
                with self._lock:
                    self._dispatch()
            else:  # for a slot: whoever frees one grants it, or wakes it when a gap or pause begins
                woken = waiter.wake.wait(_POLL)
                waiter.wake.clear()  # a grant is read from ``granted``, under the lock
                if not woken:
                    with self._lock:
                        self._dispatch()

    def enqueue(self, limiter: GroupLimiter, share: str) -> Waiter:
        """Queue a request without waiting; it may be granted at once."""
        waiter = Waiter(limiter, share)
        with self._lock:
            self._queues.setdefault(share, deque()).append(waiter)
            self._dispatch()
        return waiter

    def release(self, limiter: GroupLimiter | None = None) -> None:
        """Give a global slot back (after ``limiter``'s), and hand out what came free.

        If that release paused the group (a 429), its waiters are woken to sleep the pause out.
        """
        with self._lock:
            self.free += 1
            self._dispatch()
            if limiter is not None and limiter.ready_at() > self._clock():
                self._wake_group(limiter)

    def wake_all(self) -> None:
        """Wake every waiter, so a stop is noticed at once."""
        with self._lock:
            for queue in self._queues.values():
                for waiter in queue:
                    waiter.wake.set()

    def waiting(self) -> int:
        with self._lock:
            return sum(len(q) for q in self._queues.values())

    def _dispatch(self) -> None:
        """Grant waiters, the classes in turn, while global slots last. Holds ``_lock``."""
        while self.free > 0:
            now = self._clock()
            shares = list(self._queues)
            for i in range(len(shares)):
                index = (self._turn + i) % len(shares)
                if waiter := self._first_ready(self._queues[shares[index]], now):
                    break
            else:
                break
            self._queues[waiter.share].remove(waiter)
            waiter.granted = True
            waiter.wake.set()
            self.free -= 1
            self._turn = (index + 1) % len(shares)
            if waiter.limiter.rate:  # its group's next gap starts now: its waiters sleep it out
                self._wake_group(waiter.limiter)

    def _wake_group(self, limiter: GroupLimiter) -> None:
        """Wake the waiters of one group, to recheck when it is ready. Holds ``_lock``."""
        for queue in self._queues.values():
            for other in queue:
                if other.limiter is limiter:
                    other.wake.set()

    @staticmethod
    def _first_ready(queue: deque[Waiter], now: float) -> Waiter | None:
        """The first waiter whose group takes a slot now; each group is asked once."""
        asked: set[int] = set()
        for waiter in queue:
            if id(waiter.limiter) in asked:
                continue
            if waiter.limiter.try_take(now):
                return waiter
            asked.add(id(waiter.limiter))
        return None


def _throttled(response: httpx.Response) -> bool:
    return response.status_code == 429 or (
        response.status_code == 503 and "retry-after" in response.headers
    )


class ThrottledTransport(httpx.BaseTransport):
    """An httpx transport that applies the group limits and retries above to every request.

    Giving httpx.Client a transport turns off its environment-proxy support, so the default
    inner transport uses the HTTPS (else ALL) proxy from ``urllib.request.getproxies()`` itself.
    NO_PROXY is not honored.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport | None = None,
        start: int = 2,
        ceiling: int = 6,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.5),
        max_in_flight: int | None = None,
        limits: httpx.Limits | None = None,
        max_retries: int | None = None,
        max_retry_after: float | None = None,
        cooldown: float | None = None,
        transient_retries: int | None = None,
        max_rate: dict[str, float] | None = None,
        connect_retries: int = 1,
    ):
        if inner is None:
            proxies = urllib.request.getproxies()
            proxy = proxies.get("https") or proxies.get("all")
            if proxy and "://" not in proxy:
                proxy = f"http://{proxy}"  # as httpx does for a bare host:port
            # httpx.Client ignores its own limits when given a transport, so they go here
            inner = httpx.HTTPTransport(
                retries=connect_retries, proxy=proxy, limits=limits or httpx.Limits()
            )
        self._inner = inner
        self._start, self._ceiling = start, ceiling
        self._clock, self._sleep, self._jitter = clock, sleep, jitter
        # None: the module defaults, read now (settings.py passes the configured values)
        self._max_retries = MAX_RETRIES if max_retries is None else max_retries
        self._max_retry_after = MAX_RETRY_AFTER if max_retry_after is None else max_retry_after
        self._cooldown = cooldown
        self._transient_retries = (
            TRANSIENT_RETRIES if transient_retries is None else transient_retries
        )
        self._max_rate = dict(max_rate or {})  # rate group -> most requests sent per second
        self._limiters: dict[str, GroupLimiter] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Caps requests in flight across all groups, and shares them out between groups.
        self._dispatcher = Dispatcher(max_in_flight, clock=clock, sleep=sleep, stop=self._stop)

    @property
    def max_in_flight(self) -> int | None:
        return self._dispatcher.max_in_flight

    def limiter(self, group: str) -> GroupLimiter:
        with self._lock:
            if group not in self._limiters:
                self._limiters[group] = GroupLimiter(
                    self._start,
                    self._ceiling,
                    clock=self._clock,
                    cooldown=self._cooldown,
                    rate=self._max_rate.get(group),
                )
            return self._limiters[group]

    def stats(self) -> dict[str, dict[str, float]]:
        """Per group: requests sent (retries included), throttles, peak in flight, current limit."""
        with self._lock:
            limiters = dict(self._limiters)
        return {
            group: {
                "requests": lim.requests,
                "throttles": lim.throttles,
                "max_in_flight": lim.max_in_flight,
                "limit": lim.limit,
            }
            for group, lim in sorted(limiters.items())
        }

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        group = request_group(request.url)
        limiter, share = self.limiter(group), share_class(group)
        retries = transient = 0
        while True:
            self._dispatcher.acquire(limiter, share)
            response = None
            try:
                response = self._send(request)
                response.read()  # a body that stalls or drops fails here, so it is retried
                throttled = _throttled(response)
                retry_after = response.headers.get("retry-after") if throttled else None
                delay = retry_after_seconds(retry_after)
                if delay is not None and delay < 1:  # "Retry-After: 0" during a ban
                    delay = None
            except TRANSIENT_ERRORS:
                if response is not None:
                    response.close()
                self._release(limiter, neutral=True)
                if transient == self._transient_retries:
                    raise
                transient += 1
                self._backoff(transient)
                continue
            except BaseException:
                self._release(limiter, neutral=True)
                raise
            if throttled:
                if delay is not None and delay > self._max_retry_after:
                    self._release(limiter, throttled=True, retry_after=self._max_retry_after)
                    return response
                if retries == self._max_retries:
                    self._release(limiter, throttled=True)
                    return response
                if delay is None:
                    delay = 2.0**retries + self._jitter()
                retries += 1
                response.close()
                self._release(limiter, throttled=True, retry_after=delay)
                continue
            if response.status_code in TRANSIENT_STATUSES and transient < self._transient_retries:
                transient += 1
                response.close()
                self._release(limiter, neutral=True)
                self._backoff(transient)
                continue
            self._release(limiter, neutral=response.status_code >= 500)
            return response

    def _release(
        self,
        limiter: GroupLimiter,
        throttled: bool = False,
        retry_after: float = 0.0,
        neutral: bool = False,
    ) -> None:
        """Give back the group's slot (adjusting its limit), then the global one."""
        limiter.release(throttled=throttled, retry_after=retry_after, neutral=neutral)
        self._dispatcher.release(limiter)

    def _backoff(self, attempt: int) -> None:
        """Wait before transient retry ``attempt`` (1, 2, ...); a stop cuts it short."""
        (self._sleep or self._stop.wait)(2.0 ** (attempt - 1) + self._jitter())

    def _send(self, request: httpx.Request) -> httpx.Response:
        if self._stop.is_set():  # stopped while waiting for the slot
            raise Stopped
        return self._inner.handle_request(request)

    def stop(self) -> None:
        """Send nothing more; wake every request waiting for a slot or out a pause."""
        self._stop.set()
        self._dispatcher.wake_all()

    def close(self) -> None:
        self.stop()
        self._inner.close()


def from_settings(fetch: FetchSettings, workers: int, per_host: int) -> ThrottledTransport:
    """The transport ``fetch`` and ``slugs --check`` use, tuned by the fetch settings.

    ``workers`` caps requests in flight across all groups and sizes the connection pool to match.
    """
    pool = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    return ThrottledTransport(
        start=fetch.start_per_host,
        ceiling=per_host,
        max_in_flight=workers,
        limits=pool,
        max_retries=fetch.max_retries,
        max_retry_after=fetch.max_retry_after,
        cooldown=fetch.cooldown,
        transient_retries=fetch.transient_retries,
        max_rate=fetch.max_rate,
    )
