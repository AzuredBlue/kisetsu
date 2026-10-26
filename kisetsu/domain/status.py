from datetime import datetime
from enum import Enum

from kisetsu.domain.schedule import ShowFacts, is_unreleased, waiting_for_release


class ShowStatus(str, Enum):
    PAUSED = "paused"
    COMPLETED = "completed"
    WORKING = "working"
    UPCOMING = "upcoming"
    WAITING = "waiting"
    TESTING = "testing"


def show_status(show: ShowFacts, now: datetime) -> ShowStatus:
    """The state a show is presented in, from the most to the least specific.

    Working: a feed has proven it carries the show. Upcoming: not out yet. Waiting: it aired,
    was hunted for a day, and is left to the routine feed read. Testing: everything else.
    """
    if show.paused:
        return ShowStatus.PAUSED
    if show.completed:
        return ShowStatus.COMPLETED
    if show.working:
        return ShowStatus.WORKING
    if is_unreleased(show, now):
        return ShowStatus.UPCOMING
    if waiting_for_release(show, now):
        return ShowStatus.WAITING
    return ShowStatus.TESTING
