"""Tests for the code-health work: client reuse, WAL, title language in rules, palette and the feed cache."""

import importlib
import io
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from PIL import Image
from sqlmodel import Session, SQLModel, create_engine

from kisetsu.core import palette
from kisetsu.core.stall import check_and_handle_stalls
from kisetsu.db.models import Feed, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings, utc_now
from kisetsu.db.session import get_engine

app_module = importlib.import_module("kisetsu.server.app")


def _png(color) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_settle_client_is_reused_until_the_connection_settings_change():
    app_module._drop_settle_client()
    settings = Settings(qbit_host="http://qbit:8080", qbit_username="u", qbit_password="p")
    first = app_module._settle_client(settings)
    assert app_module._settle_client(settings) is first

    settings.qbit_password = "other"
    second = app_module._settle_client(settings)
    assert second is not first

    app_module._drop_settle_client()
    assert app_module._settle_client(settings) is not second
    app_module._drop_settle_client()


def test_a_stall_fallback_writes_the_rule_with_the_chosen_title_language():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, stall_wait_hours=24, base_dir="/tmp/Anime/{name}", title_language="romaji")
        session.add(settings)
        session.add(Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://a.example/rss", priority=1))
        session.add(Feed(id=2, qbit_feed_name="Erai-raws", qbit_feed_url="https://b.example/rss", priority=2))
        show = Monitored(
            id=2,
            anilist_id=1002,
            display_name="English Title",
            title_english="English Title",
            title_romaji="Romaji Title",
            aliases_json='["Stalled Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            qbit_rule_name="[Seasonal] Stalled Anime",
            last_confirmed_episode=1,
            next_airing_episode=2,
            next_airing_at=utc_now() - timedelta(hours=48),
        )
        session.add(show)
        session.add(RuleHistory(
            id=1, monitored_id=2, feed_id=1, created_at=utc_now() - timedelta(hours=48), outcome=RuleOutcome.PENDING,
        ))
        session.commit()

        qbit = MagicMock()
        qbit.get_rss_items.return_value = {
            "Erai-raws": {
                "url": "https://b.example/rss",
                "articles": [{"title": "[Erai-raws] Stalled Anime - 02 [1080p].mkv", "torrentURL": "https://e/2.torrent"}],
            }
        }
        check_and_handle_stalls(session, qbit, settings)

        qbit.set_rss_rule.assert_called_once()
        assert "Romaji Title" in qbit.set_rss_rule.call_args.kwargs["rule_def"]["savePath"]


def test_the_database_runs_in_wal_mode(tmp_path):
    engine = get_engine(tmp_path / "wal.db")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_dominant_hues_ignores_a_grey_image_and_finds_the_hue_of_a_red_one():
    assert palette.dominant_hues(_png((128, 128, 128))) is None
    accent_hue, _sat, _secondary, tint_hue, _tint_sat = palette.dominant_hues(_png((220, 20, 20)))
    assert accent_hue in (*range(0, 15), *range(345, 360))
    assert tint_hue in (*range(0, 15), *range(345, 360))


@pytest.mark.parametrize("url,allowed", [
    ("https://s4.anilist.co/file/banner.jpg", True),
    ("https://anilist.co/x.png", True),
    ("http://s4.anilist.co/x.png", False),
    ("https://anilist.co.evil.example/x.png", False),
    ("https://evil.example/anilist.co", False),
])
def test_only_https_anilist_images_are_fetched(url, allowed):
    assert palette.is_allowed_image_url(url) is allowed


@pytest.fixture
def cache_session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://a.example/rss", priority=1))
        session.add(Monitored(id=1, anilist_id=1, display_name="Frieren", aliases_json='["Frieren"]'))
        session.commit()
        yield session


def _article(title, item_id, date="Fri, 03 Oct 2026 10:00:00 +0000"):
    return {"id": item_id, "title": title, "torrentURL": f"https://a.example/{item_id}.torrent", "date": date}


def test_the_cache_keeps_only_releases_of_followed_shows_and_ignores_repeats(cache_session):
    from kisetsu.core import feedcache

    articles = {"https://a.example/rss": [
        _article("[SubsPlease] Frieren - 05 (1080p).mkv", "a"),
        _article("[SubsPlease] Something Else - 01 (1080p).mkv", "b"),
    ]}
    assert feedcache.store_matches(cache_session, articles) == 1
    assert feedcache.store_matches(cache_session, articles) == 0

    cached = feedcache.cached_articles(cache_session)["https://a.example/rss"]
    assert [item["id"] for item in cached] == ["a"]


def test_cached_articles_lists_newest_first_and_leaves_out_failing_feeds(cache_session, monkeypatch):
    from kisetsu.core import feedcache

    feedcache.store_matches(cache_session, {"https://a.example/rss": [
        _article("[SubsPlease] Frieren - 04 (1080p).mkv", "old", "Fri, 26 Sep 2026 10:00:00 +0000"),
        _article("[SubsPlease] Frieren - 05 (1080p).mkv", "new", "Fri, 03 Oct 2026 10:00:00 +0000"),
    ]})
    assert [a["id"] for a in feedcache.cached_articles(cache_session)["https://a.example/rss"]] == ["new", "old"]

    monkeypatch.setattr(feedcache, "_failed_feed_names", ["SubsPlease"])
    assert "https://a.example/rss" not in feedcache.cached_articles(cache_session)
    assert feedcache.last_failed_feed_names() == ["SubsPlease"]


def test_prune_drops_the_cache_of_a_feed_that_was_removed(cache_session):
    from kisetsu.core import feedcache

    feedcache.store_matches(cache_session, {"https://a.example/rss": [
        _article("[SubsPlease] Frieren - 05 (1080p).mkv", "a"),
    ]})
    # With no feeds at all nothing is pruned (a bad read must not empty the cache), so keep one.
    cache_session.add(Feed(id=2, qbit_feed_name="Other", qbit_feed_url="https://b.example/rss", priority=2))
    cache_session.delete(cache_session.get(Feed, 1))
    cache_session.commit()

    assert feedcache.prune(cache_session) == 1
    assert feedcache.cached_articles(cache_session) == {"https://b.example/rss": []}
