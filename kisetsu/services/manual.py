"""Explicit user actions on releases: list what the feeds carry, grab one, restore a lost torrent."""

from typing import Any, Dict, List, Optional, Tuple

from kisetsu.db.schema import (
    Download,
    DownloadState,
    Episode,
    EpisodeStatus,
    Feed,
    FeedItem,
    FeedState,
    Show,
)
from kisetsu.domain.parsing import same_release
from kisetsu.domain.schedule import RELEASE_HOLDING
from kisetsu.services import downloads, feeds, grab, tags
from kisetsu.services.context import Context
from kisetsu.services.downloads import MANAGED_TAG
from kisetsu.services.facts import facts_for

Candidate = Tuple[Feed, grab.Resolved]


def _is_restore(ctx: Context, episode: Episode) -> bool:
    """A downloaded episode Kisetsu holds no torrent for, so it cannot manage it."""
    cur = downloads.current(ctx, episode)
    return episode.status == EpisodeStatus.COMPLETED and (cur is None or cur.info_hash is None)


def _other_source(ctx: Context, episode: Episode, feed: Feed, item: FeedItem) -> bool:
    """Whether ``item`` is another copy of a finished, managed episode.

    That is a different feed than the one the episode came from, or, when the feed was not
    recorded, a release that is not the one already downloaded.
    """
    cur = downloads.current(ctx, episode)
    if episode.status != EpisodeStatus.COMPLETED or cur is None or not cur.info_hash:
        return False
    if item.info_hash and item.info_hash == cur.info_hash:
        return False
    if cur.feed_id is not None:
        return cur.feed_id != feed.id
    return not same_release(cur.title, item.title)


def _refusal(ctx: Context, episode: Episode, version: int, other_source: bool = False, *, stalled: Optional[Download] = None) -> Optional[str]:
    """Why an explicit grab of ``version`` of ``episode`` must not go ahead.

    ``stalled`` is the one active download that is about to be dropped; any other still blocks the grab.
    """
    active = downloads.active_download(ctx, episode)
    if active is not None and (stalled is None or active.id != stalled.id):
        return f"Episode {episode.number} already has a download in progress."
    if _is_restore(ctx, episode):
        return None
    cur = downloads.current(ctx, episode)
    held = cur.version if cur is not None else 1
    if cur is not None and downloads.is_batch_download(cur) and episode.status in RELEASE_HOLDING:
        return f"Episode {episode.number} came in a batch; a single release won't replace it."
    if other_source:
        if episode.status == EpisodeStatus.COMPLETED and version < held:
            return f"Episode {episode.number} already has v{held}; an older version won't replace it."
        return None
    if episode.status in RELEASE_HOLDING and version <= held:
        return f"Episode {episode.number} v{held} is already downloaded or queued."
    return None


def _own_feeds(ctx: Context, show: Show) -> List[Feed]:
    all_feeds = feeds.ranked(ctx)
    if show.feed_state in (FeedState.LOCKED, FeedState.PINNED):
        return [f for f in all_feeds if f.id == show.feed_id]
    if show.feed_state == FeedState.ASSIGNED:
        assigned = [f for f in all_feeds if f.id == show.feed_id]
        return assigned or all_feeds
    return all_feeds


def candidates(ctx: Context, show: Show, feed_list: Optional[List[Feed]] = None) -> List[Candidate]:
    """Every stored item the grabber would accept for the show, with its episode.

    Reads only the feeds the show may use, so the feed lock is honoured, and never writes.
    ``feed_list`` replaces that set, to list what other feeds carry.
    """
    chosen = _own_feeds(ctx, show) if feed_list is None else feed_list
    if not chosen:
        return []
    facts = facts_for(ctx, [show])[show.id]
    state = grab._state(ctx, show, facts)
    found: List[Candidate] = []
    for feed in chosen:
        resolved, _ = grab.resolve(ctx, state, feed, learn=False)
        found.extend((feed, entry) for entry in resolved)
    return found


def _role(show: Show, feed: Feed) -> str:
    if show.feed_id != feed.id:
        return "discovery"
    return {FeedState.LOCKED: "locked", FeedState.PINNED: "picked", FeedState.ASSIGNED: "assigned"}.get(show.feed_state, "discovery")


def feed_matches(ctx: Context, show: Show, limit: int = 30) -> List[Dict[str, Any]]:
    """Releases the engine recognises, grouped by feed, for the UI.

    The feeds the show may read come first, each with a ``role`` and its releases ready to
    download. Other feeds that carry releases follow as ``other`` groups, for information
    only: offering a download from them would bypass the feed lock.
    """
    all_feeds = feeds.ranked(ctx)
    own = _own_feeds(ctx, show)
    own_ids = {f.id for f in own}
    groups: Dict[int, Dict[str, Any]] = {}

    def add(feed: Feed, role: str, usable: bool, found: List[Candidate]) -> None:
        for _, entry in found:
            group = groups.setdefault(
                feed.id, {"feed_id": feed.id, "feed_name": feed.name, "role": role, "usable": usable, "matches": []}
            )
            if len(group["matches"]) >= limit:
                break
            episode, item = entry.episode, entry.item
            if usable:
                replaces = _other_source(ctx, episode, feed, item)
                refusal = _refusal(ctx, episode, entry.version, replaces)
            else:
                replaces, refusal = False, "other feed"
            cur = downloads.current(ctx, episode)
            this_release = cur is not None and (
                bool(item.info_hash and cur.info_hash == item.info_hash) or same_release(cur.title, item.title)
            )
            group["matches"].append({
                "title": item.title,
                "display_title": cur.title if this_release else item.title,
                "feed_id": feed.id,
                "feed_name": feed.name,
                "episode": entry.number,
                "version": entry.version,
                "episode_status": episode.status.value,
                "restore": usable and _is_restore(ctx, episode),
                "replaces": bool(replaces) and refusal is None,
                "downloadable": refusal is None,
            })

    for feed, entry in candidates(ctx, show):
        add(feed, _role(show, feed), True, [(feed, entry)])
    for feed in all_feeds:
        if feed.id not in own_ids:
            add(feed, "other", False, candidates(ctx, show, [feed]))
    # A feed that could not be read has nothing stored, so it would simply be missing here.
    for feed in own:
        if feed.failing_since is not None and feed.id not in groups:
            groups[feed.id] = {
                "feed_id": feed.id, "feed_name": feed.name, "role": _role(show, feed),
                "usable": True, "unavailable": True, "matches": [],
            }
    return [g for g in groups.values() if g["feed_id"] in own_ids] + [g for g in groups.values() if g["feed_id"] not in own_ids]


def manual_grab(
    ctx: Context,
    show: Show,
    title: str,
    *,
    fuzzy: bool = False,
    expect_episode: Optional[int] = None,
) -> str:
    """Add one named release through the normal download path.

    This is an explicit user action, so the discovery grace window does not apply: a MISSED
    or FAILED episode that is still in a feed may be fetched again. With ``fuzzy`` the title
    may be the release under another name (a torrent's file name rather than the feed title).
    """
    if show.paused:
        raise ValueError("This show is paused. Resume it before downloading.")
    wanted = title.strip()
    found = candidates(ctx, show)
    pick = next(((f, e) for f, e in found if e.item.title == wanted), None)
    if pick is None and fuzzy:
        near = [(f, e) for f, e in found if same_release(e.item.title, wanted)]
        if len(near) > 1:
            raise ValueError("Several releases in the feed match that name; pick one from the Releases tab.")
        pick = near[0] if near else None
    if pick is None:
        raise ValueError("That release is not in this show's feed any more, or does not match this show.")
    feed, entry = pick
    if expect_episode is not None and entry.number != expect_episode:
        raise ValueError(f"The matching release in the feed is episode {entry.number}, not episode {expect_episode}.")
    switch_source = _other_source(ctx, entry.episode, feed, entry.item)
    refusal = _refusal(ctx, entry.episode, entry.version, switch_source)
    if refusal:
        raise ValueError(refusal)
    facts = facts_for(ctx, [show])[show.id]
    grab.record_evidence(ctx, grab._state(ctx, show, facts), feed, entry.raw, entry.number)
    previous = downloads.current(ctx, entry.episode)
    dl = downloads.start_download(ctx, show, feed, entry.episode, entry.item, switch_source=switch_source)
    if dl is None:
        raise ValueError(entry.episode.last_error or "Could not start the download for that release.")
    if show.feed_state in (FeedState.NONE, FeedState.CANDIDATE):
        show.feed_id, show.feed_state, show.candidate_since = feed.id, FeedState.ASSIGNED, None
        ctx.session.add(show)
    if dl.supersedes_id is None:
        action = "download"
    elif previous is not None and previous.info_hash is None:
        action = "restore"
    else:
        action = "replacement"
    return f"Manually queued {action} for {show.title(ctx.settings.title_language)} Ep {entry.number} v{entry.version}: {entry.item.title}"


def stalled_download(ctx: Context, episode: Episode) -> Optional[Download]:
    """The episode's active download, if the last settle pass found its torrent stalled."""
    active = downloads.active_download(ctx, episode)
    return active if active is not None and active.id in ctx.caches.stalled else None


def alternatives(ctx: Context, show: Show, episode: Episode, dl: Download, found: Optional[List[Candidate]] = None) -> List[Candidate]:
    """The other releases of the episode that could take a stalled download's place, best first.

    They come from the feeds the show may use (so its feed lock stands) and never include the stalled release itself.
    A batch is never offered a swap: its torrent serves other episodes too.
    """
    if downloads.is_batch_download(dl):
        return []
    options: List[Candidate] = []
    for feed, entry in candidates(ctx, show) if found is None else found:
        item = entry.item
        if entry.episode.id != episode.id or not item.torrent_url:
            continue
        dead = item.title == dl.title or (item.info_hash and item.info_hash == dl.info_hash) or (feed.id == dl.feed_id and item.guid == dl.feed_item_guid)
        if dead:
            continue
        if dl.supersedes_id is not None and _refusal(ctx, episode, entry.version, _other_source(ctx, episode, feed, item), stalled=dl):
            # A stalled replacement leaves the previous release in place, and a new one must still be able to replace it.
            continue
        options.append((feed, entry))
    return sorted(options, key=lambda option: -option[1].version)


def try_another(ctx: Context, show: Show, episode: Episode) -> str:
    """Drop the episode's stalled download and add the best other release of it, in one step."""
    if show.paused:
        raise ValueError("This show is paused. Resume it before downloading.")
    dl = stalled_download(ctx, episode)
    if dl is None:
        raise ValueError(f"Episode {episode.number} has no stalled download.")
    options = alternatives(ctx, show, episode, dl)
    if not options:
        raise ValueError(f"No other release of episode {episode.number} is in the feeds this show uses.")
    feed, entry = options[0]
    switch_source = _other_source(ctx, episode, feed, entry.item)
    facts = facts_for(ctx, [show])[show.id]
    grab.record_evidence(ctx, grab._state(ctx, show, facts), feed, entry.raw, entry.number)
    hours = int(downloads.STALLED_AFTER.total_seconds() // 3600)
    downloads.abandon_stalled(ctx, dl, f"Stalled for over {hours} hours; replaced by another release.")
    started = downloads.start_download(ctx, show, feed, episode, entry.item, switch_source=switch_source)
    if started is None:
        raise ValueError(entry.episode.last_error or "Could not start the download for that release.")
    return f"Dropped the stalled release of {show.title(ctx.settings.title_language)} Ep {entry.number} and queued {entry.item.title}"


def restore_episode(ctx: Context, show: Show, episode: Episode) -> str:
    """Bring a finished episode with no tracked torrent under management.

    The torrent is adopted if qBittorrent still holds it, otherwise the release is added
    again from the feed.
    """
    if not _is_restore(ctx, episode):
        raise ValueError(f"Episode {episode.number} is already managed.")
    if downloads.active_download(ctx, episode) is not None:
        raise ValueError(f"Episode {episode.number} already has a download in progress.")
    cur = downloads.current(ctx, episode)
    title = (cur.title if cur is not None else "").strip()
    if not title:
        raise ValueError(f"No release is recorded for episode {episode.number}.")
    ctx.qbit.sync()
    named = [t for t in ctx.qbit.torrents.values() if same_release(t.name, title)]
    if len(named) == 1:
        torrent = named[0]
        ctx.qbit.add_tags([torrent.hash], [MANAGED_TAG, tags.series_tag(ctx, show)])
        cur.info_hash = torrent.hash
        cur.state = DownloadState.DONE if torrent.finished else DownloadState.DOWNLOADING
        ctx.session.add(cur)
        downloads.recompute_episode(ctx, episode)
        return f"Restored {show.title(ctx.settings.title_language)} Ep {episode.number}: now managed."
    try:
        return manual_grab(ctx, show, title, fuzzy=True, expect_episode=episode.number)
    except ValueError as e:
        raise ValueError(f"Not in qBittorrent. {e}") from e
