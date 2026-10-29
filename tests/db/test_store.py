import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from kisetsu.db.migrations import LATEST_VERSION, SchemaError, migrate
from kisetsu.db.schema import (
    Download,
    Episode,
    EpisodeStatus,
    Feed,
    FeedItem,
    FeedItemMatch,
    Settings,
    Show,
)
from kisetsu.db.store import make_engine, session_scope


@pytest.fixture
def engine():
    engine = make_engine()
    migrate(engine)
    return engine


def test_a_fresh_database_is_stamped_and_has_one_settings_row(engine):
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar() == LATEST_VERSION
    with Session(engine) as session:
        assert len(session.exec(select(Settings)).all()) == 1


def test_migrating_twice_changes_nothing(engine):
    migrate(engine)
    with Session(engine) as session:
        assert len(session.exec(select(Settings)).all()) == 1


def test_datetimes_come_back_aware_in_utc(engine):
    naive = datetime(2026, 10, 1, 12, 0)
    other_zone = datetime(2026, 10, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    with session_scope(engine) as session:
        session.add(Show(anilist_id=1, next_airing_at=naive))
        session.add(Show(anilist_id=2, next_airing_at=other_zone))
    with Session(engine) as session:
        one = session.exec(select(Show).where(Show.anilist_id == 1)).one()
        two = session.exec(select(Show).where(Show.anilist_id == 2)).one()
    assert one.next_airing_at == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert two.next_airing_at == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert one.next_airing_at - two.next_airing_at == timedelta(0)


def test_enums_are_stored_by_name(engine):
    with session_scope(engine) as session:
        show = Show(anilist_id=1)
        session.add(show)
        session.flush()
        session.add(Episode(show_id=show.id, number=1, status=EpisodeStatus.MISSED))
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT status FROM episodes").scalar() == "MISSED"
        assert conn.exec_driver_sql("SELECT feed_state FROM shows").scalar() == "NONE"


def test_foreign_keys_are_enforced_and_cascade(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            session.add(Episode(show_id=999, number=1))
    with session_scope(engine) as session:
        show = Show(anilist_id=1)
        session.add(show)
        session.flush()
        episode = Episode(show_id=show.id, number=1)
        session.add(episode)
        session.flush()
        session.add(Download(episode_id=episode.id, title="x"))
        session.commit()
        session.delete(show)
    with Session(engine) as session:
        assert session.exec(select(Episode)).all() == []
        assert session.exec(select(Download)).all() == []


def test_one_episode_number_per_show(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as session:
            show = Show(anilist_id=1)
            session.add(show)
            session.flush()
            session.add(Episode(show_id=show.id, number=1))
            session.add(Episode(show_id=show.id, number=1))


def test_deleting_a_feed_keeps_the_show_and_drops_its_items(engine):
    with session_scope(engine) as session:
        feed = Feed(name="A", url="https://a")
        session.add(feed)
        session.flush()
        show = Show(anilist_id=1, feed_id=feed.id)
        session.add(show)
        session.add(FeedItem(feed_id=feed.id, guid="g", title="t"))
    with session_scope(engine) as session:
        session.delete(session.exec(select(Feed)).one())
    with Session(engine) as session:
        assert session.exec(select(Show)).one().feed_id is None
        assert session.exec(select(FeedItem)).all() == []


def test_a_database_from_a_newer_release_is_refused():
    engine = make_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    with pytest.raises(SchemaError):
        migrate(engine)


def test_an_unversioned_database_with_tables_is_refused():
    engine = make_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE monitored (id INTEGER PRIMARY KEY)")
    with pytest.raises(SchemaError):
        migrate(engine)


def test_a_version_2_database_gains_the_show_tag_column():
    engine = make_engine()
    migrate(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE shows DROP COLUMN qbit_tag")
        conn.exec_driver_sql("PRAGMA user_version = 2")
    migrate(engine)
    with engine.connect() as conn:
        assert "qbit_tag" in {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(shows)")}
        assert conn.exec_driver_sql("PRAGMA user_version").scalar() == LATEST_VERSION


def test_a_version_3_database_gains_the_download_batch_column():
    engine = make_engine()
    migrate(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE downloads DROP COLUMN batch")
        conn.exec_driver_sql("PRAGMA user_version = 3")
    migrate(engine)
    with engine.connect() as conn:
        assert "batch" in {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(downloads)")}
        assert conn.exec_driver_sql("PRAGMA user_version").scalar() == LATEST_VERSION == 4


def test_the_schema_has_the_new_tables_only():
    from kisetsu.db.schema import Base

    assert "shows" in Base.metadata.tables
    assert "monitored" not in Base.metadata.tables


def test_the_engine_pragmas_apply_to_file_databases(tmp_path):
    path = tmp_path / "k.db"
    engine = make_engine(path)
    migrate(engine, path)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION


def test_one_feed_item_can_match_several_shows_and_matches_go_with_either_side(engine):
    with session_scope(engine) as session:
        feed = Feed(name="A", url="https://a")
        first, second = Show(anilist_id=1), Show(anilist_id=2)
        session.add_all([feed, first, second])
        session.flush()
        item = FeedItem(feed_id=feed.id, guid="g", title="Medalist Movie")
        session.add(item)
        session.flush()
        session.add_all([FeedItemMatch(item_id=item.id, show_id=first.id), FeedItemMatch(item_id=item.id, show_id=second.id)])
    with Session(engine) as session:
        assert len(session.exec(select(FeedItemMatch)).all()) == 2
    with session_scope(engine) as session:
        session.delete(session.exec(select(Show).where(Show.anilist_id == 1)).one())
    with Session(engine) as session:
        assert [m.show_id for m in session.exec(select(FeedItemMatch)).all()] == [2]
        assert len(session.exec(select(FeedItem)).all()) == 1
