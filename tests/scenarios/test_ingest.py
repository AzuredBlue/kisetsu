from datetime import timedelta

from sqlmodel import select

from kisetsu.db.schema import FeedItem, FeedItemMatch
from kisetsu.services import ingest
from tests.fakes import START, article

SUBS = "https://subsplease.org/rss/?t&r=1080"


def run_ingest(world, feed, titles):
    items = [article(t, world.clock()) for t in titles]
    result = ingest.ingest(world.ctx(), feed, items)
    world.commit()
    return result


def matches(world):
    world.session.expire_all()
    return sorted((m.show_id, world.session.get(FeedItem, m.item_id).title) for m in world.session.exec(select(FeedItemMatch)).all())


def test_a_new_article_is_stored_with_its_parsed_fields_and_matched_to_its_show(world):
    feed = world.feed("SubsPlease", SUBS, 1)
    show = world.show(1, "Sousou no Frieren", episodes=3, first_air=START - timedelta(days=14))
    result = run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 08v2 (1080p) [ABC].mkv", "[SubsPlease] Another Show - 01 (1080p).mkv"])
    assert (result.new, result.matched, result.show_ids) == (2, 1, {show.id})
    world.session.expire_all()
    item = world.session.exec(select(FeedItem).where(FeedItem.title.like("%Frieren%"))).one()
    assert (item.episode_raw, item.version, item.release_group, item.parsed_title) == (8, 2, "SubsPlease", "Sousou no Frieren")
    assert item.info_hash and item.first_seen_at == START
    assert matches(world) == [(show.id, item.title)]


def test_an_article_already_seen_is_not_stored_or_matched_again(world):
    feed = world.feed("SubsPlease", SUBS, 1)
    world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    title = "[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"
    first = article(title, START)
    assert ingest.ingest(world.ctx(), feed, [first]).new == 1
    world.commit()
    assert ingest.ingest(world.ctx(), feed, [first, first]).new == 0
    assert len(world.session.exec(select(FeedItem)).all()) == 1


def test_the_same_article_on_two_feeds_is_two_items(world):
    a = world.feed("A", "https://a/rss", 1)
    b = world.feed("B", "https://b/rss", 2)
    world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    item = article("[SubsPlease] Sousou no Frieren - 01 (1080p).mkv", START)
    ingest.ingest(world.ctx(), a, [item])
    ingest.ingest(world.ctx(), b, [item])
    world.commit()
    assert len(world.session.exec(select(FeedItem)).all()) == 2


def test_a_known_set_rebuilt_from_the_database_skips_stored_articles_after_a_restart(world):
    feed = world.feed("SubsPlease", SUBS, 1)
    world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    item = article("[SubsPlease] Sousou no Frieren - 01 (1080p).mkv", START)
    ingest.ingest(world.ctx(), feed, [item])
    world.commit()
    world.caches.known_items = None
    assert ingest.ingest(world.ctx(), feed, [item]).new == 0


def test_a_release_that_fits_two_followed_shows_matches_both(world):
    feed = world.feed("A", "https://a/rss", 1)
    first = world.show(1, "Medalist Movie", episodes=1, first_air=START, anilist_status="FINISHED")
    second = world.show(2, "Medalist", aliases=["Medalist Movie"], episodes=1, first_air=START)
    result = run_ingest(world, feed, ["[Group] Medalist Movie [1080p].mkv"])
    assert result.show_ids == {first.id, second.id}


def test_a_show_that_has_not_started_airing_is_not_matched_to_an_earlier_seasons_release(world):
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", anilist_status="NOT_YET_RELEASED", next_airing_episode=1, next_airing_at=START + timedelta(days=60))
    result = run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"])
    assert result.new == 1 and result.matched == 0


def test_a_show_about_to_premiere_is_matched(world):
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", anilist_status="NOT_YET_RELEASED", next_airing_episode=1, next_airing_at=START + timedelta(days=3))
    assert run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"]).matched == 1


def test_a_completed_show_is_not_matched(world):
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", completed=True)
    assert run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"]).matched == 0


def test_a_batch_release_is_stored_but_matches_nothing(world):
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    result = run_ingest(world, feed, ["[Group] Sousou no Frieren (01-28) [Batch]"])
    assert result.new == 1 and result.matched == 0


# Changing what is followed

def test_a_new_alias_finds_the_releases_already_stored(world):
    feed = world.feed("A", "https://a/rss", 1)
    show = world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    run_ingest(world, feed, ["[Erai-raws] Frieren - Beyond Journey's End - 05 [1080p].mkv"])
    assert matches(world) == []
    show.custom_aliases = ["Frieren - Beyond Journey's End"]
    world.session.add(show)
    world.commit()
    assert ingest.rematch(world.ctx(), [show.id]) == 1
    world.commit()
    assert len(matches(world)) == 1


def test_a_show_that_completes_keeps_its_matches_through_a_rematch(world):
    feed = world.feed("A", "https://a/rss", 1)
    show = world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"])
    show.completed = True
    world.session.add(show)
    world.commit()
    ingest.rematch(world.ctx())
    world.commit()
    assert len(matches(world)) == 1
    show.completed = False
    world.session.add(show)
    world.commit()
    ingest.rematch(world.ctx())
    world.commit()
    assert len(matches(world)) == 1


def test_matches_are_redone_when_the_followed_shows_changed_since_they_were_made(world):
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv", "[SubsPlease] Dandadan - 05 (1080p).mkv"])
    assert ingest.ensure_matches_current(world.ctx())
    world.commit()
    assert not ingest.ensure_matches_current(world.ctx())
    world.show(2, "Dandadan", episodes=1, first_air=START)
    assert ingest.ensure_matches_current(world.ctx())
    world.commit()
    assert len(matches(world)) == 2


# Pruning

def test_unmatched_items_go_after_thirty_days_and_can_come_back_if_the_feed_still_lists_them(world):
    feed = world.feed("A", "https://a/rss", 1)
    old = article("[Group] Unrelated Show - 01 [1080p].mkv", START)
    ingest.ingest(world.ctx(), feed, [old])
    world.commit()
    world.clock.advance(days=31)
    assert ingest.prune(world.ctx()) == 1
    world.commit()
    assert world.session.exec(select(FeedItem)).all() == []
    assert ingest.ingest(world.ctx(), feed, [old]).new == 1


def test_matched_items_are_kept_while_their_show_is_followed(world):
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"])
    world.clock.advance(days=90)
    ingest.prune(world.ctx())
    world.commit()
    assert len(world.session.exec(select(FeedItem)).all()) == 1


def test_matches_of_a_completed_show_go_after_sixty_days(world):
    feed = world.feed("A", "https://a/rss", 1)
    show = world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"])
    show.completed = True
    world.session.add(show)
    world.commit()
    world.clock.advance(days=61)
    ingest.prune(world.ctx())
    world.commit()
    assert matches(world) == []


def test_a_release_published_long_before_the_shows_first_episode_loses_its_match(world):
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", episodes=2, first_air=START)
    old = article("[SubsPlease] Sousou no Frieren - 01 (1080p).mkv", START - timedelta(days=200))
    ingest.ingest(world.ctx(), feed, [old])
    world.commit()
    assert len(matches(world)) == 1
    ingest.prune(world.ctx())
    world.commit()
    assert matches(world) == []


def test_a_shows_matches_are_capped_to_its_newest(world):
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", episodes=1, first_air=START - timedelta(days=400))
    items = [article(f"[SubsPlease] Sousou no Frieren - 01 (1080p) [{n}].mkv", START - timedelta(hours=n)) for n in range(ingest.PER_SHOW_CAP + 20)]
    ingest.ingest(world.ctx(), feed, items)
    world.commit()
    ingest.prune(world.ctx())
    world.commit()
    kept = matches(world)
    assert len(kept) == ingest.PER_SHOW_CAP
    assert all("[%d]" % n not in title for _, title in kept for n in (ingest.PER_SHOW_CAP + 19,))


def test_only_new_articles_announce_themselves_to_the_feed_items_list(world):
    feed = world.feed("Nyaa", "https://nyaa/rss", 1)
    world.show(episodes=1, first_air=START)
    first, second = world.ctx(), world.ctx()
    ingest.ingest(first, feed, [article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START)])
    world.commit()
    ingest.ingest(second, feed, [article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START)])
    assert "feed_items" in first.topics and "feed_items" not in second.topics


def test_the_match_signature_is_the_same_in_another_process():
    import subprocess
    import sys
    from kisetsu.domain.matcher import signature
    code = "from kisetsu.domain.matcher import signature; print(signature([(1, ('Sousou no Frieren', 'Frieren'), 'Frieren')]))"
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert other == str(signature([(1, ("Sousou no Frieren", "Frieren"), "Frieren")]))


def test_a_restart_does_not_redo_the_matches(world):
    from kisetsu.services.context import Caches
    feed = world.feed("A", "https://a/rss", 1)
    world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"])
    ingest.ensure_matches_current(world.ctx())
    world.commit()
    world.caches = Caches()  # what a new process starts with
    assert not ingest.ensure_matches_current(world.ctx())


def test_a_changed_show_is_rematched_without_reading_every_other_shows_matches(world, monkeypatch):
    feed = world.feed("A", "https://a/rss", 1)
    frieren = world.show(1, "Sousou no Frieren", episodes=1, first_air=START)
    world.show(2, "Dandadan", episodes=1, first_air=START)
    run_ingest(world, feed, ["[SubsPlease] Sousou no Frieren - 01 (1080p).mkv", "[SubsPlease] Dandadan - 05 (1080p).mkv",
                             "[Erai-raws] Frieren - Beyond Journey's End - 05 [1080p].mkv"])
    ingest.ensure_matches_current(world.ctx())
    world.commit()
    frieren.custom_aliases = ["Frieren - Beyond Journey's End"]
    world.session.add(frieren)
    world.commit()
    checked = []
    real = ingest.matching.broad_match
    monkeypatch.setattr(ingest.matching, "broad_match", lambda m, t, c=None: checked.append(t) or real(m, t, c))
    assert ingest.ensure_matches_current(world.ctx())
    world.commit()
    assert len(matches(world)) == 3
    # Only the changed show is tried against every item; Dandadan is asked only about the items Frieren touches.
    assert checked.count("[SubsPlease] Dandadan - 05 (1080p).mkv") == 1


def test_a_new_show_takes_over_the_releases_its_season_names(world):
    feed = world.feed("A", "https://a/rss", 1)
    first = world.show(1, "Foo", episodes=1, first_air=START)
    run_ingest(world, feed, ["[Grp] Foo 2nd Season - 01 (1080p).mkv"])
    ingest.ensure_matches_current(world.ctx())
    world.commit()
    assert [m[0] for m in matches(world)] == [first.id]
    second = world.show(2, "Foo 2nd Season", episodes=1, first_air=START)
    assert ingest.ensure_matches_current(world.ctx())
    world.commit()
    assert [m[0] for m in matches(world)] == [second.id]
