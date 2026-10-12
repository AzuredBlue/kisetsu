"""Database bookkeeping for RSS feeds, shared by the API and the supervisor's feed sync."""
from typing import Dict

from sqlalchemy import delete, func, update
from sqlmodel import Session, select

from kisetsu.db.models import Episode, Feed, MatchedFeedItem, Monitored, MonitoredStatus, RuleHistory, SeenFeedItem


def forget_feed(session: Session, feed: Feed) -> None:
    """Delete a feed row and let go of everything that pointed at it.

    Shows on the feed go back to Auto-Discover, with the lock and any
    candidate dropped. The caller commits.
    """
    feed_id = feed.id
    for show in session.exec(
        select(Monitored).where(
            (Monitored.current_feed_id == feed_id)
            | (Monitored.learned_feed_id == feed_id)
            | (Monitored.candidate_feed_id == feed_id)
        )
    ).all():
        if show.current_feed_id == feed_id:
            show.current_feed_id = None
            show.feed_pinned = False
            if show.status == MonitoredStatus.FIXED:
                show.status = MonitoredStatus.UNCONFIRMED
        if show.learned_feed_id == feed_id:
            show.learned_feed_id = None
        if show.candidate_feed_id == feed_id:
            show.candidate_feed_id = None
            show.candidate_feed_since = None
        session.add(show)

    for episode in session.exec(select(Episode).where(Episode.feed_id == feed_id)).all():
        episode.feed_id = None
        session.add(episode)

    for history in session.exec(select(RuleHistory).where(RuleHistory.feed_id == feed_id)).all():
        history.feed_id = None
        session.add(history)

    session.delete(feed)


def compact_priorities(session: Session) -> bool:
    """Renumber feed priorities 1..n in their current order. True when any changed."""
    session.flush()
    changed = False
    feeds = session.exec(select(Feed).order_by(Feed.priority, Feed.id)).all()
    for idx, feed in enumerate(feeds, start=1):
        if feed.priority != idx:
            feed.priority = idx
            session.add(feed)
            changed = True
    return changed


def repoint_feed_url(session: Session, old_url: str, new_url: str) -> None:
    """Move the URL-keyed memory of a feed to its new URL, so an edit forgets nothing.

    Rows already stored under the new URL go first, or the unique constraints
    would refuse the move. The caller commits.
    """
    if old_url == new_url:
        return
    for model in (SeenFeedItem, MatchedFeedItem):
        session.execute(delete(model).where(model.feed_url == new_url))
        session.execute(update(model).where(model.feed_url == old_url).values(feed_url=new_url))


def feeds_usage(session: Session) -> Dict[int, Dict[str, int]]:
    """Per feed id: how many shows use it now, and how many are locked to it."""
    usage: Dict[int, Dict[str, int]] = {}
    for feed_id, count in session.exec(
        select(Monitored.current_feed_id, func.count()).where(Monitored.current_feed_id.is_not(None)).group_by(Monitored.current_feed_id)
    ).all():
        usage.setdefault(feed_id, {"shows": 0, "locked": 0})["shows"] = count
    for feed_id, count in session.exec(
        select(Monitored.learned_feed_id, func.count()).where(Monitored.learned_feed_id.is_not(None)).group_by(Monitored.learned_feed_id)
    ).all():
        usage.setdefault(feed_id, {"shows": 0, "locked": 0})["locked"] = count
    return usage
