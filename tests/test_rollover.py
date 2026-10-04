from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.db.models import Episode, Monitored, MonitoredStatus, Settings, utc_now


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_continuous_numbering_never_falsely_completed(session):
    """Shows using continuous numbering (e.g. Hyakkano S3 Ep 10 released as Ep 34) must not be marked completed."""
    now = utc_now()
    show = Monitored(
        id=1,
        anilist_id=1001,
        display_name="Hyakkano Season 3",
        aliases_json='["Hyakkano"]',
        status=MonitoredStatus.FIXED,
        total_episodes=12,
        next_airing_episode=11,
        next_airing_at=now + timedelta(days=3),
        last_confirmed_episode=34,  # Continuous franchise number
        qbit_rule_name="[Seasonal] Hyakkano S3",
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 11
    assert show.qbit_rule_name == "[Seasonal] Hyakkano S3"
    mock_qbit.remove_rss_rule.assert_not_called()


def test_continuous_numbering_rolls_to_next_seasonal_episode(session):
    """When an episode airs, it rolls to current_ep + 1 (+7d), never corrupted by continuous episode count."""
    now = utc_now()
    show = Monitored(
        id=2,
        anilist_id=1002,
        display_name="Re:ZERO Season 4",
        aliases_json='["Re:ZERO"]',
        status=MonitoredStatus.FIXED,
        total_episodes=19,
        next_airing_episode=8,
        next_airing_at=now - timedelta(hours=3),
        last_confirmed_episode=82,  # Continuous franchise number
        qbit_rule_name="[Seasonal] Re:ZERO",
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    assert any("Rolled schedule to Ep 9" in log for log in logs)
    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 9  # Incremented from 8 -> 9, NOT 83!
    assert show.next_airing_at is not None
    air_at = show.next_airing_at if show.next_airing_at.tzinfo else show.next_airing_at.replace(tzinfo=timezone.utc)
    assert air_at > now


def test_finale_completes_when_all_episodes_confirmed(session):
    """When season finale has aired and all episodes confirmed, mark COMPLETED and disable the rule."""
    now = utc_now()
    show = Monitored(
        id=3,
        anilist_id=1003,
        display_name="THE GHOST IN THE SHELL",
        aliases_json='["GITS"]',
        status=MonitoredStatus.FIXED,
        total_episodes=10,
        next_airing_episode=10,
        next_airing_at=now - timedelta(hours=5),
        last_confirmed_episode=10,
        qbit_rule_name="[Seasonal] THE GHOST IN THE SHELL",
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_qbit.get_rss_rules.return_value = {
        "[Seasonal] THE GHOST IN THE SHELL": {"enabled": True}
    }
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    assert show.status == MonitoredStatus.COMPLETED
    assert show.next_airing_episode is None
    assert show.next_airing_at is None
    assert show.qbit_rule_name == "[Seasonal] THE GHOST IN THE SHELL"
    mock_qbit.set_rss_rule.assert_called_with(
        rule_name="[Seasonal] THE GHOST IN THE SHELL",
        rule_def={"enabled": False},
    )


def test_finale_waits_for_download_when_not_confirmed(session):
    """When season finale has aired but not yet confirmed downloaded, do not roll past total_episodes."""
    now = utc_now()
    show = Monitored(
        id=35,
        anilist_id=1035,
        display_name="Finale Waiting Anime",
        aliases_json='["Finale Waiting"]',
        status=MonitoredStatus.FIXED,
        total_episodes=12,
        next_airing_episode=12,
        next_airing_at=now - timedelta(hours=5),
        last_confirmed_episode=11,  # Ep 12 not downloaded yet
        qbit_rule_name="[Seasonal] Finale Waiting",
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 12  # Stays at 12, does not advance to phantom Ep 13!
    assert show.qbit_rule_name == "[Seasonal] Finale Waiting"
    mock_qbit.remove_rss_rule.assert_not_called()


def test_prune_past_season_shows(session):
    """Past-season completed shows are pruned on new season arrival, while continuing cours are preserved."""
    now = utc_now()

    show_summer_done = Monitored(
        id=10,
        anilist_id=1010,
        display_name="Summer Completed Show",
        aliases_json='["Summer Completed"]',
        status=MonitoredStatus.COMPLETED,
        season_name="SUMMER",
        season_year=2026,
        total_episodes=12,
        last_confirmed_episode=12,
        qbit_rule_name="[Seasonal] Summer Completed",
    )

    show_extending = Monitored(
        id=20,
        anilist_id=1020,
        display_name="Extending Cour Show",
        aliases_json='["Extending Cour"]',
        status=MonitoredStatus.FIXED,
        season_name="SUMMER",
        season_year=2026,
        total_episodes=24,
        next_airing_episode=12,
        next_airing_at=now + timedelta(days=2),
        last_confirmed_episode=11,
        qbit_rule_name="[Seasonal] Extending Cour",
    )

    show_fall_active = Monitored(
        id=30,
        anilist_id=1030,
        display_name="Fall 2026 New Show",
        aliases_json='["Fall New"]',
        status=MonitoredStatus.FIXED,
        season_name="FALL",
        season_year=2026,
        total_episodes=12,
        next_airing_episode=1,
        next_airing_at=now + timedelta(days=5),
        qbit_rule_name="[Seasonal] Fall New",
    )

    session.add(show_summer_done)
    session.add(show_extending)
    session.add(show_fall_active)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    from unittest.mock import patch
    with patch("qbit_seasonal_anime.core.supervisor.get_current_and_next_season", return_value=(("FALL", 2026), ("WINTER", 2027))):
        logs = supervisor.prune_past_season_shows()

    all_ids = {s.id for s in session.exec(select(Monitored)).all()}
    assert 10 not in all_ids
    assert 20 in all_ids
    assert 30 in all_ids
    assert any("Pruned completed show 'Summer Completed Show'" in log for log in logs)


def test_stale_fixed_show_overdue_advance(session):
    now = utc_now()
    show = Monitored(
        id=4,
        anilist_id=1004,
        display_name="Stale Show",
        aliases_json='["Stale Show"]',
        status=MonitoredStatus.FIXED,
        total_episodes=12,
        next_airing_episode=3,
        next_airing_at=now - timedelta(hours=30),  # > 24 hours ago
        last_confirmed_episode=2,
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_anilist = MagicMock()
    settings = Settings(id=1, base_dir="/tmp")
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    assert any("Rolled schedule to Ep 4" in log for log in logs)
    assert show.next_airing_episode == 4
    assert show.next_airing_at is not None
    air_at = show.next_airing_at if show.next_airing_at.tzinfo else show.next_airing_at.replace(tzinfo=timezone.utc)
    assert air_at > now


def test_direct_mode_never_rolls_the_schedule_forward(session):
    """An overdue episode stays put against its real air time.

    Guessing a weekly rollover renumbers a whole season against feeds that
    carry absolute numbering, which is what the direct engine would then
    download against.
    """
    now = utc_now()
    show = Monitored(
        id=2,
        anilist_id=1002,
        display_name="Re:ZERO Season 4",
        aliases_json='["Re:ZERO"]',
        status=MonitoredStatus.FIXED,
        total_episodes=19,
        next_airing_episode=8,
        next_airing_at=now - timedelta(hours=3),
        # Continuous franchise number, which is not the canonical episode.
        last_confirmed_episode=82,
        qbit_rule_name="[Seasonal] Re:ZERO",
    )
    session.add(show)
    session.commit()

    settings = Settings(id=1, base_dir="/tmp", download_mode="direct")
    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=MagicMock(), settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)
    ).first()
    assert any("Episode 8 remains overdue" in log for log in logs)
    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 8
    assert episode.schedule_state == "aired"
    air_at = show.next_airing_at if show.next_airing_at.tzinfo else show.next_airing_at.replace(tzinfo=timezone.utc)
    assert air_at <= now


def test_direct_mode_keeps_an_overdue_episode_retryable_in_direct_mode(session):
    """A show overdue by more than a day is still just overdue, not advanced."""
    now = utc_now()
    show = Monitored(
        id=4,
        anilist_id=1004,
        display_name="Stale Show",
        aliases_json='["Stale Show"]',
        status=MonitoredStatus.FIXED,
        total_episodes=12,
        next_airing_episode=3,
        next_airing_at=now - timedelta(hours=30),
        last_confirmed_episode=2,
    )
    session.add(show)
    session.commit()

    settings = Settings(id=1, base_dir="/tmp", download_mode="direct")
    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=MagicMock(), settings=settings)

    logs = supervisor.reconcile_schedule_rollover()
    session.refresh(show)

    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 3)
    ).first()
    assert any("Episode 3 remains overdue" in log for log in logs)
    assert show.next_airing_episode == 3
    assert episode.schedule_state == "aired"
    air_at = show.next_airing_at if show.next_airing_at.tzinfo else show.next_airing_at.replace(tzinfo=timezone.utc)
    assert air_at <= now
