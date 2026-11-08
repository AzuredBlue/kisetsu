from datetime import timedelta

from sqlalchemy import event
from sqlmodel import select

from kisetsu.db.schema import Download, DownloadState, Episode, EpisodeStatus, FeedItem, FeedState
from kisetsu.web import views
from tests.fakes import START


def _long_show(world, episodes=60):
    show = world.show(episodes=episodes, first_air=START - timedelta(days=7 * (episodes - 1)))
    for episode in world.session.exec(select(Episode).where(Episode.show_id == show.id)).all():
        dl = Download(episode_id=episode.id, title=f"[Grp] Sousou no Frieren - {episode.number:02d}.mkv", state=DownloadState.DONE, info_hash=f"{episode.number:040x}")
        world.session.add(dl)
        world.session.flush()
        episode.current_download_id, episode.status = dl.id, EpisodeStatus.COMPLETED
    world.commit()
    return show


def test_the_show_page_reads_its_downloads_in_a_few_queries_however_many_episodes(world):
    show = _long_show(world)
    statements = []
    listener = lambda *args: statements.append(1)  # noqa: E731
    event.listen(world.engine, "before_cursor_execute", listener)
    try:
        page = views.show_view(world.ctx(), show)
    finally:
        event.remove(world.engine, "before_cursor_execute", listener)
    assert len(page["episodes"]) == 60 and page["episodes"][0]["release"]["state"] == "done"
    assert len(statements) < 30


def test_the_show_list_sends_only_the_last_years_air_dates(world):
    _long_show(world, episodes=80)
    [card] = views.shows_view(world.ctx())
    assert 50 <= len(card["airings"]) <= 54
    assert card["airings"][-1]["episode"] == 80


def test_a_show_learns_how_long_after_airing_its_feed_releases_it(world):
    feed = world.feed("Grp", "https://nyaa.si/?page=rss&u=Grp", 1)
    other = world.feed("Other", "https://nyaa.si/?page=rss&u=Other", 2)
    show = world.show(episodes=4, first_air=START - timedelta(days=28), feed=feed, feed_state=FeedState.LOCKED)

    def grab(number, minutes, source=feed, **fields):
        episode = world.episode(show, number)
        guid = f"{source.name}-{number}-{fields.get('version', 1)}"
        world.session.add(FeedItem(feed_id=source.id, guid=guid, title=guid, published_at=episode.air_at + timedelta(minutes=minutes)))
        world.session.add(Download(episode_id=episode.id, feed_id=source.id, feed_item_guid=guid, title=guid, state=DownloadState.DONE, **fields))
        world.commit()

    grab(1, 35, adopted=True)  # adopted, but it came from an item of the show's feed
    [card] = views.shows_view(world.ctx())
    assert card["release_delay_minutes"] is None
    grab(2, 300, version=2)  # a v2 days later says nothing about when the show comes out
    grab(2, 600, source=other)  # nor does a release from a feed the show no longer uses
    assert views.shows_view(world.ctx())[0]["release_delay_minutes"] is None
    grab(3, 37)
    assert views.shows_view(world.ctx())[0]["release_delay_minutes"] == 36
    assert views.card_for(world.ctx(), show)["release_delay_minutes"] == 36
