"""Adding a show by hand from the user's AniList list.

The user picks an entry of their list, the feeds' sites are searched for it, and they choose
the feed and the episodes to download. Nothing is saved for the show until they confirm, so
the grabber can never pick a feed or start a download for it on its own meanwhile.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlmodel import select

from kisetsu.clients import sites
from kisetsu.clients.rss import FeedArticle
from kisetsu.db.schema import DismissedShow, Episode, EpisodeStatus, Feed, FeedItem, Show
from kisetsu.domain.matcher import build_matcher, decide
from kisetsu.domain.parsing import batch_range, is_batch, parse_release_title, release_season, strip_batch
from kisetsu.domain.patterns import sanitize_folder_name
from kisetsu.services import adoption, downloads, feeds, ingest, library, manual, shows
from kisetsu.services.context import Context
from kisetsu.services.feeds import Evidence

logger = logging.getLogger("kisetsu.services.addshow")

# The list statuses offered for adding: what the user watches, paused or dropped. Completed ones are left out.
LIST_STATUSES = ("CURRENT", "REPEATING", "PAUSED", "DROPPED")
LIST_LABELS = {"CURRENT": "Watching", "REPEATING": "Rewatching", "PAUSED": "Paused", "DROPPED": "Dropped"}


@dataclass
class Plan:
    """What searching for a new show needs: its names and the feeds whose sites can be searched."""

    terms: List[str]
    feeds: List[Feed]
    unsearchable: List[str] = field(default_factory=list)


def list_view(ctx: Context, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The user's list entries that can be added, each marked when Kisetsu already follows it."""
    followed = set(ctx.session.exec(select(Show.anilist_id)).all())
    language = ctx.settings.title_language
    rows = []
    for entry in entries:
        if entry.get("list_status") not in LIST_STATUSES:
            continue
        english, romaji = entry.get("title_english") or "", entry.get("title_romaji") or ""
        title = (english if language == "english" else romaji) or english or romaji or entry.get("display_name") or ""
        rows.append({
            "anilist_id": entry["anilist_id"],
            "title": title,
            "aliases": entry.get("aliases") or [],
            "cover": entry.get("cover_image") or None,
            "status": entry.get("status"),
            "list_status": entry.get("list_status"),
            "list_label": LIST_LABELS.get(entry.get("list_status"), entry.get("list_status")),
            "total_episodes": entry.get("total_episodes"),
            "season": " ".join(str(p) for p in (entry.get("season") and entry["season"].title(), entry.get("season_year")) if p) or None,
            "followed": entry["anilist_id"] in followed,
        })
    rows.sort(key=lambda row: row["title"].casefold())
    return rows


def _titles(entry: Dict[str, Any]) -> List[str]:
    return [t for t in (entry.get("title_romaji"), entry.get("title_english"), *(entry.get("aliases") or [])) if t]


def matcher_for(entry: Dict[str, Any]):
    return build_matcher(list(dict.fromkeys(_titles(entry))), None)


def search_plan(ctx: Context, entry: Dict[str, Any]) -> Plan:
    all_feeds = feeds.ranked(ctx)
    return Plan(
        terms=sites.search_terms(_titles(entry)),
        feeds=[f for f in all_feeds if sites.site_of(f.url)],
        unsearchable=[f.name for f in all_feeds if not sites.site_of(f.url)],
    )


def store_found(ctx: Context, found: Dict[int, List[FeedArticle]]) -> int:
    """Keep what the search found as ordinary feed items, so the chosen ones can be grabbed; returns the new rows."""
    added = 0
    for feed_id, articles in found.items():
        feed = ctx.session.get(Feed, feed_id)
        if feed is not None and articles:
            added += ingest.ingest(ctx, feed, articles).new
    return added


def _folder_names(entry: Dict[str, Any]) -> Set[str]:
    return {sanitize_folder_name(t).casefold() for t in _titles(entry)}


def existing_folder(ctx: Context, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A folder in the download folder named after one of the show's titles, with the show's video files in it."""
    root = library.base_root(ctx.settings.base_dir)
    if not root or not os.path.isdir(root):
        return None
    wanted = _folder_names(entry)
    matcher = matcher_for(entry)
    cache: Dict[str, dict] = {}
    try:
        folders = [e for e in os.scandir(root) if e.is_dir() and e.name.casefold() in wanted]
    except OSError as e:
        logger.debug(f"Could not read the download folder: {e}")
        return None
    for folder in sorted(folders, key=lambda e: e.name):
        names: List[str] = []
        try:
            for item in os.scandir(folder.path):
                if item.is_file():
                    names.append(item.name)
                elif item.is_dir():
                    names.extend(inner.name for inner in os.scandir(item.path) if inner.is_file())
        except OSError:
            continue
        videos = sorted(n for n in names if n.lower().endswith(library.VIDEO_EXTENSIONS) and decide(matcher, n, cache)[0])
        if videos:
            return {"name": folder.name, "path": folder.path, "files": videos}
    return None


def _save_folder(ctx: Context, show: Show, folder: Optional[Dict[str, Any]]) -> str:
    """The save folder setting that points the show at a folder found on disk ("" when that is its usual one)."""
    if folder is None:
        return ""
    default = sanitize_folder_name(show.title(ctx.settings.title_language))
    return "" if default.casefold() == folder["name"].casefold() else folder["name"]


def _draft(ctx: Context, entry: Dict[str, Any], folder: Optional[Dict[str, Any]]) -> Show:
    show = shows.show_from_entry(entry)
    show.save_folder = _save_folder(ctx, show, folder)
    ctx.session.add(show)
    ctx.session.flush()
    shows.ensure_episodes(ctx, show)
    ctx.session.flush()
    ingest.rematch(ctx, [show.id])
    return show


def _releases(ctx: Context, show: Show, feed: Feed) -> Dict[int, Any]:
    """The release the feed offers for each episode: the highest version, then the newest."""
    best: Dict[int, Any] = {}
    for _, entry in manual.candidates(ctx, show, [feed]):
        held = best.get(entry.number)
        key = (entry.version, entry.item.published_at or entry.item.first_seen_at)
        if held is None or key > (held.version, held.item.published_at or held.item.first_seen_at):
            best[entry.number] = entry
    return best


@dataclass
class Batch:
    item: FeedItem
    numbers: List[int]


def _covered(span: Optional[Tuple[int, int]], target: int) -> List[int]:
    """The show's episodes a batch holds: its range, read as the season's own numbers when they fit."""
    if span is None:
        return list(range(1, target + 1))
    first, last = span
    if last <= target:
        return list(range(first, last + 1))
    if last - first + 1 == target:
        # Numbered from the series' start: the batch is this whole season.
        return list(range(1, target + 1))
    # Longer than the season: another season's, or several seasons together.
    return []


def _batches(ctx: Context, show: Show, feed: Feed) -> List[Batch]:
    """The feed's batches of the show, the ones holding most episodes first.

    A batch names its season more often than single releases do, so it must name the same
    season as the show's titles; naming none counts as the first.
    """
    matcher = build_matcher(show.effective_aliases, None)
    cache: Dict[str, dict] = {}
    target = show.total_episodes or len(shows.ensure_episodes(ctx, show))
    season = max((n for n in (release_season(a) for a in show.effective_aliases) if n), default=1)
    found: List[Batch] = []
    for item in ctx.session.exec(select(FeedItem).where(FeedItem.feed_id == feed.id)).all():
        if not item.torrent_url or not is_batch(item.title):
            continue
        name = strip_batch(item.title)
        if (release_season(name) or 1) != season or not decide(matcher, name, cache)[0]:
            continue
        numbers = _covered(batch_range(item.title), target)
        if numbers:
            found.append(Batch(item, numbers))
    found.sort(key=lambda b: (-len(b.numbers), -(b.item.published_at or b.item.first_seen_at).timestamp()))
    return found


def _group_of(name: str) -> Optional[str]:
    group = parse_release_title(name).get("release_group")
    return group.casefold() if group else None


def _feed_from_files(ctx: Context, show: Show, files: List[str]) -> Optional[Feed]:
    """The feed the files on disk came from.

    Files a batch put there name no release of the feed; their release group then points at
    the one feed with a batch from that group.
    """
    found = adoption._feed_from_saved_files(ctx, show, files, [])
    if found is not None:
        return found[0]
    groups = {g for g in (_group_of(name) for name in files) if g}
    if not groups:
        return None
    carriers = [f for f in feeds.ranked(ctx) if any(_group_of(b.item.title) in groups for b in _batches(ctx, show, f))]
    return carriers[0] if len(carriers) == 1 else None


def preview(ctx: Context, entry: Dict[str, Any], unsearchable: Optional[List[str]] = None) -> Dict[str, Any]:
    """What each feed carries for the entry, episode by episode and in batches, and the feed its files on disk came from.

    Meant for ``Engine.dry_run``: the show it builds to number the releases exactly as the
    grabber will is rolled back with the rest of the run.
    """
    if ctx.session.exec(select(Show.id).where(Show.anilist_id == entry["anilist_id"])).first() is not None:
        raise ValueError("Kisetsu already follows this show.")
    folder = existing_folder(ctx, entry)
    show = _draft(ctx, entry, folder)
    on_disk_feed: Optional[Feed] = None
    note = None
    if folder is not None:
        on_disk_feed = _feed_from_files(ctx, show, folder["files"])
        if on_disk_feed is None:
            note = f"Found {len(folder['files'])} file(s) in {folder['path']}, but no feed matches their naming. Pick the feed yourself."
    files = folder["files"] if folder else []
    disk_keys = {library_key(name) for name in files}
    disk_numbers = {parse_release_title(name).get("episode") for name in files}
    groups = []
    for feed in feeds.ranked(ctx):
        releases = _releases(ctx, show, feed)
        batches = _batches(ctx, show, feed)
        if not releases and not batches:
            continue
        groups.append({
            "feed_id": feed.id,
            "feed_name": feed.name,
            "batches": [
                {
                    "item_id": b.item.id,
                    "title": b.item.title,
                    "numbers": b.numbers,
                    "ranged": batch_range(b.item.title) is not None,
                    "published_at": b.item.published_at.isoformat() if b.item.published_at else None,
                    "on_disk": len(disk_numbers & set(b.numbers)),
                }
                for b in batches
            ],
            "episodes": [
                {
                    "number": number,
                    "item_id": r.item.id,
                    "title": r.item.title,
                    "version": r.version,
                    "published_at": r.item.published_at.isoformat() if r.item.published_at else None,
                    "on_disk": library_key(r.item.title) in disk_keys,
                }
                for number, r in sorted(releases.items())
            ],
        })
    return {
        "anilist_id": entry["anilist_id"],
        "title": show.title(ctx.settings.title_language),
        "status": entry.get("status"),
        "total_episodes": show.total_episodes,
        "episode_count": len(shows.ensure_episodes(ctx, show)),
        "feeds": groups,
        "unsearchable": list(unsearchable or []),
        "folder": None if folder is None else {"path": folder["path"], "files": len(folder["files"])},
        "preselect": on_disk_feed.id if on_disk_feed is not None and any(g["feed_id"] == on_disk_feed.id for g in groups) else None,
        "note": note,
    }


def library_key(name: str) -> str:
    """A release name compared without its extension or case, as file names and feed titles differ there."""
    base, ext = os.path.splitext(name)
    return (base if ext.lower() in library.VIDEO_EXTENSIONS else name).casefold().strip()


def confirm(ctx: Context, entry: Dict[str, Any], feed_id: int, item_ids: List[int]) -> Dict[str, Any]:
    """Follow the entry from ``feed_id`` and grab the chosen releases; the feed's other episodes are skipped."""
    if ctx.session.exec(select(Show).where(Show.anilist_id == entry["anilist_id"])).first() is not None:
        raise ValueError("Kisetsu already follows this show.")
    feed = ctx.session.get(Feed, feed_id)
    if feed is None:
        raise ValueError("Selected feed not found")
    folder = existing_folder(ctx, entry)
    paused_all = shows.all_paused(ctx)
    show = shows.show_from_entry(entry, paused_all)
    show.save_folder = _save_folder(ctx, show, folder)
    ctx.session.add(show)
    ctx.session.flush()
    dismissed = ctx.session.get(DismissedShow, show.anilist_id)
    if dismissed is not None:
        ctx.session.delete(dismissed)
    episodes = shows.ensure_episodes(ctx, show)
    ctx.session.flush()
    ingest.rematch(ctx, [show.id])
    feeds.pin(ctx, show, feed)
    if folder is not None:
        found = adoption._feed_from_saved_files(ctx, show, folder["files"], [])
        if found is not None and found[0].id == feed.id:
            # The user kept the feed the files on disk came from: that is the evidence a lock needs.
            feeds.lock(ctx, show, feed, Evidence.SAVED_FILES_UNIQUE, learned_name=found[2], learned_group=found[1])
    chosen = set(item_ids)
    releases = _releases(ctx, show, feed)
    batches = _batches(ctx, show, feed)
    # Each episode comes from one release: a chosen batch holds its episodes, singles fill in the rest.
    picked: Dict[int, Tuple[Episode, FeedItem]] = {}
    by_number = {e.number: e for e in episodes}
    for batch in batches:
        if batch.item.id in chosen:
            for number in batch.numbers:
                if number in by_number:
                    picked.setdefault(number, (by_number[number], batch.item))
    for number, release in releases.items():
        if release.item.id in chosen:
            picked.setdefault(number, (release.episode, release.item))
    offered = set(releases) | {n for b in batches for n in b.numbers}
    for episode in episodes:
        if episode.number in offered and episode.number not in picked:
            episode.status = EpisodeStatus.SKIPPED
            ctx.session.add(episode)
    ctx.session.flush()
    name = show.title(ctx.settings.title_language)
    logger.info(f"Added '{name}' by hand, from {feed.name}.")
    ctx.emit("shows", f"show:{show.id}")
    result = {"show_id": show.id, "started": 0, "skipped": len([e for e in episodes if e.status == EpisodeStatus.SKIPPED]), "errors": []}
    if paused_all or not picked:
        return result
    ctx.checkpoint()
    # Torrents already in qBittorrent are taken over rather than added twice.
    adoption.adopt_existing(ctx)
    added: Dict[int, str] = {}
    for number, (episode, item) in sorted(picked.items()):
        ctx.session.refresh(episode)
        if episode.status not in (EpisodeStatus.WANTED, EpisodeStatus.MISSED):
            continue
        if item.id in added:
            # The rest of a batch already in qBittorrent follow that torrent; adding it again would fetch it again.
            dl = downloads.follow_download(ctx, show, feed, episode, item, added[item.id])
        else:
            dl = downloads.start_download(ctx, show, feed, episode, item)
            if dl is not None and is_batch(item.title) and (dl.info_hash or item.info_hash):
                added[item.id] = dl.info_hash or item.info_hash
        if dl is None:
            result["errors"].append(f"Ep {number}: {episode.last_error or 'could not start the download'}")
        else:
            result["started"] += 1
    return result
