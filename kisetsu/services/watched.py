"""Episodes the user has watched, going by their AniList progress."""

from kisetsu.db.schema import Show
from kisetsu.services.context import Context


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
        return True
    show.watched = progress
    if progress < show.watched_applied or not ctx.settings.delete_watched:
        show.watched_applied = progress
    return True
