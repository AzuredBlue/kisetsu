import asyncio
from datetime import datetime, timedelta, timezone

import httpx2
import pytest

from kisetsu.clients import rss
from kisetsu.clients.rss import (
    Backoff,
    FeedFetchError,
    HostGate,
    fetch_feed,
    normalize_info_hash,
    parse_date,
    parse_feed,
)

NYAA = b"""<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:nyaa="https://nyaa.si/xmlns/nyaa" version="2.0">
<channel><title>Nyaa</title>
<item>
<title>[ToonsHub] Even the Student Council Has Its Holes S01E04 1080p UNCENSORED ADN WEB-DL</title>
<link>https://nyaa.si/download/2178190.torrent</link>
<guid isPermaLink="true">https://nyaa.si/view/2178190</guid>
<pubDate>Sat, 24 Oct 2026 17:19:47 -0000</pubDate>
<nyaa:infoHash>a0fee4b4b218cb7d6e6b50c7380f0476e9e9309d</nyaa:infoHash>
</item>
<item><title>no link</title></item>
</channel></rss>"""

SUBSPLEASE = b"""<rss version="2.0" xmlns:subsplease="https://subsplease.org/rss"><channel>
<item><title>[SubsPlease] PSYREN - 04 (1080p) [1A417A22].mkv</title>
<link>https://nyaa.si/view/2179056/torrent</link>
<guid isPermaLink="false">HWYV3VXS4V2Y5IZX5AALSWZ6S2VJ7VTH</guid>
<pubDate>Mon, 26 Oct 2026 07:32:02 +0000</pubDate></item>
</channel></rss>"""


def test_a_nyaa_item_carries_its_hash_date_and_torrent_link():
    articles = parse_feed(NYAA)
    assert len(articles) == 1
    a = articles[0]
    assert a.guid == "https://nyaa.si/view/2178190"
    assert a.torrent_url == "https://nyaa.si/download/2178190.torrent"
    assert a.info_hash == "a0fee4b4b218cb7d6e6b50c7380f0476e9e9309d"
    assert a.published_at == datetime(2026, 10, 24, 17, 19, 47, tzinfo=timezone.utc)


def test_a_subsplease_item_takes_its_hash_from_the_base32_guid():
    a = parse_feed(SUBSPLEASE)[0]
    assert a.info_hash == normalize_info_hash("HWYV3VXS4V2Y5IZX5AALSWZ6S2VJ7VTH")
    assert len(a.info_hash) == 40
    assert a.guid == "HWYV3VXS4V2Y5IZX5AALSWZ6S2VJ7VTH"


def test_a_magnet_link_supplies_the_hash():
    body = (
        b"<rss><channel><item><title>x - 01</title>"
        b"<link>magnet:?xt=urn:btih:A0FEE4B4B218CB7D6E6B50C7380F0476E9E9309D&amp;dn=x</link></item></channel></rss>"
    )
    assert parse_feed(body)[0].info_hash == "a0fee4b4b218cb7d6e6b50c7380f0476e9e9309d"


def test_invalid_xml_is_a_fetch_error():
    with pytest.raises(FeedFetchError):
        parse_feed(b"<html>Cloudflare</html")


def test_dates_without_a_weekday_or_with_a_negative_zero_zone_parse_as_utc():
    assert parse_date("06 Sep 2026 15:46:09 +0000") == datetime(2026, 9, 6, 15, 46, 9, tzinfo=timezone.utc)
    assert parse_date("Sat, 24 Oct 2026 17:19:47 -0000") == datetime(2026, 10, 24, 17, 19, 47, tzinfo=timezone.utc)
    assert parse_date("Sat, 24 Oct 2026 19:19:47 +0200") == datetime(2026, 10, 24, 17, 19, 47, tzinfo=timezone.utc)
    assert parse_date("") is None
    assert parse_date("not a date") is None


def test_hashes_normalise_from_hex_and_base32_only():
    assert normalize_info_hash("A0FEE4B4B218CB7D6E6B50C7380F0476E9E9309D") == "a0fee4b4b218cb7d6e6b50c7380f0476e9e9309d"
    assert normalize_info_hash("short") is None
    assert normalize_info_hash(None) is None
    assert normalize_info_hash("https://nyaa.si/view/1") is None


def _fetch(handler, url="https://nyaa.si/?page=rss"):
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await fetch_feed(client, url)

    return asyncio.run(run())


def test_fetch_returns_the_parsed_items():
    assert len(_fetch(lambda request: httpx2.Response(200, content=NYAA))) == 1


@pytest.mark.parametrize("status,message", [(429, "rate limiting"), (503, "HTTP 503"), (404, "HTTP 404")])
def test_http_failures_are_fetch_errors(status, message):
    with pytest.raises(FeedFetchError, match=message):
        _fetch(lambda request: httpx2.Response(status))


def test_a_connection_failure_is_a_fetch_error():
    def refuse(request):
        raise httpx2.ConnectError("refused")

    with pytest.raises(FeedFetchError, match="could not be reached"):
        _fetch(refuse)


def test_a_timeout_is_a_fetch_error():
    def slow(request):
        raise httpx2.ReadTimeout("slow")

    with pytest.raises(FeedFetchError, match="did not answer"):
        _fetch(slow)


def test_an_oversized_feed_is_refused(monkeypatch):
    monkeypatch.setattr("kisetsu.clients.rss.MAX_BODY_BYTES", 10)
    with pytest.raises(FeedFetchError, match="larger"):
        _fetch(lambda request: httpx2.Response(200, content=NYAA))


def test_backoff_doubles_up_to_its_cap_and_resets():
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    backoff = Backoff(base=timedelta(minutes=1), cap=timedelta(minutes=5))
    assert backoff.ready("f", now)
    assert backoff.failed("f", now) == now + timedelta(minutes=1)
    assert backoff.failed("f", now) == now + timedelta(minutes=2)
    assert backoff.failed("f", now) == now + timedelta(minutes=4)
    assert backoff.failed("f", now) == now + timedelta(minutes=5)
    assert not backoff.ready("f", now + timedelta(minutes=4))
    assert backoff.ready("f", now + timedelta(minutes=5))
    backoff.succeeded("f")
    assert backoff.ready("f", now)
    assert backoff.failed("f", now) == now + timedelta(minutes=1)


class _Clock:
    """A monotonic clock that a wait moves forward instead of sleeping."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _gate(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(rss, "_host_gate", HostGate(sleep=clock.sleep, clock=clock))
    return clock


def _answers(*responses):
    calls = []

    def handler(request):
        calls.append(request.url)
        return responses[min(len(calls), len(responses)) - 1]

    return handler, calls


def test_requests_to_one_host_are_not_spaced_apart(monkeypatch):
    clock = _gate(monkeypatch)
    for _ in range(3):
        _fetch(lambda request: httpx2.Response(200, content=NYAA))
    assert clock.slept == []


def test_a_429_is_waited_out_for_as_long_as_the_site_asks_then_retried(monkeypatch):
    clock = _gate(monkeypatch)
    handler, calls = _answers(httpx2.Response(429, headers={"Retry-After": "3"}), httpx2.Response(200, content=NYAA))
    assert len(_fetch(handler)) == 1
    assert clock.slept == [3.0] and len(calls) == 2


def test_a_429_naming_no_wait_waits_the_default(monkeypatch):
    clock = _gate(monkeypatch)
    handler, _ = _answers(httpx2.Response(429), httpx2.Response(200, content=NYAA))
    _fetch(handler)
    assert clock.slept == [rss.RATE_LIMIT_WAIT_SECONDS]


def test_a_site_that_keeps_rate_limiting_is_given_up_on(monkeypatch):
    _gate(monkeypatch)
    handler, calls = _answers(httpx2.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(FeedFetchError, match="rate limiting"):
        _fetch(handler)
    assert len(calls) == rss.RATE_LIMIT_RETRIES + 1


def test_a_long_rate_limit_fails_at_once_and_holds_back_the_hosts_next_requests(monkeypatch):
    clock = _gate(monkeypatch)
    handler, calls = _answers(httpx2.Response(429, headers={"Retry-After": "600"}), httpx2.Response(200, content=NYAA))
    with pytest.raises(FeedFetchError, match="rate limiting"):
        _fetch(handler)
    with pytest.raises(FeedFetchError, match="rate limiting"):
        _fetch(handler)
    assert len(calls) == 1 and clock.slept == []
    clock.now += 600
    assert len(_fetch(handler)) == 1
