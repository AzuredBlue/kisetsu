use jiff::Timestamp;

use crate::schedule::{ShowFacts, is_unreleased, waiting_for_release};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShowStatus {
    Paused,
    Completed,
    Working,
    Upcoming,
    Waiting,
    Testing,
}

/// The state a show is presented in, from the most to the least specific.
///
/// Working: a feed has proven it carries the show. Upcoming: not out yet. Waiting: it aired,
/// was hunted for a day, and is left to the routine feed read. Testing: everything else.
pub fn show_status(show: &ShowFacts, now: Timestamp) -> ShowStatus {
    if show.paused {
        ShowStatus::Paused
    } else if show.completed {
        ShowStatus::Completed
    } else if show.working {
        ShowStatus::Working
    } else if is_unreleased(show, now) {
        ShowStatus::Upcoming
    } else if waiting_for_release(show, now) {
        ShowStatus::Waiting
    } else {
        ShowStatus::Testing
    }
}
