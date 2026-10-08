"""Per-group concurrency limits and polite retries. Fake clock and recorded sleeps; no waiting."""

from __future__ import annotations

import json
import urllib.request

import httpcore
import httpx
import pytest
import respx

from jobhunt import throttle
from jobhunt.sources import workday


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def _limiter(clock, **kw):
    return throttle.GroupLimiter(clock=clock, **kw)


def test_limit_grows_on_success_up_to_the_ceiling():
    clock = FakeClock()
    lim = _limiter(clock, start=2, ceiling=4)
    for _ in range(50):
        assert lim.try_take(clock())
        lim.release(throttled=False)
    assert lim.limit == 4


def test_a_throttle_halves_the_limit_but_not_below_one():
    clock = FakeClock()
    lim = _limiter(clock, start=4, ceiling=6)
    lim.try_take(clock())
    lim.release(throttled=True, retry_after=0)
    assert lim.limit == 2
    clock.now += 60  # past the cooldown
    for _ in range(3):
        lim.try_take(clock())
        lim.release(throttled=True, retry_after=0)
        clock.now += 60
    assert lim.limit == 1


def test_a_burst_of_throttles_halves_once_per_cooldown():
    # requests already in flight when the server starts refusing shouldn't collapse the limit
    clock = FakeClock()
    lim = _limiter(clock, start=4, ceiling=6)
    for _ in range(3):
        lim.try_take(clock())
    for _ in range(3):
        lim.release(throttled=True, retry_after=0)
    assert lim.limit == 2


def test_a_group_takes_no_more_slots_than_its_limit():
    clock = FakeClock()
    lim = _limiter(clock, start=2, ceiling=6)
    assert [lim.try_take(clock()) for _ in range(3)] == [True, True, False]
    assert lim.in_flight == 2 and lim.requests == 2 and lim.max_in_flight == 2


def test_a_rate_spaces_request_starts():
    clock = FakeClock()
    lim = _limiter(clock, start=6, ceiling=6, rate=2.0)
    assert lim.try_take(clock()) and lim.ready_at() == clock.now + 0.5
    assert not lim.try_take(clock())  # a slot is free, but the gap isn't over
    clock.now += 0.5
    assert lim.try_take(clock())
    clock.now += 10  # idle time isn't banked as a burst
    assert lim.try_take(clock()) and not lim.try_take(clock())


def test_a_paused_group_takes_no_slot_until_the_pause_ends():
    clock = FakeClock()
    lim = _limiter(clock, start=2, ceiling=6)
    lim.try_take(clock())
    lim.release(throttled=True, retry_after=7)
    assert lim.ready_at() == clock.now + 7 and not lim.try_take(clock())
    clock.now += 7
    assert lim.try_take(clock())


def test_without_a_rate_or_a_pause_a_group_is_ready_at_once():
    clock = FakeClock()
    lim = _limiter(clock, start=6, ceiling=6)
    assert lim.ready_at() <= clock.now


@pytest.mark.parametrize(
    ("header", "expected"),
    [("7", 7.0), ("0", 0.0), ("999", 999.0), ("soon", None), (None, None), ("²", None)],
)
def test_retry_after_seconds(header, expected):
    assert throttle.retry_after_seconds(header) == expected


def test_retry_after_http_date():
    now = 1_700_000_000.0
    header = "Tue, 14 Nov 2023 22:13:30 GMT"  # 10 s after `now`
    assert throttle.retry_after_seconds(header, now=now) == pytest.approx(10.0)


def test_retry_after_http_date_in_minus_zero_zone_is_utc():
    # parsedate_to_datetime gives a naive datetime for -0000; it must not be read as local time
    now = 1_700_000_000.0
    header = "Tue, 14 Nov 2023 22:13:30 -0000"
    assert throttle.retry_after_seconds(header, now=now) == pytest.approx(10.0)


# --------------------------------------------------------------------------- transport

URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _client(clock):
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    return httpx.Client(transport=transport)


@respx.mock
def test_429_with_retry_after_is_retried_after_waiting():
    clock = FakeClock()
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"jobs": []})]
    )
    with _client(clock) as client:
        assert client.get(URL).status_code == 200
    assert route.call_count == 2
    assert sum(clock.slept) == pytest.approx(7)


@respx.mock
def test_429_without_retry_after_backs_off_exponentially_then_gives_up():
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(429))
    with _client(clock) as client:
        assert client.get(URL).status_code == 429  # the caller's raise_for_status takes it from here
    assert route.call_count == 1 + throttle.MAX_RETRIES
    assert clock.slept == [1.0, 2.0, 4.0]


@respx.mock
def test_a_429_with_retry_after_zero_backs_off_like_one_without():
    # Cloudflare's rate-limit ban (error 1015) says "Retry-After: 0"; retrying at once only
    # spends the retries inside the ban
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "0"}))
    with _client(clock) as client:
        assert client.get(URL).status_code == 429
    assert route.call_count == 1 + throttle.MAX_RETRIES
    assert clock.slept == [1.0, 2.0, 4.0]


@respx.mock
def test_max_rate_caps_one_group_and_leaves_the_others_alone():
    clock = FakeClock()
    other = "https://api.lever.co/v0/postings/acme"
    respx.get(URL).mock(return_value=httpx.Response(200))
    respx.get(other).mock(return_value=httpx.Response(200))
    transport = throttle.ThrottledTransport(
        start=6, clock=clock, sleep=clock.sleep, max_rate={"greenhouse": 2.0}
    )
    with httpx.Client(transport=transport) as client:
        for _ in range(3):
            client.get(other)
        assert clock.slept == []
        for _ in range(3):
            client.get(URL)
    assert clock.slept == [0.5, 0.5]


@respx.mock
def test_503_is_retried_only_with_retry_after():
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with _client(clock) as client:
        assert client.get(URL).status_code == 503
    assert route.call_count == 1

    route.mock(side_effect=[httpx.Response(503, headers={"Retry-After": "2"}), httpx.Response(200)])
    with _client(clock) as client:
        assert client.get(URL).status_code == 200


@pytest.mark.parametrize("status", [500, 502, 504])
@respx.mock
def test_transient_server_errors_are_retried_with_a_backoff_then_returned(status):
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    with _client(clock) as client:
        assert client.get(URL).status_code == status  # the caller's raise_for_status takes it
    assert route.call_count == 1 + throttle.TRANSIENT_RETRIES
    assert clock.slept == [1.0, 2.0]

    route.mock(side_effect=[httpx.Response(status), httpx.Response(200)])
    with _client(clock) as client:
        assert client.get(URL).status_code == 200


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("[Errno 9] Bad file descriptor"),
        httpx.ConnectError("[Errno 6] Device not configured"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("The read operation timed out"),
        httpx.ReadError("connection reset"),
        httpx.WriteError("broken pipe"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    ],
)
@respx.mock
def test_transient_transport_errors_are_retried_with_a_backoff_then_raised(error):
    clock = FakeClock()
    route = respx.get(URL).mock(side_effect=[error, httpx.Response(200)])
    with _client(clock) as client:
        assert client.get(URL).status_code == 200
    assert clock.slept == [1.0]

    route.mock(side_effect=error)
    with _client(clock) as client, pytest.raises(type(error)):
        client.get(URL)
    assert route.call_count == 2 + 1 + throttle.TRANSIENT_RETRIES


class _Body(httpx.SyncByteStream):
    """A response body that raises ``error`` while it is read, if given; records reads and closes."""

    def __init__(self, error: Exception | None = None):
        self.error, self.reads, self.closed = error, 0, False

    def __iter__(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        yield b"ok"

    def close(self):
        self.closed = True


def _serving(bodies, statuses=None):
    """A transport that answers each request with the next of ``bodies`` (and ``statuses``)."""
    statuses = list(statuses or [200] * len(bodies))
    queue = list(bodies)
    return httpx.MockTransport(lambda request: httpx.Response(statuses.pop(0), stream=queue.pop(0)))


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("The read operation timed out"), httpx.RemoteProtocolError("peer closed connection")],
)
def test_a_failure_while_reading_the_body_is_retried(error):
    # the inner transport returns after the headers; a body that stalls or drops comes later
    clock = FakeClock()
    bodies = [_Body(error), _Body()]
    transport = throttle.ThrottledTransport(
        inner=_serving(bodies), clock=clock, sleep=clock.sleep, jitter=lambda: 0.0
    )
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).text == "ok"
    assert clock.slept == [1.0]
    assert bodies[0].closed
    assert bodies[1].reads == 1  # read once, by the transport; the client's read is a no-op
    assert transport.limiter("greenhouse").in_flight == 0


@pytest.mark.parametrize("status", [429, 502])
def test_a_retried_response_is_closed(status):
    clock = FakeClock()
    bodies = [_Body(), _Body()]
    transport = throttle.ThrottledTransport(
        inner=_serving(bodies, [status, 200]), clock=clock, sleep=clock.sleep, jitter=lambda: 0.0
    )
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 200
    assert bodies[0].closed and bodies[1].closed


@respx.mock
def test_a_transient_retry_frees_its_slots_while_it_waits():
    clock = FakeClock()
    transport = throttle.ThrottledTransport(
        start=1, ceiling=1, clock=clock, jitter=lambda: 0.0, max_in_flight=1
    )
    seen: list[tuple[int, int]] = []

    def sleep(seconds):
        lim = transport.limiter(throttle.request_group(httpx.URL(URL)))
        seen.append((lim.in_flight, transport._dispatcher.free))
        clock.sleep(seconds)

    transport._sleep = sleep
    respx.get(URL).mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200)])
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 200
    assert seen == [(0, 1)]  # neither the group's slot nor the global one is held


@respx.mock
def test_a_transient_failure_neither_grows_nor_shrinks_the_limit():
    clock = FakeClock()
    respx.get(URL).mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(500), httpx.Response(500)])
    transport = throttle.ThrottledTransport(start=2, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 500
    (stats,) = transport.stats().values()
    assert stats["limit"] == 2 and stats["throttles"] == 0 and stats["requests"] == 3


def test_a_stop_during_a_transient_backoff_sends_nothing_more():
    transport = throttle.ThrottledTransport(jitter=lambda: 0.0)
    sent = []

    def handler(request):
        sent.append(request)
        transport.stop()  # e.g. Ctrl-C while this request's retry is waiting
        raise httpx.ConnectError("boom", request=request)

    transport._inner = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client, pytest.raises(throttle.Stopped):
        client.get(URL)
    assert len(sent) == 1


def test_a_stop_cuts_a_transient_backoff_short():
    import threading

    sent = threading.Event()

    def handler(request):
        sent.set()
        raise httpx.ConnectError("boom", request=request)

    # a 30 s backoff: only a wait on the stop event, not time.sleep, ends it at once
    transport = throttle.ThrottledTransport(inner=httpx.MockTransport(handler), jitter=lambda: 29.0)
    raised: list[BaseException] = []

    def run():
        try:
            transport.handle_request(httpx.Request("GET", URL))
        except BaseException as e:
            raised.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert sent.wait(STOP_WAIT)
    transport.stop()
    thread.join(1)
    assert not thread.is_alive()
    assert [type(e) for e in raised] == [throttle.Stopped]


@respx.mock
def test_transient_retries_is_a_constructor_parameter():
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(502))
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, transient_retries=0)
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 502
    assert route.call_count == 1
    route.mock(side_effect=httpx.ConnectError("boom"))
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, transient_retries=0)
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectError):
        client.get(URL)
    assert route.call_count == 2


@respx.mock
def test_other_errors_pass_straight_through():
    clock = FakeClock()
    for status in (404, 403, 422, 400, 501):
        route = respx.get(URL).mock(return_value=httpx.Response(status))
        with _client(clock) as client:
            assert client.get(URL).status_code == status
        assert route.call_count == 1
        respx.reset()
    route = respx.get(URL).mock(side_effect=httpx.UnsupportedProtocol("ftp?"))
    with _client(clock) as client, pytest.raises(httpx.UnsupportedProtocol):
        client.get(URL)
    assert route.call_count == 1
    assert clock.slept == []


@respx.mock
def test_a_retried_post_sends_the_same_body():
    clock = FakeClock()
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    route = respx.post(url).mock(side_effect=[httpx.Response(429), httpx.Response(200, json={})])
    with _client(clock) as client:
        client.post(url, json={"offset": 20})
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies == [{"offset": 20}, {"offset": 20}]


@respx.mock
def test_throttling_one_group_leaves_others_alone():
    clock = FakeClock()
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200)])
    respx.get("https://api.lever.co/v0/postings/acme").mock(return_value=httpx.Response(200, json=[]))
    with httpx.Client(transport=transport) as client:
        client.get(URL)
        client.get("https://api.lever.co/v0/postings/acme")
    assert transport.limiter("greenhouse").throttles == 1
    assert transport.limiter("lever").throttles == 0
    assert transport.limiter("lever").limit > transport.limiter("greenhouse").limit


WD = "https://examplecorp.wd5.myworkdayjobs.com/wday/cxs/examplecorp/External"


@respx.mock
def test_a_throttled_workday_description_is_kept(workday_company, fixture_json):
    # before, a 429 on a detail request silently dropped that posting's description
    clock = FakeClock()
    respx.post(WD + "/jobs").mock(return_value=httpx.Response(200, json=fixture_json("workday_jobs.json")))
    respx.get(WD + "/job/Seattle-WA/Director-of-Platform-Engineering_R1001").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=fixture_json("workday_job.json"))]
    )
    with _client(clock) as client:
        jobs = workday.fetch(workday_company, client, lambda job: job.title.startswith("Director of Platform"))
    assert "infrastructure & developer experience" in jobs[0].body


@respx.mock
def test_an_unparseable_retry_after_backs_off_and_frees_the_slot():
    # "²" passes str.isdigit but not float(); it used to raise with the slot still held
    clock = FakeClock()
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers=[(b"Retry-After", "²".encode())]), httpx.Response(200)]
    )
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 200
    assert clock.slept == [1.0]
    assert transport.limiter("greenhouse").in_flight == 0


@respx.mock
def test_a_retry_after_over_the_cap_is_not_retried_but_pauses_the_group():
    clock = FakeClock()
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    route = respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 429  # the caller skips the board
    assert route.call_count == 1
    limiter = transport.limiter("greenhouse")
    assert limiter.pause_until == pytest.approx(clock.now + throttle.MAX_RETRY_AFTER)
    assert limiter.in_flight == 0


def _no_proxy_env(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    # only the environment, never this machine's system proxy settings
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)


def test_the_default_transport_uses_the_environment_https_proxy(monkeypatch):
    # passing transport= to httpx.Client turns off its own env-proxy support, so we must do it
    _no_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    inner = throttle.ThrottledTransport()._inner
    assert isinstance(inner._pool, httpcore.HTTPProxy)


def test_the_default_transport_accepts_a_scheme_less_proxy(monkeypatch):
    # httpx's own env handling prepends http:// to a bare host:port, so we do the same
    _no_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "proxy.example:3128")
    inner = throttle.ThrottledTransport()._inner
    assert isinstance(inner._pool, httpcore.HTTPProxy)


def test_the_default_transport_connects_directly_without_a_proxy(monkeypatch):
    _no_proxy_env(monkeypatch)
    inner = throttle.ThrottledTransport()._inner
    assert not isinstance(inner._pool, httpcore.HTTPProxy)


@respx.mock
def test_max_in_flight_caps_requests_across_groups():
    # with a cap of 1, the lever request can't start while the greenhouse one is in flight
    import threading

    greenhouse_in, lever_started = threading.Event(), threading.Event()
    overlapped = []

    def greenhouse(request):
        greenhouse_in.set()
        overlapped.append(lever_started.wait(0.5))  # times out only if lever is held back
        return httpx.Response(200)

    def lever(request):
        lever_started.set()
        return httpx.Response(200)

    respx.get(URL).mock(side_effect=greenhouse)
    respx.get("https://api.lever.co/v0/postings/acme").mock(side_effect=lever)
    transport = throttle.ThrottledTransport(max_in_flight=1)
    with httpx.Client(transport=transport) as client:
        first = threading.Thread(target=client.get, args=(URL,))
        first.start()
        assert greenhouse_in.wait(5)
        client.get("https://api.lever.co/v0/postings/acme")
        first.join()
    assert overlapped == [False]


def test_max_rate_spaces_sends_while_the_global_cap_is_full():
    # with every global slot taken, rate-capped requests used to queue there and then go out
    # together; the gap is now taken after the global slot, just before the send
    import threading
    import time

    lever_in = threading.Event()
    sends: list[float] = []

    class Inner(httpx.BaseTransport):
        def handle_request(self, request):
            if "lever" in request.url.host:
                lever_in.set()
                time.sleep(0.4)
            else:
                sends.append(time.monotonic())
            return httpx.Response(200, request=request)

    transport = throttle.ThrottledTransport(
        inner=Inner(), start=6, ceiling=6, max_in_flight=1, max_rate={"workable": 10.0}
    )
    workable = "https://apply.workable.com/api/v1/widget/accounts/acme"
    with httpx.Client(transport=transport) as client:
        first = threading.Thread(target=client.get, args=("https://api.lever.co/v0/postings/acme",))
        first.start()
        assert lever_in.wait(5)
        threads = [threading.Thread(target=client.get, args=(workable,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads + [first]:
            t.join(5)
    assert len(sends) == 4
    gaps = [b - a for a, b in zip(sends, sends[1:], strict=False)]
    assert min(gaps) >= 0.09, gaps


@pytest.mark.parametrize("max_in_flight", [32, None])  # with and without the global slots
def test_a_pause_set_while_spacing_holds_back_the_spaced_sends(max_in_flight):
    # requests sleeping in space() when a 429 paused the group used to go out inside the pause
    import threading
    import time

    lock = threading.Lock()
    sends: list[float] = []
    paused_at: list[float] = []

    class Inner(httpx.BaseTransport):
        def handle_request(self, request):
            with lock:
                sends.append(time.monotonic())
                first = len(sends) == 1
            if first:
                time.sleep(0.3)
                paused_at.append(time.monotonic())
                return httpx.Response(429, headers={"retry-after": "1"}, request=request)
            return httpx.Response(200, request=request)

    transport = throttle.ThrottledTransport(
        inner=Inner(), start=6, ceiling=6, max_in_flight=max_in_flight, max_rate={"workable": 2.0}
    )
    workable = "https://apply.workable.com/api/v1/widget/accounts/acme"
    with httpx.Client(transport=transport) as client:
        threads = [threading.Thread(target=client.get, args=(workable,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert len(sends) == 5  # the 429 is retried once; the waits aren't retries
    pause_end = paused_at[0] + 1.0
    assert min(sends[1:]) >= pause_end - 0.02, [round(s - sends[0], 2) for s in sends]
    assert transport.stats()["workable"]["throttles"] == 1
    assert transport.stats()["workable"]["requests"] == 5
    # the held-back sends gave their group slot back, neutrally: one 429 halved 6 to 3, then 4 successes
    assert transport.limiter("workable").in_flight == 0
    assert transport.limiter("workable").limit == pytest.approx(4.164, abs=0.01)


def test_limits_size_the_connection_pool(monkeypatch):
    _no_proxy_env(monkeypatch)
    transport = throttle.ThrottledTransport(limits=httpx.Limits(max_connections=32))
    assert transport._inner._pool._max_connections == 32


@respx.mock
def test_stats_per_group():
    clock = FakeClock()
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200)])
    respx.get("https://api.lever.co/v0/postings/acme").mock(return_value=httpx.Response(200, json=[]))
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    with httpx.Client(transport=transport) as client:
        client.get(URL)
        client.get("https://api.lever.co/v0/postings/acme")
    stats = transport.stats()
    assert stats["greenhouse"] == {"requests": 2, "throttles": 1, "max_in_flight": 1, "limit": 2.0}
    assert stats["lever"]["requests"] == 1 and stats["lever"]["throttles"] == 0


# ------------------------------------------------------------------ stopping

STOP_WAIT = 5  # seconds; only reached if a test is broken


def _acquire_in_thread(dispatcher, lim, share="g"):
    """Start ``dispatcher.acquire(lim, share)`` on a thread; returns it and what it raised."""
    import threading

    raised: list[BaseException] = []

    def run():
        try:
            dispatcher.acquire(lim, share)
        except BaseException as e:
            raised.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, raised


def test_a_request_waiting_out_a_pause_wakes_and_raises_when_stopped():
    import threading

    stop, sleeping = threading.Event(), threading.Event()

    def sleep(seconds):  # the real wait, announced
        sleeping.set()
        stop.wait(seconds)

    dispatcher = throttle.Dispatcher(None, sleep=sleep, stop=stop)
    lim = throttle.GroupLimiter(start=2, ceiling=6)
    dispatcher.acquire(lim, "g")
    lim.release(throttled=True, retry_after=100)
    dispatcher.release()
    thread, raised = _acquire_in_thread(dispatcher, lim)
    assert sleeping.wait(STOP_WAIT)
    stop.set()
    dispatcher.wake_all()
    thread.join(STOP_WAIT)
    assert not thread.is_alive()
    assert [type(e) for e in raised] == [throttle.Stopped]
    assert lim.in_flight == 0 and dispatcher.waiting() == 0


def test_a_request_waiting_for_a_slot_wakes_and_raises_when_stopped():
    import threading

    stop = threading.Event()
    dispatcher = throttle.Dispatcher(None, stop=stop)
    lim = throttle.GroupLimiter(start=1, ceiling=1)
    dispatcher.acquire(lim, "g")  # the only slot, never released
    thread, raised = _acquire_in_thread(dispatcher, lim)
    stop.set()
    dispatcher.wake_all()
    thread.join(STOP_WAIT)
    assert not thread.is_alive()
    assert [type(e) for e in raised] == [throttle.Stopped]
    assert lim.in_flight == 1 and dispatcher.waiting() == 0


def test_stopped_is_not_an_http_error():
    # so a description fetch's `except httpx.HTTPError` doesn't report it as a missing description
    assert not issubclass(throttle.Stopped, httpx.HTTPError)


@pytest.mark.parametrize("how", ["stop", "close"])
def test_a_stopped_transport_sends_nothing(how):
    sent = []
    transport = throttle.ThrottledTransport(inner=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    client = httpx.Client(transport=transport)
    assert client.get(URL).status_code == 200
    getattr(transport, how)()
    with pytest.raises(throttle.Stopped):
        transport.handle_request(httpx.Request("GET", URL))
    assert len(sent) == 1
    assert transport.limiter("greenhouse").in_flight == 0


def test_a_request_waiting_for_a_global_slot_is_not_sent_after_a_stop():
    import threading

    first_in, finish_first = threading.Event(), threading.Event()
    sent = []

    def handler(request):
        sent.append(request.url)
        if len(sent) == 1:
            first_in.set()
            assert finish_first.wait(STOP_WAIT)
        return httpx.Response(200)

    transport = throttle.ThrottledTransport(inner=httpx.MockTransport(handler), max_in_flight=1)
    first = threading.Thread(target=transport.handle_request, args=(httpx.Request("GET", URL),), daemon=True)
    first.start()
    assert first_in.wait(STOP_WAIT)
    raised: list[BaseException] = []

    def second():
        try:
            transport.handle_request(httpx.Request("GET", "https://api.lever.co/v0/postings/acme"))
        except BaseException as e:
            raised.append(e)

    waiting = threading.Thread(target=second, daemon=True)
    waiting.start()  # holds lever's slot, then waits for the one global slot
    transport.stop()
    finish_first.set()  # the request in flight finishes; the waiting one must not go out
    first.join(STOP_WAIT)
    waiting.join(STOP_WAIT)
    assert not waiting.is_alive()
    assert [type(e) for e in raised] == [throttle.Stopped]
    assert len(sent) == 1


@respx.mock
def test_retry_limits_are_constructor_parameters():
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(429))
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, jitter=lambda: 0.0, max_retries=1)
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 429
    assert route.call_count == 2

    route.mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    transport = throttle.ThrottledTransport(clock=clock, sleep=clock.sleep, max_retry_after=10)
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 429
    assert route.call_count == 3  # 30 s > the 10 s cap: not retried


def test_cooldown_is_a_limiter_parameter():
    clock = FakeClock()
    lim = throttle.GroupLimiter(start=4, ceiling=6, clock=clock, cooldown=0)
    for _ in range(2):
        lim.try_take(clock())
    lim.release(throttled=True)
    lim.release(throttled=True)
    assert lim.limit == 1  # no cooldown: both halvings count


@respx.mock
def test_the_transport_gives_its_limiters_its_cooldown():
    clock = FakeClock()
    respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "0"}))
    transport = throttle.ThrottledTransport(start=4, clock=clock, sleep=clock.sleep, max_retries=1, cooldown=0)
    with httpx.Client(transport=transport) as client:
        assert client.get(URL).status_code == 429
    (stats,) = transport.stats().values()
    assert stats["limit"] == 1  # two 429s, no cooldown: halved twice


def test_connect_retries_sets_the_default_inner_transports_retries(monkeypatch):
    _no_proxy_env(monkeypatch)
    assert throttle.ThrottledTransport()._inner._pool._retries == 1
    assert throttle.ThrottledTransport(connect_retries=0)._inner._pool._retries == 0


# ------------------------------------------------------------------ sharing the global slots


def _dispatcher(max_in_flight, clock=None):
    clock = clock or FakeClock()
    return throttle.Dispatcher(max_in_flight, clock=clock, sleep=clock.sleep), clock


def test_a_waiter_is_granted_its_group_slot_and_a_global_slot_together():
    d, clock = _dispatcher(1)
    a, b = _limiter(clock, start=6, ceiling=6), _limiter(clock, start=6, ceiling=6)
    first = d.enqueue(a, "a")
    second = d.enqueue(b, "b")
    assert first.granted and not second.granted
    assert (a.in_flight, b.in_flight, d.free) == (1, 0, 0)  # a waiter holds no group slot
    a.release()
    d.release()
    assert second.granted and (a.in_flight, b.in_flight, d.free) == (0, 1, 0)


def test_freed_slots_go_to_the_classes_in_turn_not_to_the_one_with_most_waiters():
    d, clock = _dispatcher(1)
    holder = _limiter(clock, start=6, ceiling=6)
    d.enqueue(holder, "holder")
    tenants = [_limiter(clock, start=6, ceiling=6) for _ in range(5)]
    oracle = [d.enqueue(lim, "oracle") for lim in tenants]  # five tenants, one class
    big = _limiter(clock, start=6, ceiling=6)
    workday = [d.enqueue(big, "workday:wd1") for _ in range(5)]
    order = []
    for _ in range(6):
        d.release()  # the request that held the one global slot finished
        granted = [w for w in oracle + workday if w.granted and w not in order]
        assert len(granted) == 1
        order.append(granted[0])
    classes = ["oracle" if w in oracle else "workday" for w in order]
    assert classes == ["oracle", "workday", "oracle", "workday", "oracle", "workday"]


def test_one_big_group_gets_half_the_slots_against_fifty_one_board_tenants():
    d, clock = _dispatcher(4)
    big = _limiter(clock, start=6, ceiling=6)
    waiters = [(_limiter(clock, start=6, ceiling=6), "oracle") for _ in range(50)]
    waiters += [(big, "greenhouse")] * 50
    waiters = [(lim, d.enqueue(lim, share)) for lim, share in waiters]
    running = [pair for pair in waiters if pair[1].granted]
    started = list(running)
    for _ in range(40):  # the oldest request finishes; its slots go to the next class in turn
        lim, _ = running.pop(0)
        lim.release()
        d.release()
        for pair in waiters:
            if pair[1].granted and pair not in started:
                started.append(pair)
                running.append(pair)
    assert len(started) == 44
    assert 20 <= sum(lim is big for lim, _ in started) <= 24  # not 4 of 44, as by arrival


def test_a_rate_capped_waiter_holds_no_global_slot_while_its_gap_runs():
    d, clock = _dispatcher(2)
    workable = _limiter(clock, start=6, ceiling=6, rate=1.0)
    lever = _limiter(clock, start=6, ceiling=6)
    first, spaced = d.enqueue(workable, "workable"), d.enqueue(workable, "workable")
    other = d.enqueue(lever, "lever")
    assert first.granted and not spaced.granted and other.granted  # the second slot went to lever
    assert d.free == 0


def test_a_waiter_sleeps_out_its_groups_gap_then_goes():
    d, clock = _dispatcher(None)
    workable = _limiter(clock, start=6, ceiling=6, rate=2.0)
    for _ in range(3):
        d.acquire(workable, "workable")
    assert clock.slept == [0.5, 0.5]


def test_a_paused_group_waits_and_the_others_go_ahead():
    d, clock = _dispatcher(1)
    paused = _limiter(clock, start=6, ceiling=6)
    paused.try_take(clock())
    paused.release(throttled=True, retry_after=30)
    held = d.enqueue(paused, "paused")
    other = d.enqueue(_limiter(clock, start=6, ceiling=6), "other")
    assert not held.granted and other.granted


@respx.mock
def test_the_transport_shares_global_slots_by_class():
    # one global slot, held by lever; meanwhile three oracle tenants and greenhouse queue up
    import threading
    import time

    order: list[str] = []
    lever_in, finish_lever = threading.Event(), threading.Event()

    def lever(request):
        lever_in.set()
        assert finish_lever.wait(STOP_WAIT)
        return httpx.Response(200)

    def record(request):
        order.append(request.url.host)
        return httpx.Response(200)

    respx.get("https://api.lever.co/v0/postings/acme").mock(side_effect=lever)
    respx.get(url__regex=r"https://t\d\.fa\.us2\.oraclecloud\.com/x").mock(side_effect=record)
    respx.get(URL).mock(side_effect=record)
    transport = throttle.ThrottledTransport(max_in_flight=1)
    client = httpx.Client(transport=transport)
    threads = [threading.Thread(target=client.get, args=("https://api.lever.co/v0/postings/acme",))]
    threads[0].start()
    assert lever_in.wait(STOP_WAIT)
    urls = [f"https://t{i}.fa.us2.oraclecloud.com/x" for i in range(3)] + [URL, URL]
    for n, url in enumerate(urls, start=1):
        threads.append(threading.Thread(target=client.get, args=(url,)))
        threads[-1].start()
        while transport._dispatcher.waiting() < n:  # queued in this order
            time.sleep(0.001)
    finish_lever.set()
    for t in threads:
        t.join(STOP_WAIT)
    classes = ["greenhouse" if host == "boards-api.greenhouse.io" else "oracle" for host in order]
    assert classes == ["oracle", "greenhouse", "oracle", "greenhouse", "oracle"]


def test_max_in_flight_is_readable():
    assert throttle.ThrottledTransport(max_in_flight=3).max_in_flight == 3
    assert throttle.ThrottledTransport().max_in_flight is None


def test_a_pause_wakes_its_groups_waiters_to_sleep_it_out():
    # a waiter for a full group is woken when a 429 pauses that group, then goes when the
    # pause ends, rather than at its safety poll
    import time

    dispatcher = throttle.Dispatcher(None)
    lim = throttle.GroupLimiter(start=1, ceiling=1)
    dispatcher.acquire(lim, "g")  # the group's only slot
    thread, raised = _acquire_in_thread(dispatcher, lim)
    while dispatcher.waiting() < 1:
        time.sleep(0.001)
    started = time.monotonic()
    lim.release(throttled=True, retry_after=0.2)
    dispatcher.release(lim)
    thread.join(STOP_WAIT)
    assert not thread.is_alive() and not raised
    assert 0.15 <= time.monotonic() - started < 1.0
    assert lim.in_flight == 1
