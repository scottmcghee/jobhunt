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
    return throttle.GroupLimiter(clock=clock, sleep=clock.sleep, **kw)


def test_limit_grows_on_success_up_to_the_ceiling():
    lim = _limiter(FakeClock(), start=2, ceiling=4)
    for _ in range(50):
        lim.acquire()
        lim.release(throttled=False)
    assert lim.limit == 4


def test_a_throttle_halves_the_limit_but_not_below_one():
    clock = FakeClock()
    lim = _limiter(clock, start=4, ceiling=6)
    lim.acquire()
    lim.release(throttled=True, retry_after=0)
    assert lim.limit == 2
    clock.now += 60  # past the cooldown
    for _ in range(3):
        lim.acquire()
        lim.release(throttled=True, retry_after=0)
        clock.now += 60
    assert lim.limit == 1


def test_a_burst_of_throttles_halves_once_per_cooldown():
    # requests already in flight when the server starts refusing shouldn't collapse the limit
    lim = _limiter(FakeClock(), start=4, ceiling=6)
    for _ in range(3):
        lim.acquire()
    for _ in range(3):
        lim.release(throttled=True, retry_after=0)
    assert lim.limit == 2


def test_acquire_waits_out_a_pause():
    clock = FakeClock()
    lim = _limiter(clock, start=2, ceiling=6)
    lim.acquire()
    lim.release(throttled=True, retry_after=7)
    lim.acquire()  # must not start before the pause ends
    assert sum(clock.slept) == pytest.approx(7)


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
def test_503_is_retried_only_with_retry_after():
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with _client(clock) as client:
        assert client.get(URL).status_code == 503
    assert route.call_count == 1

    route.mock(side_effect=[httpx.Response(503, headers={"Retry-After": "2"}), httpx.Response(200)])
    with _client(clock) as client:
        assert client.get(URL).status_code == 200


@respx.mock
def test_502_and_504_are_retried_once():
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(502))
    with _client(clock) as client:
        assert client.get(URL).status_code == 502
    assert route.call_count == 2

    route.mock(side_effect=[httpx.Response(504), httpx.Response(200)])
    with _client(clock) as client:
        assert client.get(URL).status_code == 200


@respx.mock
def test_other_errors_pass_straight_through():
    clock = FakeClock()
    for status in (500, 404, 403):
        route = respx.get(URL).mock(return_value=httpx.Response(status))
        with _client(clock) as client:
            assert client.get(URL).status_code == status
        assert route.call_count == 1
        respx.reset()
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


def test_limits_size_the_connection_pool(monkeypatch):
    _no_proxy_env(monkeypatch)
    transport = throttle.ThrottledTransport(limits=httpx.Limits(max_connections=32))
    assert transport._inner._pool._max_connections == 32
