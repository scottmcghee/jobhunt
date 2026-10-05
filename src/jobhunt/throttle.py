"""Polite HTTP for ``jobhunt fetch``: per-group concurrency that adapts to 429s, and retries.

Every request goes through ``ThrottledTransport``. It finds the request's rate-limit group
(``sources.request_group``: a Workday datacenter or an API host) and:

- waits for a free slot in that group's ``GroupLimiter``, and out any pause a 429 set;
- retries a 429, or a 503 that carries Retry-After, after Retry-After seconds or a 1/2/4 s
  backoff, up to ``MAX_RETRIES`` times; the last response is then returned as is, so the
  caller's ``raise_for_status`` handles it like any other error. A Retry-After over
  ``MAX_RETRY_AFTER`` is not retried: the group pauses for the cap and the response is returned;
- retries a 502 or 504 once;
- passes everything else through untouched.

``GroupLimiter`` is AIMD: each success raises the limit by 1/limit, up to the ceiling; a throttle
halves it, never below 1, at most once per ``COOLDOWN``, so a burst of 429s from requests that
were already in flight counts as one signal.

The default inner transport retries a failed connection once (httpx's default client: never).
"""

from __future__ import annotations

import email.utils
import random
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC

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
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.5),
        max_in_flight: int | None = None,
        limits: httpx.Limits | None = None,
    ):
        if inner is None:
            proxies = urllib.request.getproxies()
            proxy = proxies.get("https") or proxies.get("all")
            if proxy and "://" not in proxy:
                proxy = f"http://{proxy}"  # as httpx does for a bare host:port
            # httpx.Client ignores its own limits when given a transport, so they go here
            inner = httpx.HTTPTransport(retries=1, proxy=proxy, limits=limits or httpx.Limits())
        self._inner = inner
        self._start, self._ceiling = start, ceiling
        self._clock, self._sleep, self._jitter = clock, sleep, jitter
        self._limiters: dict[str, GroupLimiter] = {}
        self._lock = threading.Lock()
        # Caps requests in flight across all groups; taken after the group's slot, never before.
        self._slots = threading.BoundedSemaphore(max_in_flight) if max_in_flight else None

    def limiter(self, group: str) -> GroupLimiter:
        with self._lock:
            if group not in self._limiters:
                self._limiters[group] = GroupLimiter(
                    self._start, self._ceiling, clock=self._clock, sleep=self._sleep
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
        limiter = self.limiter(request_group(request.url))
        retries = 0
        gateway_retried = False
        while True:
            limiter.acquire()
            try:
                if self._slots is None:
                    response = self._inner.handle_request(request)
                else:
                    with self._slots:
                        response = self._inner.handle_request(request)
                throttled = _throttled(response)
                retry_after = response.headers.get("retry-after") if throttled else None
                delay = retry_after_seconds(retry_after)
            except BaseException:
                limiter.release(neutral=True)
                raise
            if throttled:
                if delay is not None and delay > MAX_RETRY_AFTER:
                    limiter.release(throttled=True, retry_after=MAX_RETRY_AFTER)
                    return response
                if retries == MAX_RETRIES:
                    limiter.release(throttled=True)
                    return response
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
