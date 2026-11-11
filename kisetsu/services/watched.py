"""Episodes the user has watched, going by their AniList progress.

With ``Settings.delete_watched`` on, a watched episode is not downloaded, and its torrent is deleted with its files.
Each show keeps the progress (``Show.watched``) and up to which episode it was acted on (``Show.watched_applied``), so
an episode is acted on once: one the user asks for again after it was watched is kept.
"""

from typing import Iterable, Optional

from sqlmodel import select

from kisetsu.db.schema import Episode, EpisodeStatus, Show
from kisetsu.services import downloads
from kisetsu.services.context import Context

# Watched episodes in these states that hold no torrent are skipped.
SKIPPABLE = (EpisodeStatus.WANTED, EpisodeStatus.MISSED, EpisodeStatus.FAILED)


def is_watched(ctx: Context, show: Show, number: int) -> bool:
    """Whether the episode is one to leave alone because the user watched it; always False with the setting off."""
    return ctx.settings.delete_watched and show.watched is not None and number <= show.watched


def _holds_nothing(ctx: Context, episode: Episode) -> bool:
    if downloads.active_download(ctx, episode) is not None:
        return False
    cur = downloads.current(ctx, episode)
    return cur is None or cur.info_hash is None


def skip_watched(ctx: Context, show: Show, after: int = 0, episodes: Optional[Iterable[Episode]] = None) -> int:
    """Skip the watched episodes past ``after`` that hold no torrent, so nothing downloads or searches for them.

    Returns how many were skipped. A torrent is left to ``apply``, which needs qBittorrent.
    """
    if not ctx.settings.delete_watched or not show.watched or show.watched <= after:
        return 0
    if episodes is None:
        episodes = ctx.session.exec(
            select(Episode).where(Episode.show_id == show.id, Episode.number > after, Episode.number <= show.watched)
        ).all()
    skipped = 0
    for episode in episodes:
        if after < episode.number <= show.watched and episode.status in SKIPPABLE and _holds_nothing(ctx, episode):
            episode.status, episode.last_error, episode.retry_after = EpisodeStatus.SKIPPED, None, None
            ctx.session.add(episode)
            skipped += 1
    if skipped:
        ctx.emit("episodes", f"show:{show.id}")
    return skipped


def note_progress(ctx: Context, show: Show, progress: int) -> bool:
    """Take in the progress a sync reported; returns whether the show changed.

    The first progress seen for a show is where it stands, not a watch. With deleting watched episodes off there is
    nothing to act on, so the progress counts as acted on at once. A lower one (a rewatch, a correction) lowers how far
    it was acted on too, so the episodes are acted on again when the progress passes them once more.
    """
    if show.watched == progress:
        return False
    if show.watched is None or show.watched_applied is None:
        show.watched = show.watched_applied = progress
        # Turned on before the progress was known: what was watched is not downloaded, and what is in qBittorrent stays.
        skip_watched(ctx, show)
        return True
    show.watched = progress
    if progress < show.watched_applied or not ctx.settings.delete_watched:
        show.watched_applied = progress
    else:
        # Before a grab could start them; their torrents wait for ``apply``.
        skip_watched(ctx, show, show.watched_applied)
    return True
