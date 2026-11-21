//! Work that waits on the network, done a few items at a time.

use std::panic;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;

/// `work` on every item, at most `limit` at a time, with the results in the order of the items.
/// A panic in `work` goes on as a panic of the caller.
pub fn map_limited<T: Sync, R: Send>(
    items: &[T],
    limit: usize,
    work: impl Fn(&T) -> R + Sync,
) -> Vec<R> {
    let workers = limit.clamp(1, items.len().max(1));
    if workers == 1 {
        return items.iter().map(work).collect();
    }
    let next = AtomicUsize::new(0);
    let mut done: Vec<(usize, R)> = thread::scope(|scope| {
        let handles: Vec<_> = (0..workers)
            .map(|_| {
                scope.spawn(|| {
                    let mut mine = Vec::new();
                    loop {
                        let at = next.fetch_add(1, Ordering::Relaxed);
                        let Some(item) = items.get(at) else { break };
                        mine.push((at, work(item)));
                    }
                    mine
                })
            })
            .collect();
        handles
            .into_iter()
            .flat_map(|handle| {
                handle
                    .join()
                    .unwrap_or_else(|panic| panic::resume_unwind(panic))
            })
            .collect()
    });
    done.sort_by_key(|(at, _)| *at);
    done.into_iter().map(|(_, result)| result).collect()
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::AtomicUsize;
    use std::time::Duration;

    use super::*;

    #[test]
    fn results_come_back_in_the_order_of_the_items_and_never_more_than_the_limit_run_at_once() {
        let active = AtomicUsize::new(0);
        let peak = AtomicUsize::new(0);
        let items: Vec<u64> = (0..12).collect();
        let doubled = map_limited(&items, 3, |item| {
            let now = active.fetch_add(1, Ordering::SeqCst) + 1;
            peak.fetch_max(now, Ordering::SeqCst);
            thread::sleep(Duration::from_millis(5));
            active.fetch_sub(1, Ordering::SeqCst);
            item * 2
        });
        assert_eq!(doubled, (0..12).map(|item| item * 2).collect::<Vec<_>>());
        assert!(peak.load(Ordering::SeqCst) <= 3);
    }

    #[test]
    fn nothing_to_do_is_nothing_done() {
        assert!(map_limited(&[] as &[u8], 4, |_| 1).is_empty());
    }
}
