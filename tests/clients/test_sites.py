import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx2

from kisetsu.clients import sites

NYAA_FEED = "https://nyaa.si/?page=rss&q=1080p+-HEVC&c=1_2&f=0&u=Erai-raws"
SUBS_FEED = "https://subsplease.org/rss/?t&r=1080"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

NYAA_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><title>search</title>
<item><title>[Erai-raws] Sousou no Frieren - 07 [1080p][CE60799F]</title>
<link>https://nyaa.si/download/2171496.torrent</link>
<guid isPermaLink="true">https://nyaa.si/view/2171496</guid>
<pubDate>Fri, 09 Oct 2026 12:00:00 +0000</pubDate>
<nyaa:infoHash>f83da926a56300ef7925f3b1f716f374aa0692d0</nyaa:infoHash></item>
</channel></rss>"""

SUBS_PAYLOAD = {
    "Frieren - 07": {
        "show": "Frieren",
        "release_date": "Fri, 09 Oct 2026 12:00:00 +0000",
        "downloads": [
            {"res": "720", "magnet": "magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&dn=%5BSubsPlease%5D%20Frieren%20-%2007%20%28720p%29.mkv"},
            {"res": "1080", "magnet": "magnet:?xt=urn:btih:VYR2IC6LOQ2Z5IBYGK2KZY4C6F6JYS6W&dn=%5BSubsPlease%5D%20Frieren%20-%2007%20%281080p%29%20%5B151C1660%5D.mkv"},
        ],
    }
}


def test_search_terms_split_titles_and_drop_season_markers():
    steel = [
        "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
        "STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
    ]
    assert sites.search_terms(steel) == [
        "JoJo no Kimyou na Bouken",
        "Steel Ball Run",
        "STEEL BALL RUN JoJo's Bizarre Adventure",
    ]
    assert sites.search_terms(["Koori no Jouheki 2nd Season", "The Ramparts of Ice Season 2"]) == [
        "Koori no Jouheki", "The Ramparts of Ice",
    ]
    assert sites.search_terms([None, "K", ""]) == []
    # A name in another script finds nothing a Latin name does not; alone, it is all there is to ask for.
    assert sites.search_terms(["Yasei no Last Boss", "野生のラスボスが現れた 第2期", "A Wild Last Boss Appeared"]) == ["Yasei no Last Boss", "A Wild Last Boss Appeared"]
    assert sites.search_terms(["野生のラスボスが現れた"]) == ["野生のラスボスが現れた"]


def test_nyaa_search_url_extends_the_feed_query():
    query = parse_qs(urlparse(sites.nyaa_search_url(NYAA_FEED, "Steel Ball Run")).query)
    assert query["q"] == ["1080p -HEVC Steel Ball Run"]
    assert query["u"] == ["Erai-raws"]
    assert query["c"] == ["1_2"]
    assert query["page"] == ["rss"]
    bare = parse_qs(urlparse(sites.nyaa_search_url("https://nyaa.si/?page=rss&u=varyg1001", "Frieren")).query)
    assert bare["q"] == ["Frieren"]
    assert bare["u"] == ["varyg1001"]


def test_several_names_are_one_nyaa_search_that_keeps_the_feeds_own_filter():
    term = sites.any_of(["Kusuriya no Hitorigoto", "The Apothecary Diaries"])
    assert term == "((Kusuriya no Hitorigoto)|(The Apothecary Diaries))"
    # Grouped, the alternatives stay inside the feed's query: "1080p -HEVC" still applies to each.
    query = parse_qs(urlparse(sites.nyaa_search_url(NYAA_FEED, term)).query)
    assert query["q"] == ["1080p -HEVC ((Kusuriya no Hitorigoto)|(The Apothecary Diaries))"]
    assert sites.any_of(["Frieren"]) == "Frieren"


def test_whether_a_site_takes_grouped_names_is_checked_again_after_a_day():
    now = [0.0]
    grouping = sites.Grouping(clock=lambda: now[0])
    assert grouping.works("nyaa.si") is None
    grouping.record("nyaa.si", False)
    assert grouping.works("nyaa.si") is False and grouping.works("subsplease.org") is None
    now[0] += sites.GROUPING_RECHECK_SECONDS + 1
    assert grouping.works("nyaa.si") is None


def test_a_page_is_complete_only_when_empty_or_smaller_than_one_the_host_returned_before():
    grouping = sites.Grouping()
    # Nothing is known of the host's page size yet: only an empty page is surely complete.
    assert grouping.complete("nyaa.si", 0) and not grouping.complete("nyaa.si", 5)
    grouping.saw("nyaa.si", 30)
    assert grouping.complete("nyaa.si", 29) and not grouping.complete("nyaa.si", 30)
    assert not grouping.complete("other.example", 5)


def test_one_search_at_a_time_checks_a_site():
    grouping = sites.Grouping()
    assert grouping.claim("nyaa.si") and not grouping.claim("nyaa.si") and grouping.claim("subsplease.org")
    grouping.release("nyaa.si")
    assert grouping.claim("nyaa.si")
    grouping.release("nyaa.si")
    grouping.record("nyaa.si", True)
    assert not grouping.claim("nyaa.si")


def test_only_known_sites_are_searched():
    assert sites.site_of(NYAA_FEED) == "nyaa"
    assert sites.site_of(SUBS_FEED) == "subsplease"
    assert sites.site_of("https://example.com/rss") is None
    assert sites.site_of("https://nyaa.si/view/1") is None


def test_subsplease_results_use_the_rss_ids_and_the_feeds_resolution():
    articles = sites.parse_subsplease_search(SUBS_PAYLOAD, "1080", now=NOW)
    assert len(articles) == 1
    a = articles[0]
    assert a.guid == "VYR2IC6LOQ2Z5IBYGK2KZY4C6F6JYS6W"
    assert a.title == "[SubsPlease] Frieren - 07 (1080p) [151C1660].mkv"
    assert a.torrent_url.startswith("magnet:?xt=urn:btih:VYR2IC6")
    assert len(a.info_hash) == 40
    assert a.published_at == datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert sites.parse_subsplease_search([], "1080") == []


def test_subsplease_search_dates_are_never_in_the_future():
    payload = {"x": {"release_date": "Sat, 10 Oct 2026 19:00:00 +0000", "downloads": [
        {"res": "1080", "magnet": "magnet:?xt=urn:btih:VYR2IC6LOQ2Z5IBYGK2KZY4C6F6JYS6W&dn=%5BSubsPlease%5D%20Show%20-%2001.mkv"},
    ]}}
    assert sites.parse_subsplease_search(payload, "1080", now=NOW)[0].published_at == NOW


def test_the_resolution_comes_from_the_feed_url():
    assert sites.subsplease_resolution("https://subsplease.org/rss/?t&r=720") == "720"
    assert sites.subsplease_resolution("https://subsplease.org/rss/?t") == "1080"


def _search(handler, feed_url):
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await sites.search_feed(client, feed_url, "Frieren", now=NOW)

    return asyncio.run(run())


def test_every_search_attempt_is_counted_with_its_outcome():
    from kisetsu.clients import traffic

    traffic.reset()
    answers = [httpx2.Response(429, headers={"Retry-After": "0"}), httpx2.Response(200, content=NYAA_BODY)]
    _search(lambda request: answers.pop(0), NYAA_FEED)
    [row] = traffic.snapshot()["rows"]
    assert (row["kind"], row["host"], row["requests"], row["ok"], row["rate_limited"]) == ("Search", "nyaa.si", 2, 1, 1)


def test_a_nyaa_search_requests_the_extended_query_and_parses_the_items():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx2.Response(200, content=NYAA_BODY)

    articles = _search(handler, NYAA_FEED)
    assert parse_qs(urlparse(seen[0]).query)["q"] == ["1080p -HEVC Frieren"]
    assert [a.guid for a in articles] == ["https://nyaa.si/view/2171496"]


def test_a_subsplease_search_uses_its_api_and_filters_by_resolution():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx2.Response(200, content=json.dumps(SUBS_PAYLOAD).encode())

    articles = _search(handler, SUBS_FEED)
    assert "subsplease.org/api/" in seen[0]
    assert len(articles) == 1


def test_a_subsplease_search_with_no_results_is_empty():
    assert _search(lambda request: httpx2.Response(200, content=b"[]"), SUBS_FEED) == []
    assert _search(lambda request: httpx2.Response(200, content=b"not json"), SUBS_FEED) == []


def test_an_unsearchable_feed_returns_none_without_a_request():
    def handler(request):
        raise AssertionError("no request expected")

    assert _search(handler, "https://example.com/rss") is None


def test_what_was_learned_of_a_host_is_kept_across_a_restart_until_it_is_due_again():
    now = [1_000_000.0]
    clock = lambda: now[0]  # noqa: E731
    before = sites.Grouping(clock=clock)
    before.saw("nyaa.si", 75)
    before.record("nyaa.si", True)
    after = sites.Grouping(clock=clock)
    after.load(before.dump())
    assert after.works("nyaa.si") is True and after.complete("nyaa.si", 74) and not after.claim("nyaa.si")
    now[0] += sites.GROUPING_RECHECK_SECONDS + 1
    later = sites.Grouping(clock=clock)
    later.load(before.dump())
    # Due again: checked anew, and its page size learned anew with it.
    assert later.works("nyaa.si") is None and not later.complete("nyaa.si", 74)
    # A page size seen in this run is not forgotten for a smaller one that was kept.
    seen = sites.Grouping(clock=clock)
    seen.saw("nyaa.si", 100)
    now[0] -= sites.GROUPING_RECHECK_SECONDS + 1
    seen.load(before.dump())
    assert seen.works("nyaa.si") is True and seen.complete("nyaa.si", 75)
    for broken in ("", "not json", "[]", '{"checked": 1}'):
        sites.Grouping(clock=clock).load(broken)


def test_subsplease_schedule_titles_are_read_from_every_day():
    payload = {"tz": "UTC", "schedule": {"Monday": [{"title": "Kanata kara", "time": "12:00"}], "Friday": [{"title": "Sasaki to Pii-chan S2"}, {"x": 1}]}}
    assert sites.parse_subsplease_schedule(payload) == ["Kanata kara", "Sasaki to Pii-chan S2"]
    assert sites.parse_subsplease_schedule({"schedule": {}}) is None
    assert sites.parse_subsplease_schedule([]) is None
