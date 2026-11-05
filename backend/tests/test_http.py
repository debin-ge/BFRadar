import httpx
import pytest

from app.config import load_settings
from app.http import FetchError, HttpClient


def make_client(handler, sleeps):
    return HttpClient(load_settings({}, env_file=None), transport=httpx.MockTransport(handler), sleep=sleeps.append)


def sequence(*responses):
    """依次返回给定响应；元素为异常时抛出。"""
    it = iter(responses)

    def handler(request):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def test_success_sends_user_agent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["User-Agent"]
        return httpx.Response(200, text="<rss/>")

    sleeps = []
    with make_client(handler, sleeps) as client:
        assert client.get("https://example.com/feed").text == "<rss/>"
    assert seen["ua"].startswith("Mozilla/5.0 (compatible; BFRadar/")
    assert sleeps == []


def test_retries_5xx_with_backoff_then_succeeds():
    sleeps = []
    client = make_client(sequence(httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")), sleeps)
    assert client.get("https://example.com/").text == "ok"
    assert sleeps == [5.0, 10.0]


@pytest.mark.parametrize("headers, expected", [
    ({"Retry-After": "7"}, 7.0),
    ({"Retry-After": "600"}, 60.0),
    ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 5.0),
    ({"X-RateLimit-Reset": "53"}, 53.0),  # Reddit
    ({"Retry-After": "3", "X-RateLimit-Reset": "53"}, 3.0),
])
def test_retry_after_is_respected_and_capped(headers, expected):
    sleeps = []
    client = make_client(sequence(httpx.Response(429, headers=headers), httpx.Response(200)), sleeps)
    client.get("https://example.com/")
    assert sleeps == [expected]


def test_gives_up_after_retries():
    sleeps = []
    client = make_client(lambda request: httpx.Response(500), sleeps)
    with pytest.raises(FetchError) as e:
        client.get("https://example.com/")
    assert e.value.status == 500
    assert sleeps == [5.0, 10.0]


def test_client_errors_are_not_retried():
    sleeps = []
    client = make_client(lambda request: httpx.Response(404), sleeps)
    with pytest.raises(FetchError) as e:
        client.get("https://example.com/missing")
    assert e.value.status == 404
    assert sleeps == []


@pytest.mark.parametrize("response", [
    httpx.Response(403, text="<html><head><title>Just a moment...</title></head></html>"),
    httpx.Response(403, headers={"cf-mitigated": "challenge"}, text=""),
    httpx.Response(200, text="<title>Attention Required! | Cloudflare</title>"),
])
def test_cloudflare_challenge_fails_fast(response):
    sleeps = []
    client = make_client(lambda request: response, sleeps)
    with pytest.raises(FetchError, match="Cloudflare"):
        client.get("https://lowendtalk.com/categories/offers/feed.rss")
    assert sleeps == []


def test_network_errors_are_retried():
    sleeps = []
    client = make_client(sequence(httpx.ConnectError("boom"), httpx.Response(200, text="ok")), sleeps)
    assert client.get("https://example.com/").text == "ok"
    assert sleeps == [5.0]

    client = make_client(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), sleeps := [])
    with pytest.raises(FetchError, match="ReadTimeout"):
        client.get("https://example.com/")
    assert sleeps == [5.0, 10.0]
