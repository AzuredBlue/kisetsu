"""A qBittorrent tag per show, named after it, so its torrents can be filtered there and told apart here."""

import logging
import re
from collections import Counter
from typing import Dict, Optional

from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError, Torrent
from kisetsu.db.schema import Show
from kisetsu.services.context import Context

logger = logging.getLogger("kisetsu.services.tags")


def _clean(name: str) -> str:
    """The name as a tag: qBittorrent splits tags on commas, so they go, and spaces are collapsed."""
    return re.sub(r"\s+", " ", name.replace(",", " ")).strip()


def wanted_tags(ctx: Context) -> Dict[int, str]:
    """The tag each show should carry, by show id. Shows whose names come out the same get their AniList id added."""
    shows = list(ctx.session.exec(select(Show)).all())
    language = ctx.settings.title_language
    names = {show.id: _clean(show.title(language)) or f"AniList {show.anilist_id}" for show in shows}
    counts = Counter(names.values())
    return {show.id: f"{names[show.id]} #{show.anilist_id}" if counts[names[show.id]] > 1 else names[show.id] for show in shows}


def series_tag(ctx: Context, show: Show) -> str:
    """The tag for a torrent being added for this show; the first one is remembered on the show."""
    tag = wanted_tags(ctx)[show.id]
    if not show.qbit_tag:
        show.qbit_tag = tag
        ctx.session.add(show)
    return tag


def owners(ctx: Context) -> Dict[str, int]:
    """Which show each series tag names. A show not tagged yet is known by the tag it would get."""
    wanted = wanted_tags(ctx)
    found: Dict[str, set] = {}
    for show in ctx.session.exec(select(Show)).all():
        found.setdefault(show.qbit_tag or wanted[show.id], set()).add(show.id)
    return {tag: next(iter(ids)) for tag, ids in found.items() if len(ids) == 1}


def owner_id(torrent: Torrent, owned: Dict[str, int]) -> Optional[int]:
    """The show the torrent's tags name, if they name exactly one."""
    ids = {owned[tag] for tag in torrent.tags if tag in owned}
    return next(iter(ids)) if len(ids) == 1 else None


def sync_tags(ctx: Context) -> int:
    """Give every torrent Kisetsu tracks its show's tag, and move the tag when the show's name changed.

    Returns how many torrents were tagged or retagged. A qBittorrent failure stops the pass;
    what was not stored on a show is tried again next time.
    """
    if ctx.qbit is None:
        return 0
    from kisetsu.services import library  # library reads the tags too, so it cannot be imported at the top

    changed = 0
    try:
        if not ctx.qbit.synced:
            ctx.qbit.sync()
        view = ctx.qbit.torrents
        wanted = wanted_tags(ctx)
        in_use = set(wanted.values())
        for show in ctx.session.exec(select(Show)).all():
            tag, old = wanted[show.id], show.qbit_tag
            tracked = [h for h in library.show_torrent_hashes(ctx, show) if h in view]
            if old and old != tag:
                carrying = [h for h in tracked if old in view[h].tags]
                if carrying:
                    ctx.qbit.remove_tags(carrying, [old])
                if old not in in_use and not any(old in t.tags for h, t in view.items() if h not in carrying):
                    ctx.qbit.delete_tags([old])
            missing = [h for h in tracked if tag not in view[h].tags]
            if missing:
                ctx.qbit.add_tags(missing, [tag])
                changed += len(missing)
            if old != tag:
                show.qbit_tag = tag
                ctx.session.add(show)
    except QbitError as e:
        logger.debug(f"Could not tag torrents by show: {e}")
    return changed
