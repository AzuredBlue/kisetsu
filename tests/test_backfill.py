import json
from datetime import timedelta
from email.utils import format_datetime
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from kisetsu.core import backfill
from kisetsu.core.feedcache import CachedRssSnapshot
from kisetsu.core.grabber import evaluate_and_grab_releases, sync_show_episodes
from kisetsu.db.models import (
    Episode,
    EpisodeStatus,
    Feed,
    MatchedFeedItem,
    Monitored,
    MonitoredStatus,
    Settings,
    utc_now,
)

NYAA_FEED = "https://nyaa.si/?page=rss&q=1080p+-HEVC&c=1_2&f=0&u=Erai-raws"
SUBS_FEED = "https://subsplease.org/rss/?t&r=1080"

NYAA_BODY = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">
<channel><title>search</title>
<item>
  <title>[Erai-raws] Sousou no Frieren - 07 [1080p][CE60799F]</title>
  <link>https://nyaa.si/download/2171496.torrent</link>
  <guid isPermaLink="true">https://nyaa.si/view/2171496</guid>
  <pubDate>Fri, 09 Oct 2026 08:11:34 -0000</pubDate>
  <nyaa:infoHash>f83da926a56300ef7925f3b1f716f374aa0692d0</nyaa:infoHash>
  <nyaa:size>892.2 MiB</nyaa:size>
</item>
<item>
  <title>[Erai-raws] Another Show - 03 [1080p]</title>
  <link>https://nyaa.si/download/1.torrent</link>
  <guid isPermaLink="true">https://nyaa.si/view/1</guid>
</item>
</channel></rss>"""

SUBS_BODY = json.dumps({
    "Frieren - 07": {
        "show": "Frieren",
        "episode": "07",
        "downloads": [
            {"res": "720", "magnet": "magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&dn=%5BSubsPlease%5D%20Frieren%20-%2007%20%28720p%29.mkv"},
            {"res": "1080", "magnet": "magnet:?xt=urn:btih:VYR2IC6LOQ2Z5IBYGK2KZY4C6F6JYS6W&dn=%5BSubsPlease%5D%20Frieren%20-%2007%20%281080p%29%20%5B151C1660%5D.mkv"},
        ],
    }
})


def _database():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine, Session(engine)


def test_search_terms_split_titles_and_drop_season_markers():
    steel = Monitored(
        anilist_id=1,
        display_name="STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
        title_romaji="JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
        title_english="STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
    )
    assert backfill.search_terms(steel) == [
        "JoJo no Kimyou na Bouken",
        "Steel Ball Run",
        "STEEL BALL RUN JoJo's Bizarre Adventure",
    ]
    ramparts = Monitored(
        anilist_id=2,
        display_name="The Ramparts of Ice Season 2",
        title_romaji="Koori no Jouheki 2nd Season",
        title_english="The Ramparts of Ice Season 2",
    )
    assert backfill.search_terms(ramparts) == ["Koori no Jouheki", "The Ramparts of Ice"]
    # Too short to search for.
    assert backfill.search_terms(Monitored(anilist_id=3, display_name="K", title_romaji="K")) == []


def test_nyaa_search_url_extends_the_feed_query():
    url = backfill.nyaa_search_url(NYAA_FEED, "Steel Ball Run")
    query = parse_qs(urlparse(url).query)
    assert query["q"] == ["1080p -HEVC Steel Ball Run"]
    assert query["u"] == ["Erai-raws"]
    assert query["c"] == ["1_2"]
    assert query["page"] == ["rss"]
    # A feed without a search gets one.
    bare = parse_qs(urlparse(backfill.nyaa_search_url("https://nyaa.si/?page=rss&u=varyg1001", "Frieren")).query)
    assert bare["q"] == ["Frieren"]
    assert bare["u"] == ["varyg1001"]


def test_parse_nyaa_rss_matches_qbittorrent_article_shape():
    articles = backfill.parse_nyaa_rss(NYAA_BODY)
    assert articles[0] == {
        "id": "https://nyaa.si/view/2171496",
        "title": "[Erai-raws] Sousou no Frieren - 07 [1080p][CE60799F]",
        "link": "https://nyaa.si/download/2171496.torrent",
        "torrentURL": "https://nyaa.si/download/2171496.torrent",
        "date": "Fri, 09 Oct 2026 08:11:34 -0000",
        "infoHash": "f83da926a56300ef7925f3b1f716f374aa0692d0",
        "size": "892.2 MiB",
    }
    assert "date" not in articles[1]


def test_parse_subsplease_search_uses_the_rss_ids_and_the_feed_resolution():
    articles = backfill.parse_subsplease_search(json.loads(SUBS_BODY), "1080")
    assert len(articles) == 1
    assert articles[0]["id"] == "VYR2IC6LOQ2Z5IBYGK2KZY4C6F6JYS6W"
    assert articles[0]["title"] == "[SubsPlease] Frieren - 07 (1080p) [151C1660].mkv"
    assert articles[0]["torrentURL"].startswith("magnet:?xt=urn:btih:VYR2IC6")
    assert len(articles[0]["infoHash"]) == 40
    assert "date" not in articles[0]
    # An empty result is a list, not a dict.
    assert backfill.parse_subsplease_search([], "1080") == []


def test_only_known_sites_are_searched():
    assert backfill._site_of(NYAA_FEED) == "nyaa"
    assert backfill._site_of(SUBS_FEED) == "subsplease"
    assert backfill._site_of("https://example.com/rss") is None
    assert backfill._site_of("https://nyaa.si/view/1") is None


def _frieren_setup(session, feeds):
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct", backfill_window_days=14)
    session.add(settings)
    for feed in feeds:
        session.add(feed)
    show = Monitored(
        id=1,
        anilist_id=154587,
        display_name="Sousou no Frieren",
        title_romaji="Sousou no Frieren",
        aliases_json='["Sousou no Frieren", "Frieren"]',
        status=MonitoredStatus.UNCONFIRMED,
        total_episodes=12,
        next_airing_episode=8,
        next_airing_at=utc_now() + timedelta(days=5),
    )
    session.add(show)
    session.commit()
    episodes = sync_show_episodes(session, show)
    episode = next(e for e in episodes if e.episode_number == 7)
    episode.status = EpisodeStatus.MISSED
    episode.air_at = (utc_now() - timedelta(days=2)).replace(tzinfo=None)
    session.add(episode)
    session.commit()
    return settings, show


def _fetch_by_site(requested):
    def fetch(url):
        requested.append(url)
        return SUBS_BODY if "subsplease.org/api" in url else NYAA_BODY
    return fetch


def test_backfill_stores_matching_items_under_the_feeds_own_url_and_throttles():
    backfill._searched.clear()
    engine, session = _database()
    feeds = [
        Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1),
        Feed(id=2, qbit_feed_name="Other", qbit_feed_url="https://example.com/rss", priority=2),
    ]
    _frieren_setup(session, feeds)
    requested = []

    found = backfill.run_backfill(engine, fetch=_fetch_by_site(requested), pause=lambda s: None)

    assert found == {"Sousou no Frieren": 1}
    assert all("nyaa.si" in url for url in requested)
    rows = session.exec(select(MatchedFeedItem)).all()
    assert [(row.feed_url, row.item_id) for row in rows] == [(NYAA_FEED, "https://nyaa.si/view/2171496")]
    # The unrelated release in the same result is not kept.
    assert "Another Show" not in rows[0].title

    # Searched already: nothing is asked again until the repeat interval passes.
    requested.clear()
    assert backfill.run_backfill(engine, fetch=_fetch_by_site(requested), pause=lambda s: None) == {}
    assert requested == []
    # The episode is in the cache now, so there is nothing left to search for, even by hand.
    assert backfill.missing_episodes(engine, 1) == []
    backfill.run_backfill(engine, show_id=1, force=True, fetch=_fetch_by_site(requested), pause=lambda s: None)
    assert requested == []
    session.close()
    engine.dispose()


def test_backfill_skips_shows_with_nothing_outstanding():
    backfill._searched.clear()
    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])
    episode = session.exec(select(Episode).where(Episode.episode_number == 7)).first()
    for status, air_at in (
        (EpisodeStatus.COMPLETED, utc_now() - timedelta(days=2)),
        (EpisodeStatus.WANTED, utc_now() + timedelta(days=3)),
    ):
        episode.status = status
        episode.air_at = air_at.replace(tzinfo=None)
        session.add(episode)
        session.commit()
        others = [e for e in session.exec(select(Episode)).all() if e.id != episode.id]
        for other in others:
            other.status = EpisodeStatus.COMPLETED
            session.add(other)
        session.commit()
        assert backfill.backfill_due(session) == []
    session.close()
    engine.dispose()


def test_locked_show_only_searches_its_locked_feed():
    backfill._searched.clear()
    engine, session = _database()
    feeds = [
        Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1),
        Feed(id=2, qbit_feed_name="Subs", qbit_feed_url=SUBS_FEED, priority=2),
    ]
    _, show = _frieren_setup(session, feeds)
    assert [f.id for f in backfill.backfill_due(session)[0].feeds] == [1, 2]
    backfill._searched.clear()
    show.learned_feed_id = 2
    session.add(show)
    session.commit()
    assert [f.id for f in backfill.backfill_due(session)[0].feeds] == [2]
    session.close()
    engine.dispose()


def test_a_rate_limited_site_stops_the_round_and_is_retried_later():
    backfill._searched.clear()
    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])

    def limited(url):
        raise backfill.BackfillAbort("nyaa.si answered HTTP 429")

    assert backfill.run_backfill(engine, fetch=limited, pause=lambda s: None) == {}
    assert session.exec(select(MatchedFeedItem)).all() == []
    # Not recorded as searched, so the next pass tries again.
    assert backfill.backfill_due(session) != []
    session.close()
    engine.dispose()


def test_a_missed_episode_is_grabbed_once_backfill_finds_its_release():
    backfill._searched.clear()
    engine, session = _database()
    feed = Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)
    settings, show = _frieren_setup(session, [feed])
    backfill.run_backfill(engine, fetch=_fetch_by_site([]), pause=lambda s: None)

    qbit = MagicMock()
    qbit.ensure_category_exists.return_value = True
    qbit.get_torrents.return_value = []
    evaluate_and_grab_releases(
        session, qbit, settings, [feed], mode="direct", rss_snapshot=CachedRssSnapshot(qbit, session),
    )

    assert qbit.add_torrent.call_count == 1
    episode = session.exec(select(Episode).where(Episode.episode_number == 7)).first()
    assert episode.status != EpisodeStatus.MISSED
    assert episode.feed_id == feed.id
    session.close()
    engine.dispose()


def _nyaa(*items):
    """Nyaa RSS with (title, guid number, publication time or None) items."""
    rows = []
    for title, number, published in items:
        date = f"<pubDate>{format_datetime(published)}</pubDate>" if published else ""
        rows.append(
            f"<item><title>{title}</title><link>https://nyaa.si/download/{number}.torrent</link>"
            f'<guid isPermaLink="true">https://nyaa.si/view/{number}</guid>{date}</item>'
        )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>' + "".join(rows) + "</channel></rss>"
    )


def test_backfill_keeps_only_releases_dated_inside_this_shows_run():
    backfill._searched.clear()
    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])
    now = utc_now()
    body = _nyaa(
        ("[Erai-raws] Sousou no Frieren - 07 [1080p]", 1, now - timedelta(days=1)),
        # Same show, older season: numbered like this one but published long before.
        ("[Erai-raws] Sousou no Frieren - 06 [1080p]", 2, now - timedelta(days=900)),
        # Undated: a search cannot vouch for it.
        ("[Erai-raws] Sousou no Frieren - 05 [1080p]", 3, None),
        ("[Erai-raws] Sousou no Frieren - 08 [1080p]", 4, now + timedelta(days=30)),
    )

    found = backfill.run_backfill(engine, fetch=lambda url: body, pause=lambda s: None)

    assert found == {"Sousou no Frieren": 1}
    titles = [row.title for row in session.exec(select(MatchedFeedItem)).all()]
    assert titles == ["[Erai-raws] Sousou no Frieren - 07 [1080p]"]
    session.close()
    engine.dispose()


def test_backfill_attaches_results_only_to_the_show_that_was_searched():
    backfill._searched.clear()
    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])
    session.add(Monitored(
        id=2, anilist_id=2, display_name="Another Show", aliases_json='["Another Show"]',
        status=MonitoredStatus.UNCONFIRMED,
    ))
    session.commit()
    now = utc_now()
    body = _nyaa(
        ("[Erai-raws] Sousou no Frieren - 07 [1080p]", 1, now - timedelta(days=1)),
        ("[Erai-raws] Another Show - 03 [1080p]", 2, now - timedelta(days=1)),
    )

    backfill.run_backfill(engine, fetch=lambda url: body, pause=lambda s: None)

    rows = session.exec(select(MatchedFeedItem)).all()
    assert [(row.monitored_id, row.title) for row in rows] == [(1, "[Erai-raws] Sousou no Frieren - 07 [1080p]")]
    session.close()
    engine.dispose()


def test_subsplease_search_dates_are_never_in_the_future():
    now = utc_now()
    payload = {"x": {"release_date": format_datetime(now + timedelta(hours=7)), "downloads": [
        {"res": "1080", "magnet": "magnet:?xt=urn:btih:VYR2IC6LOQ2Z5IBYGK2KZY4C6F6JYS6W&dn=%5BSubsPlease%5D%20Show%20-%2001.mkv"},
    ]}}
    article = backfill.parse_subsplease_search(payload, "1080", now=now)[0]
    assert backfill.parse_article_date(article) <= now.replace(microsecond=0)


def test_season_two_releases_for_a_season_three_show_never_reach_the_cache_or_a_grab():
    """The real incident: old-season SubsPlease/Erai items matched the new season's show."""
    backfill._searched.clear()
    engine, session = _database()
    feed = Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)
    settings, show = _frieren_setup(session, [feed])
    now = utc_now()
    body = _nyaa(
        ("[Erai-raws] Sousou no Frieren S2 - 08v2 [1080p]", 1, now - timedelta(days=700)),
        ("[Erai-raws] Sousou no Frieren - 07 [1080p]", 2, now - timedelta(days=1)),
    )
    backfill.run_backfill(engine, fetch=lambda url: body, pause=lambda s: None)
    # Even if an old item were already cached, the grabber refuses it on its date.
    session.add(MatchedFeedItem(
        monitored_id=1, feed_url=NYAA_FEED, item_id="old", title="[Erai-raws] Sousou no Frieren - 06 [1080p]",
        published_at=(now - timedelta(days=700)).replace(tzinfo=None),
        data_json=json.dumps({"id": "old", "title": "[Erai-raws] Sousou no Frieren - 06 [1080p]",
                              "torrentURL": "magnet:old", "date": format_datetime(now - timedelta(days=700))}),
    ))
    session.commit()

    qbit = MagicMock()
    qbit.ensure_category_exists.return_value = True
    qbit.get_torrents.return_value = []
    evaluate_and_grab_releases(
        session, qbit, settings, [feed], mode="direct", rss_snapshot=CachedRssSnapshot(qbit, session),
    )

    urls = [call.kwargs.get("urls") for call in qbit.add_torrent.call_args_list]
    assert urls == ["https://nyaa.si/download/2.torrent"]
    session.close()
    engine.dispose()


def test_prune_removes_releases_published_before_the_shows_premiere():
    from kisetsu.core import feedcache

    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])
    first_air = session.exec(select(Episode.air_at).where(Episode.air_at.is_not(None)).order_by(Episode.air_at)).first()
    for item_id, published in (
        ("old", first_air - timedelta(days=400)),
        ("fresh", first_air + timedelta(days=1)),
        ("undated", None),
    ):
        session.add(MatchedFeedItem(
            monitored_id=1, feed_url=NYAA_FEED, item_id=item_id, title=item_id, published_at=published, data_json="{}",
        ))
    session.commit()

    feedcache.prune(session)

    assert sorted(row.item_id for row in session.exec(select(MatchedFeedItem)).all()) == ["fresh", "undated"]
    session.close()
    engine.dispose()


def _empty_nyaa(url):
    return _nyaa()


def test_nothing_is_searched_when_the_wanted_episode_is_already_in_the_cache():
    """Wanted episodes that the cached feed items cover are the next check's job, not a search's."""
    backfill._searched.clear()
    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])
    session.add(MatchedFeedItem(
        monitored_id=1, feed_url=NYAA_FEED, item_id="cached", title="[Erai-raws] Sousou no Frieren - 07 [1080p]",
        published_at=(utc_now() - timedelta(days=1)).replace(tzinfo=None),
        data_json=json.dumps({
            "id": "cached", "title": "[Erai-raws] Sousou no Frieren - 07 [1080p]", "torrentURL": "magnet:cached",
            "date": format_datetime(utc_now() - timedelta(days=1)),
        }),
    ))
    session.commit()
    requested = []

    def fetch(url):
        requested.append(url)
        return _nyaa()

    assert backfill.backfill_due(session) == []
    assert backfill.run_backfill(engine, fetch=fetch, pause=lambda s: None) == {}
    assert backfill.run_backfill(engine, show_id=1, force=True, fetch=fetch, pause=lambda s: None) == {}
    assert requested == []
    session.close()
    engine.dispose()


def test_an_episode_the_cache_cannot_supply_is_searched_for_and_named():
    backfill._searched.clear()
    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])
    # Only an earlier season's release is cached; it must not hide the missing episode.
    session.add(MatchedFeedItem(
        monitored_id=1, feed_url=NYAA_FEED, item_id="old", title="[Erai-raws] Sousou no Frieren - 07 [1080p]",
        published_at=(utc_now() - timedelta(days=700)).replace(tzinfo=None),
        data_json=json.dumps({
            "id": "old", "title": "[Erai-raws] Sousou no Frieren - 07 [1080p]", "torrentURL": "magnet:old",
            "date": format_datetime(utc_now() - timedelta(days=700)),
        }),
    ))
    session.commit()

    due = backfill.backfill_due(session)

    assert [(item.show.id, item.missing) for item in due] == [(1, [7])]
    requested = []

    def fetch(url):
        requested.append(url)
        return _nyaa()

    # Nothing found: asked once, then left alone until the set of missing episodes changes...
    backfill.run_backfill(engine, fetch=fetch, pause=lambda s: None)
    assert requested
    requested.clear()
    assert backfill.run_backfill(engine, fetch=fetch, pause=lambda s: None) == {}
    assert requested == []
    # ...but a manual search asks again.
    backfill.run_backfill(engine, show_id=1, force=True, fetch=fetch, pause=lambda s: None)
    assert requested
    session.close()
    engine.dispose()


def test_a_search_repeats_when_another_episode_goes_missing():
    backfill._searched.clear()
    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])
    requested = []

    def fetch(url):
        requested.append(url)
        return _nyaa()

    backfill.run_backfill(engine, fetch=fetch, pause=lambda s: None)
    requested.clear()
    sixth = session.exec(select(Episode).where(Episode.episode_number == 6)).first()
    sixth.status = EpisodeStatus.MISSED
    sixth.air_at = (utc_now() - timedelta(days=9)).replace(tzinfo=None)
    session.add(sixth)
    session.commit()

    assert [item.missing for item in backfill.backfill_due(session)] == [[6, 7]]
    session.close()
    engine.dispose()


def test_an_old_missed_episode_of_the_season_is_searched_for():
    """No day limit: an episode from weeks ago is as much the season's as last night's."""
    backfill._searched.clear()
    engine, session = _database()
    _frieren_setup(session, [Feed(id=1, qbit_feed_name="Erai", qbit_feed_url=NYAA_FEED, priority=1)])
    episode = session.exec(select(Episode).where(Episode.episode_number == 7)).first()
    episode.air_at = (utc_now() - timedelta(days=40)).replace(tzinfo=None)
    session.add(episode)
    session.commit()

    assert [item.missing for item in backfill.backfill_due(session)] == [[7]]
    session.close()
    engine.dispose()
