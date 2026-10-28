"""Taking over torrents that are already in qBittorrent, and locking a show's feed from what it already has."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from sqlmodel import select

from kisetsu.db.schema import (
    Download,
    DownloadState,
    Episode,
    EpisodeOffset,
    EventKind,
    Feed,
    FeedItem,
    FeedItemMatch,
    FIXED_FEED_STATES,
    FeedState,
    Show,
)
from kisetsu.clients.qbittorrent import QbitError
from kisetsu.domain import matcher as matching
from kisetsu.domain.parsing import parse_release_title, release_key
from kisetsu.domain.schedule import PUBLISHED_BEFORE_AIR_SLACK, air_horizon, latest_aired
from kisetsu.services import events, feeds, library, tags
from kisetsu.services.context import Context
from kisetsu.services.downloads import MANAGED_TAG, recompute_episode
from kisetsu.services.facts import facts_for
from kisetsu.services.feeds import Evidence
from kisetsu.services.ingest import followed_shows

logger = logging.getLogger("kisetsu.services.adoption")

_REFUSED_LIMIT = 5000


@dataclass
class AdoptResult:
    imported: int = 0
    refused: int = 0


def managed_torrents(ctx: Context):
    """Torrents with the Kisetsu tag or in the user's category; torrents with neither are not ours."""
    category = ctx.settings.default_category
    return [
        t for t in ctx.qbit.torrents.values()
        if MANAGED_TAG in t.tags or (category and t.category == category)
    ]


def _added_before_air(added_on: Optional[datetime], reference: Optional[datetime], tolerance_hours: int) -> bool:
    """Whether qBittorrent added the torrent well before ``reference``, so it cannot be a release of what aired then."""
    if reference is None or added_on is None:
        return False
    return added_on < reference - max(PUBLISHED_BEFORE_AIR_SLACK, timedelta(hours=tolerance_hours))


def adoption_refusal(ctx: Context, show: Show, episode: Episode, torrent, newest_aired: Optional[int], first_air: Optional[datetime]) -> Optional[str]:
    """Why an existing torrent cannot be taken as the episode's release, or None when it can.

    A release name does not tell one season of a show from the next, so adoption goes by
    dates: an episode that has not aired has no release yet, and a torrent that qBittorrent
    added well before the episode could have aired is from an earlier run.
    """
    reason: Optional[str] = None
    if newest_aired is None or episode.number > newest_aired:
        reason = "the episode has not aired yet"
    elif _added_before_air(torrent.added_on, episode.air_at or first_air, ctx.tolerance_hours):
        reason = "it was added before the episode aired"
    if reason and torrent.hash not in ctx.caches.refused:
        if len(ctx.caches.refused) > _REFUSED_LIMIT:
            ctx.caches.refused.clear()
        ctx.caches.refused.add(torrent.hash)
        logger.debug(f"Not adopting '{torrent.name}' as Ep {episode.number} of '{show.title()}': {reason}.")
    return reason


def _carriers(ctx: Context, show: Show, name: str, info_hash: str) -> Tuple[Optional[Feed], Optional[FeedItem]]:
    """The one feed whose stored items carry this release, and the item; a release on several feeds proves none."""
    rows = ctx.session.exec(
        select(FeedItem)
        .join(FeedItemMatch, FeedItemMatch.item_id == FeedItem.id)
        .where(FeedItemMatch.show_id == show.id)
    ).all()
    by_hash = [r for r in rows if r.info_hash and r.info_hash == info_hash]
    if by_hash:
        found = by_hash
    else:
        key = release_key(name)
        found = [r for r in rows if key and (release_key(r.title) == key)]
        if not found and key:
            # The feed title may extend the torrent's name ("... (Ao no Hako Season 2, ...)").
            found = [r for r in rows if release_key(r.title).startswith(key + " ") or key.startswith(release_key(r.title) + " ")]
    feed_ids = {r.feed_id for r in found}
    # A release in several feeds proves none, unless the user pinned one of them.
    if len(feed_ids) > 1 and show.feed_state in FIXED_FEED_STATES and show.feed_id in feed_ids:
        feed_ids = {show.feed_id}
    if len(feed_ids) != 1:
        return None, None
    feed_id = next(iter(feed_ids))
    return ctx.session.get(Feed, feed_id), next(r for r in found if r.feed_id == feed_id)


def _known_hashes(ctx: Context) -> Set[str]:
    # A release a replacement superseded is accounted for: adopting it again would roll the episode back.
    return set(ctx.session.exec(select(Download.info_hash).where(Download.info_hash.is_not(None))).all())


def _hash_owners(ctx: Context, hashes: List[str], show_ids: Set[int]) -> Dict[str, int]:
    """The show a stored feed item with this info-hash was matched to; a hash on items of several shows names none."""
    owners: Dict[str, Set[int]] = {}
    rows = ctx.session.exec(
        select(FeedItem.info_hash, FeedItemMatch.show_id)
        .join(FeedItemMatch, FeedItemMatch.item_id == FeedItem.id)
        .where(FeedItem.info_hash.in_(hashes))
    ).all()
    for info_hash, show_id in rows:
        if show_id in show_ids:
            owners.setdefault(info_hash, set()).add(show_id)
    return {h: next(iter(ids)) for h, ids in owners.items() if len(ids) == 1}


def adopt_existing(ctx: Context) -> AdoptResult:
    """Fill gaps with the torrents already in qBittorrent; a tracked release is never overwritten."""
    result = AdoptResult()
    torrents = managed_torrents(ctx)
    if not torrents:
        return result
    known = _known_hashes(ctx)
    pending = [t for t in torrents if t.hash not in known and t.name]
    if not pending:
        return result
    shows = [s for s in followed_shows(ctx) if not s.completed]
    facts = facts_for(ctx, shows)
    matchers = [(s, matching.build_matcher(s.effective_aliases, s.learned_name)) for s in shows]
    by_id = {s.id: s for s in shows}
    carried = _hash_owners(ctx, [t.hash for t in pending], set(by_id))
    tagged_shows = tags.owners(ctx)
    retired_tags = set(tags.retired(ctx))
    cache = ctx.caches.parsed_cache()
    states: Dict[int, Tuple[List[Episode], Optional[int], Optional[datetime]]] = {}
    for torrent in pending:
        parsed = parse_release_title(torrent.name)
        raw = parsed.get("episode")
        if raw is None:
            continue
        # A series tag says which show the torrent belongs to; a show that is not followed now leaves it alone.
        tagged = tags.owner_id(torrent, tagged_shows)
        if tagged is not None:
            show = by_id.get(tagged)
        elif retired_tags & torrent.tags:
            show = None
        else:
            show = by_id.get(carried.get(torrent.hash)) or next((s for s, m in matchers if matching.decide(m, torrent.name, cache)[0]), None)
        if show is None:
            continue
        if show.id not in states:
            episodes = list(ctx.session.exec(select(Episode).where(Episode.show_id == show.id)).all())
            horizon = air_horizon(facts[show.id], ctx.now, ctx.tolerance_hours)
            first_air = min((e.air_at for e in episodes if e.air_at is not None), default=None)
            states[show.id] = (episodes, latest_aired(facts[show.id], ctx.now, horizon), first_air)
        episodes, newest_aired, first_air = states[show.id]
        # A feed that numbers episodes from the series' start (week 52 of a long run) is mapped by its known offset.
        carrier, _ = _carriers(ctx, show, torrent.name, torrent.hash)
        offset = _offset(ctx, show, carrier) if carrier is not None else 0
        episode = next((e for e in episodes if e.number == int(raw) - offset), None)
        if episode is None or not _is_gap(ctx, episode):
            continue
        if adoption_refusal(ctx, show, episode, torrent, newest_aired, first_air):
            result.refused += 1
            continue
        _adopt(ctx, show, episode, torrent, parsed, int(raw))
        known.add(torrent.hash)
        result.imported += 1
    if result.imported:
        ctx.emit("shows", "history")
    return result


def _offset(ctx: Context, show: Show, feed: Feed) -> int:
    row = ctx.session.exec(select(EpisodeOffset).where(EpisodeOffset.show_id == show.id, EpisodeOffset.feed_id == feed.id)).first()
    return row.offset if row is not None else 0


def _is_gap(ctx: Context, episode: Episode) -> bool:
    """Whether the episode has no tracked torrent: only gaps are filled."""
    active = ctx.session.exec(
        select(Download.id).where(Download.episode_id == episode.id, Download.state.in_([
            DownloadState.ADDING, DownloadState.VERIFYING, DownloadState.DOWNLOADING, DownloadState.RETRY_WAIT,
        ])).limit(1)
    ).first()
    if active is not None:
        return False
    cur = ctx.session.get(Download, episode.current_download_id) if episode.current_download_id else None
    return cur is None or cur.info_hash is None


def _adopt(ctx: Context, show: Show, episode: Episode, torrent, parsed: dict, raw: int) -> None:
    feed, item = _carriers(ctx, show, torrent.name, torrent.hash)
    dl = Download(
        episode_id=episode.id,
        feed_id=feed.id if feed else None,
        feed_item_guid=item.guid if item else None,
        title=torrent.name,
        parsed_title=item.parsed_title if item else parsed.get("title"),
        release_group=parsed.get("release_group"),
        version=max(1, int(parsed.get("version") or 1)),
        source_episode=raw,
        torrent_url=item.torrent_url if item else None,
        info_hash=torrent.hash,
        state=DownloadState.DONE if torrent.finished else DownloadState.DOWNLOADING,
        adopted=True,
        created_at=ctx.now,
        updated_at=ctx.now,
        completed_at=ctx.now if torrent.finished else None,
    )
    ctx.session.add(dl)
    ctx.session.flush()
    previous = ctx.session.get(Download, episode.current_download_id) if episode.current_download_id else None
    if previous is not None:
        previous.state = DownloadState.SUPERSEDED
        ctx.session.add(previous)
    episode.current_download_id = dl.id
    recompute_episode(ctx, episode)
    events.record(ctx, EventKind.ADOPTED, f"Found in qBittorrent: {torrent.name}", show=show, episode=episode.number, feed_id=dl.feed_id, title=torrent.name)
    try:
        ctx.qbit.add_tags([torrent.hash], [tags.series_tag(ctx, show)])
    except QbitError as e:
        logger.debug(f"Could not tag '{torrent.name}' with its show: {e}")
    # Only a feed that actually carries this release is recorded; an assigned feed is a guess and never the lock.
    if feed is not None and show.feed_state != FeedState.LOCKED:
        feeds.lock(ctx, show, feed, Evidence.ADOPTED_UNIQUE, learned_name=item.parsed_title if item else None, learned_group=item.release_group if item else None)


def failed_feed_ids(ctx: Context, show: Show) -> List[int]:
    return list(
        ctx.session.exec(
            select(Download.feed_id)
            .join(Episode, Episode.id == Download.episode_id)
            .where(Episode.show_id == show.id, Download.state == DownloadState.FAILED, Download.feed_id.is_not(None))
            .distinct()
        ).all()
    )


def _saved_file_candidates(ctx: Context) -> List[Tuple[Show, List[str]]]:
    shows = ctx.session.exec(
        select(Show).where(Show.completed == False, Show.paused == False, Show.feed_state.in_([FeedState.NONE, FeedState.CANDIDATE, FeedState.ASSIGNED]))  # noqa: E712
    ).all()
    candidates = []
    for show in shows:
        names = library.saved_release_names(show, ctx.settings)
        if names:
            candidates.append((show, names))
    return candidates


def _feed_from_saved_files(
    ctx: Context, show: Show, names: List[str], excluded: List[int]
) -> Optional[Tuple[Feed, Optional[str], Optional[str]]]:
    """(feed, release group, title) of the feed the newest saved file came from, if exactly one feed carries it.

    A file several feeds carry proves none of them. When the files came from different feeds,
    the newest episode is the latest choice.
    """
    best: Optional[Tuple[int, Feed, dict]] = None
    for name in names:
        feed, _ = _carriers(ctx, show, name, "")
        if feed is None or feed.id in excluded:
            continue
        parsed = parse_release_title(name)
        try:
            episode = int(parsed.get("episode") or 0)
        except (TypeError, ValueError):
            episode = 0
        if best is None or episode >= best[0]:
            best = (episode, feed, parsed)
    if best is None:
        return None
    return best[1], best[2].get("release_group"), best[2].get("title")


def lock_from_saved_files(ctx: Context) -> List[Show]:
    """Lock a show to the feed its saved files came from, so a library carried over from another machine keeps its source."""
    locked: List[Show] = []
    for show, names in _saved_file_candidates(ctx):
        found = _feed_from_saved_files(ctx, show, names, failed_feed_ids(ctx, show))
        if found is None:
            continue
        feed, group, title = found
        feeds.lock(ctx, show, feed, Evidence.SAVED_FILES_UNIQUE, learned_name=title, learned_group=group)
        library.release(ctx, show.id)
        locked.append(show)
    return locked


def unmatched_saved_files(ctx: Context) -> List[Show]:
    """Shows with saved files that no feed's items account for yet; they are held until a search has run."""
    unmatched = []
    for show, names in _saved_file_candidates(ctx):
        if _feed_from_saved_files(ctx, show, names, failed_feed_ids(ctx, show)) is None:
            unmatched.append(show)
        else:
            library.release(ctx, show.id)
    return unmatched


def reattribute(ctx: Context) -> int:
    """Give adopted downloads with no feed their feed once an item that carries them is stored.

    A torrent adopted before its feed item was known is never revisited by adoption, so its
    feed would stay unproven. Returns how many downloads got a feed.
    """
    rows = ctx.session.exec(
        select(Download, Episode)
        .join(Episode, Episode.id == Download.episode_id)
        .where(Download.adopted == True, Download.feed_id.is_(None), Download.info_hash.is_not(None))  # noqa: E712
    ).all()
    done = 0
    for dl, episode in rows:
        show = ctx.session.get(Show, episode.show_id)
        if show is None:
            continue
        feed, item = _carriers(ctx, show, dl.title, dl.info_hash)
        if feed is None:
            continue
        dl.feed_id, dl.feed_item_guid = feed.id, item.guid
        dl.torrent_url = dl.torrent_url or item.torrent_url
        dl.parsed_title = item.parsed_title or dl.parsed_title
        ctx.session.add(dl)
        if show.feed_state != FeedState.LOCKED:
            feeds.lock(ctx, show, feed, Evidence.ADOPTED_UNIQUE, learned_name=item.parsed_title, learned_group=item.release_group)
        done += 1
    return done
