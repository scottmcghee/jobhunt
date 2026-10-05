"""Polite HTTP for ``jobhunt fetch``: per-group concurrency that adapts to 429s, and retries.

Every request goes through ``ThrottledTransport``. It finds the request's rate-limit group
(``sources.request_group``: a Workday datacenter or an API host) and:

- waits for a free slot in that group's ``GroupLimiter``, and out any pause a 429 set;
- retries a 429, or a 503 that carries Retry-After, after Retry-After seconds (capped) or a
  1/2/4 s backoff, up to ``MAX_RETRIES`` times; the last response is then returned as is, so the
  caller's ``raise_for_status`` handles it like any other error;
- retries a 502 or 504 once;
- passes everything else through untouched.

``GroupLimiter`` is AIMD: each success raises the limit by 1/limit, up to the ceiling; a throttle
halves it, never below 1, at most once per ``COOLDOWN``, so a burst of 429s from requests that
were already in flight counts as one signal.
"""

from __future__ import annotations

import email.utils
import random
import threading
import time
from collections.abc import Callable

import httpx

from jobhunt.sources import request_group

MAX_RETRIES = 3
MAX_RETRY_AFTER = 120.0  # seconds; a server asking for more gets a skipped board instead
COOLDOWN = 5.0  # seconds between two halvings of one group's limit


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or an HTTP date), or None."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return min(max(when.timestamp() - now, 0.0), MAX_RETRY_AFTER)


class GroupLimiter:
    """How many requests one group may have in flight, and when it may send again."""

    def __init__(
        self,
        start: int = 2,
        ceiling: int = 6,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.ceiling = ceiling
        self.limit = float(min(start, ceiling))
        self.in_flight = 0
        self.pause_until = 0.0
        self.requests = 0
        self.throttles = 0
        self.max_in_flight = 0
        self._cooldown_until = 0.0
        self._clock = clock
        self._sleep = sleep
        self._cond = threading.Condition()

    def acquire(self) -> None:
        while True:
            with self._cond:
                wait = self.pause_until - self._clock()
                if wait <= 0:
                    if self.in_flight < int(self.limit):
                        self.in_flight += 1
                        self.requests += 1
                        self.max_in_flight = max(self.max_in_flight, self.in_flight)
                        return
                    self._cond.wait(0.05)
                    continue
            self._sleep(wait)  # outside the lock, so releases aren't blocked meanwhile

    def release(self, throttled: bool = False, retry_after: float = 0.0, neutral: bool = False):
        """Free a slot. A success grows the limit, a throttle shrinks it, ``neutral`` neither."""
        with self._cond:
            self.in_flight -= 1
            now = self._clock()
            if throttled:
                self.throttles += 1
                if now >= self._cooldown_until:
                    self.limit = max(1.0, self.limit / 2)
                    self._cooldown_until = now + COOLDOWN
                self.pause_until = max(self.pause_until, now + retry_after)
            elif not neutral:
                self.limit = min(float(self.ceiling), self.limit + 1 / self.limit)
            self._cond.notify_all()


def _throttled(response: httpx.Response) -> bool:
    return response.status_code == 429 or (
        response.status_code == 503 and "retry-after" in response.headers
    )


class ThrottledTransport(httpx.BaseTransport):
    """An httpx transport that applies the group limits and retries above to every request."""

    def __init__(
        self,
        inner: httpx.BaseTransport | None = None,
        start: int = 2,
        ceiling: int = 6,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.5),
    ):
        self._inner = inner or httpx.HTTPTransport(retries=1)
        self._start, self._ceiling = start, ceiling
        self._clock, self._sleep, self._jitter = clock, sleep, jitter
        self._limiters: dict[str, GroupLimiter] = {}
        self._lock = threading.Lock()

    def limiter(self, group: str) -> GroupLimiter:
        with self._lock:
            if group not in self._limiters:
                self._limiters[group] = GroupLimiter(
                    self._start, self._ceiling, clock=self._clock, sleep=self._sleep
                )
            return self._limiters[group]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        limiter = self.limiter(request_group(request.url))
        retries = 0
        gateway_retried = False
        while True:
            limiter.acquire()
            try:
                response = self._inner.handle_request(request)
            except BaseException:
                limiter.release(neutral=True)
                raise
            if _throttled(response):
                if retries == MAX_RETRIES:
                    limiter.release(throttled=True)
                    return response
                delay = retry_after_seconds(response.headers.get("retry-after"))
                if delay is None:
                    delay = 2.0**retries + self._jitter()
                retries += 1
                response.close()
                limiter.release(throttled=True, retry_after=delay)
                continue
            if response.status_code in (502, 504) and not gateway_retried:
                gateway_retried = True
                response.close()
                limiter.release(neutral=True)
                continue
            limiter.release(neutral=response.status_code >= 500)
            return response

    def close(self) -> None:
        self._inner.close()
