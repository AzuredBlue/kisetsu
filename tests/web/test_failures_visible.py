from datetime import timedelta

import pytest

from kisetsu.db.schema import Download, DownloadState, EndReason, Episode, EpisodeStatus, Event, EventKind, Feed
from tests.fakes import START

GAVE_UP = "Gave up after 8 attempts: qBittorrent never showed the torrent."
GAVE_UP_SLOW = "Gave up after 48 hours: the replacement did not finish and was removed; the previous release was kept."


async def test_failures_and_misses_are_history_rows_with_their_reason(web):
    show = web.show()

    def add(session):
        session.add(Event(at=START, kind=EventKind.GRABBED, show_id=show, episode=1, title="Varyg - 01", message="Added"))
        session.add(Event(at=START + timedelta(minutes=1), kind=EventKind.FAILED, show_id=show, episode=1, title="Varyg - 01", message=GAVE_UP))
        session.add(Event(at=START + timedelta(minutes=2), kind=EventKind.MISSED, show_id=show, episode=2, title="", message="Not in the feed"))
        session.add(Event(at=START + timedelta(minutes=3), kind=EventKind.INFO, show_id=show, episode=2, title="", message="Gave up"))
        session.commit()

    web.query(add)
    rows = (await web.client.get("/api/history")).json()
    assert [(r["kind"], r["episode"], r["title"], r["message"]) for r in rows] == [
        ("missed", 2, "", "Not in the feed"),
        ("failed", 1, "Varyg - 01", GAVE_UP),
    ]


async def test_clearing_the_history_clears_failures_but_not_other_events(web):
    show = web.show()

    def add(session):
        session.add(Event(at=START, kind=EventKind.FAILED, show_id=show, episode=1, title="x", message="boom"))
        session.add(Event(at=START, kind=EventKind.INFO, show_id=show, episode=1, title="x", message="note"))
        session.commit()

    web.query(add)
    await web.client.delete("/api/history")
    assert (await web.client.get("/api/history")).json() == []
    assert web.query(lambda s: [e.kind for e in s.query(Event).all()]) == [EventKind.INFO]


async def test_status_names_the_feeds_that_are_failing(web):
    web.feed("Good", "https://good/rss")
    bad = web.feed("Bad", "https://bad/rss")

    def fail(session):
        session.get(Feed, bad).failing_since = START
        session.commit()

    web.query(fail)
    assert (await web.client.get("/api/status")).json()["failing_feeds"] == ["Bad"]


def _download(web, show, *, state, error=None, age=timedelta(hours=2), retry_in=None, episode_status=None, end_reason=None):
    """A download of episode 1 of ``show``, created ``age`` ago."""

    def add(session):
        episode = session.query(Episode).filter(Episode.show_id == show, Episode.number == 1).one()
        if retry_in is not None:
            episode.retry_after = web.clock() + retry_in
        if episode_status is not None:
            episode.status = episode_status
        session.add(Download(
            episode_id=episode.id, title="Varyg - 01", state=state, last_error=error, end_reason=end_reason,
            created_at=web.clock() - age, updated_at=web.clock() - age,
        ))
        session.commit()

    web.query(add)


async def _stuck(web):
    return (await web.client.get("/api/status")).json()["stuck_adds"]


@pytest.mark.parametrize("state", [DownloadState.ADDING, DownloadState.RETRY_WAIT])
async def test_an_add_qbittorrent_never_showed_for_an_hour_is_stuck(web, state):
    show = web.show()
    _download(web, show, state=state)
    assert await _stuck(web) == 1


@pytest.mark.parametrize("state", [DownloadState.ADDING, DownloadState.RETRY_WAIT])
async def test_a_young_add_is_not_stuck(web, state):
    show = web.show()
    _download(web, show, state=state, age=timedelta(minutes=30))
    assert await _stuck(web) == 0


async def test_a_downloading_torrent_is_not_a_stuck_add(web):
    show = web.show()
    _download(web, show, state=DownloadState.DOWNLOADING)
    assert await _stuck(web) == 0


async def test_paused_and_completed_shows_are_never_stuck(web):
    paused = web.show(1, "Paused", paused=True)
    done = web.show(2, "Done", completed=True)
    _download(web, paused, state=DownloadState.ADDING)
    _download(web, done, state=DownloadState.RETRY_WAIT)
    assert await _stuck(web) == 0


async def test_a_give_up_stays_stuck_through_its_wait_before_the_next_try(web):
    show = web.show()
    _download(web, show, state=DownloadState.CANCELED, error=GAVE_UP, end_reason=EndReason.ADD_GAVE_UP, age=timedelta(hours=1), retry_in=timedelta(hours=2))
    assert await _stuck(web) == 1


async def test_a_give_up_whose_wait_is_over_is_not_counted(web):
    show = web.show()
    _download(web, show, state=DownloadState.CANCELED, error=GAVE_UP, end_reason=EndReason.ADD_GAVE_UP, retry_in=-timedelta(minutes=1))
    assert await _stuck(web) == 0


async def test_a_replacement_that_gave_up_after_48_hours_is_a_slow_torrent_not_a_lost_add(web):
    show = web.show()
    _download(web, show, state=DownloadState.CANCELED, error=GAVE_UP_SLOW, end_reason=EndReason.REPLACEMENT_GAVE_UP, retry_in=timedelta(hours=2))
    assert await _stuck(web) == 0


async def test_a_give_up_whose_episode_has_since_completed_is_not_counted(web):
    show = web.show()
    _download(
        web, show, state=DownloadState.CANCELED, error=GAVE_UP, end_reason=EndReason.ADD_GAVE_UP,
        retry_in=timedelta(hours=2), episode_status=EpisodeStatus.COMPLETED,
    )
    assert await _stuck(web) == 0


async def test_error_text_alone_does_not_make_a_give_up(web):
    show = web.show()
    _download(web, show, state=DownloadState.CANCELED, error=GAVE_UP, retry_in=timedelta(hours=2))
    assert await _stuck(web) == 0


async def test_the_same_episode_is_counted_once_and_not_while_qbittorrent_is_unreachable(web):
    show = web.show()
    _download(web, show, state=DownloadState.CANCELED, error=GAVE_UP, end_reason=EndReason.ADD_GAVE_UP, retry_in=timedelta(hours=2))
    _download(web, show, state=DownloadState.ADDING)
    assert await _stuck(web) == 1
    web.runtime.jobs.health["qbit"] = "refused"
    assert await _stuck(web) == 0


async def test_a_stalled_download_reaches_the_show_page_and_try_another_swaps_it(web):
    from kisetsu.services import ingest
    from tests.fakes import article

    feed = web.feed()
    show = web.show()
    engine = web.runtime.engine

    def post(ctx, *titles):
        ingest.ingest(ctx, web.query(lambda s: s.get(Feed, feed)), [article(t, web.clock()) for t in titles])

    first, other = "[Grp] Sousou no Frieren - 01 (1080p).mkv", "[Other] Sousou no Frieren - 01 (1080p).mkv"
    await engine.run(post, first, other)
    assert (await web.client.post(f"/api/shows/{show}/download", json={"title": first})).status_code == 200
    episode = web.episode(show, 1)
    dl = web.query(lambda s: s.query(Download).one())
    page = (await web.client.get(f"/api/shows/{show}")).json()
    assert page["episodes"][0]["stalled"] is None
    # What the settle pass records; the read must be able to see it although a read has no caches of its own.
    engine.caches.stalled = {dl.id}
    page = (await web.client.get(f"/api/shows/{show}")).json()
    assert page["episodes"][0]["stalled"] == {"alternatives": 1}
    answer = await web.client.post(f"/api/shows/{show}/episodes/{episode.id}/try-another")
    assert answer.status_code == 200 and other in answer.json()["message"]
    page = (await web.client.get(f"/api/shows/{show}")).json()
    assert page["episodes"][0]["stalled"] is None and page["episodes"][0]["release"]["title"] == other
    assert (await web.client.post(f"/api/shows/{show}/episodes/{episode.id}/try-another")).status_code == 400


async def test_the_status_tells_the_first_run_checklist_and_failing_background_work(web):
    body = (await web.client.get("/api/status")).json()
    assert body["feeds"] == 0 and body["anilist_synced_at"] is None and body["any_download"] is False
    assert body["grab_error"] is None and body["jobs_failing"] == {}
    web.runtime.jobs.health["grab"] = "Sousou no Frieren: boom"
    job = web.runtime.scheduler.add("backfill", None, interval=None, first=None)
    job.last_error = "site down"
    body = (await web.client.get("/api/status")).json()
    assert body["grab_error"] == "Sousou no Frieren: boom" and body["jobs_failing"] == {"backfill": "site down"}


async def test_the_attention_list_gathers_failed_missed_and_retrying_episodes_across_shows(web):
    from datetime import timedelta
    from kisetsu.db.schema import Download, DownloadState, Episode, EpisodeStatus
    from sqlmodel import select
    from tests.fakes import START

    first = web.show(1, "Sousou no Frieren", episodes=3)
    second = web.show(2, "Dandadan", episodes=3)

    def mark(session):
        eps = {(e.show_id, e.number): e for e in session.exec(select(Episode)).all()}
        failed = eps[(first, 1)]
        dl = Download(episode_id=failed.id, title="[Grp] Sousou no Frieren - 01.mkv", state=DownloadState.FAILED, last_error="qBittorrent rejected it")
        session.add(dl)
        session.flush()
        failed.current_download_id, failed.status, failed.last_error = dl.id, EpisodeStatus.FAILED, "qBittorrent rejected it"
        eps[(second, 2)].status = EpisodeStatus.MISSED
        retrying = eps[(second, 3)]
        retrying.last_error, retrying.retry_after = "Gave up after 8 attempts", START + timedelta(hours=2)
        session.commit()

    web.query(mark)
    rows = (await web.client.get("/api/attention")).json()
    assert [(r["show"], r["episode"], r["kind"]) for r in rows] == [
        ("Sousou no Frieren", 1, "failed"), ("Dandadan", 3, "retrying"), ("Dandadan", 2, "missed"),
    ]
    assert rows[0]["reason"] == "qBittorrent rejected it" and rows[0]["show_id"] == first
