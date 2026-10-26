from typing import Optional

from kisetsu.db.schema import Event, EventKind, Show
from kisetsu.services.context import Context


def record(
    ctx: Context,
    kind: EventKind,
    message: str,
    *,
    show: Optional[Show] = None,
    episode: Optional[int] = None,
    feed_id: Optional[int] = None,
    title: str = "",
) -> Event:
    """Add a History entry. The caller's transaction commits it."""
    event = Event(
        at=ctx.now,
        kind=kind,
        show_id=show.id if show is not None else None,
        episode=episode,
        feed_id=feed_id,
        title=title,
        message=message,
    )
    ctx.session.add(event)
    ctx.emit("history")
    return event
