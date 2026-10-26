"""Per-show choices carried over from the old database, applied when AniList first adds the show."""
import json
from typing import Any, Dict, List

from sqlmodel import select

from kisetsu.db.schema import EpisodeOffset, Feed, FeedState, OffsetSource, Show
from kisetsu.services.context import Context, get_state, set_state

PENDING_KEY = "legacy_overrides"


def _fold(name: str) -> str:
    return " ".join(name.lower().split())


def store_pending(ctx: Context, overrides: Dict[int, Dict[str, Any]]) -> None:
    set_state(ctx, PENDING_KEY, json.dumps({str(k): v for k, v in overrides.items()}))


def apply_pending(ctx: Context, show: Show) -> bool:
    """Apply and consume the imported choices for a newly added show; returns whether there were any."""
    raw = get_state(ctx, PENDING_KEY)
    if not raw:
        return False
    pending = json.loads(raw)
    entry = pending.pop(str(show.anilist_id), None)
    if entry is None:
        return False
    set_state(ctx, PENDING_KEY, json.dumps(pending))
    if entry.get("save_folder"):
        show.save_folder = entry["save_folder"]
    known = {_fold(name) for name in [*show.aliases, show.title_romaji or "", show.title_english or ""]}
    extra: List[str] = []
    for name in entry.get("aliases", []):
        if _fold(name) not in known:
            known.add(_fold(name))
            extra.append(name)
    if extra:
        show.custom_aliases = [*show.custom_aliases, *extra]
    feeds = {f.url: f for f in ctx.session.exec(select(Feed)).all()}
    pinned = feeds.get(entry.get("pinned_feed_url") or "")
    if pinned is not None:
        show.feed_id, show.feed_state = pinned.id, FeedState.PINNED
    ctx.session.add(show)
    for item in entry.get("offsets", []):
        feed = feeds.get(item["feed_url"])
        if feed is None:
            continue
        ctx.session.add(EpisodeOffset(
            show_id=show.id, feed_id=feed.id, offset=item["offset"],
            source=OffsetSource[item["source"]], evidence_count=item.get("evidence_count", 0),
        ))
    return True
