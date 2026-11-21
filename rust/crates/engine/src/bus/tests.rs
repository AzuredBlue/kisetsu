use super::*;

const SECOND: Duration = Duration::from_secs(1);

fn names(topics: &[&str]) -> BTreeSet<String> {
    topics.iter().map(|topic| (*topic).to_owned()).collect()
}

/// A bus state with one subscriber.
fn with_subscriber() -> (State, Arc<Queue>) {
    let queue = Arc::new(Queue::default());
    let state = State {
        subscribers: vec![Arc::clone(&queue)],
        ..State::default()
    };
    (state, queue)
}

fn drain(queue: &Queue) -> Vec<Change> {
    locked(&queue.state).items.drain(..).collect()
}

#[test]
fn changes_published_close_together_reach_subscribers_as_one_message() {
    let (mut state, queue) = with_subscriber();
    let start = Instant::now();
    let batch = Duration::from_millis(50);
    state.publish(names(&["shows", "show:1"]), batch, start);
    state.publish(names(&["shows", "show:2"]), batch, start + batch / 2);
    state.publish(names(&["logs"]), batch, start + batch / 2);
    assert_eq!(state.next_due(), Some(start + batch));
    state.flush_due(start + batch / 2);
    assert!(drain(&queue).is_empty());
    state.flush_due(start + batch);
    assert_eq!(
        drain(&queue),
        [Change {
            changed: ["logs", "show:1", "show:2", "shows"]
                .map(str::to_owned)
                .to_vec(),
            rev: 1
        }]
    );
    assert_eq!(state.next_due(), None);
}

#[test]
fn without_a_batch_every_change_goes_out_at_once() {
    let (mut state, queue) = with_subscriber();
    state.publish(names(&["feeds"]), Duration::ZERO, Instant::now());
    state.publish(names(&[]), Duration::ZERO, Instant::now());
    assert_eq!(
        drain(&queue),
        [Change {
            changed: vec!["feeds".to_owned()],
            rev: 1
        }]
    );
}

#[test]
fn a_topic_published_after_a_delay_goes_out_once_however_often_it_is_asked_for() {
    let (mut state, queue) = with_subscriber();
    let start = Instant::now();
    for _ in 0..50 {
        state.publish_after("requests", Duration::from_millis(50), start);
    }
    state.flush_due(start + Duration::from_millis(10));
    assert!(drain(&queue).is_empty());
    state.flush_due(start + Duration::from_millis(60));
    assert_eq!(
        drain(&queue)
            .iter()
            .map(|c| c.changed.clone())
            .collect::<Vec<_>>(),
        [vec!["requests".to_owned()]]
    );
    state.publish_after("requests", Duration::from_millis(10), start + SECOND);
    state.flush_due(start + SECOND + Duration::from_millis(10));
    assert_eq!(drain(&queue).len(), 1);
}

#[test]
fn a_subscriber_that_falls_behind_is_told_to_refetch_everything() {
    let (mut state, queue) = with_subscriber();
    for index in 0..=SUBSCRIBER_BACKLOG {
        state.deliver(vec![format!("show:{index}")]);
    }
    let rev = state.rev;
    assert_eq!(
        drain(&queue),
        [Change {
            changed: vec![EVERYTHING.to_owned()],
            rev
        }]
    );
}

#[test]
fn the_end_survives_a_subscriber_that_fell_behind_and_nothing_follows_it() {
    let bus = ChangeBus::new(Duration::ZERO);
    let subscription = bus.subscribe();
    for _ in 0..SUBSCRIBER_BACKLOG * 2 {
        bus.publish(["shows"]);
    }
    bus.shutdown();
    bus.publish(["status"]);
    assert_eq!(bus.subscribers(), 0);
    let mut heard = Vec::new();
    while let Ok(change) = subscription.recv_timeout(SECOND) {
        heard.push(change);
    }
    assert_eq!(subscription.recv_timeout(SECOND), Err(RecvError::Ended));
    assert!(heard.iter().all(|change| change.changed != ["status"]));
    assert!(bus.subscribe().recv_timeout(SECOND) == Err(RecvError::Ended));
}

#[test]
fn a_dropped_subscription_is_forgotten() {
    let bus = ChangeBus::default();
    let first = bus.subscribe();
    let second = bus.subscribe();
    assert_eq!(bus.subscribers(), 2);
    drop(first);
    assert_eq!(bus.subscribers(), 1);
    drop(second);
    assert_eq!(bus.subscribers(), 0);
}

#[test]
fn the_timer_sends_a_batch_as_one_message_and_a_held_topic_once() {
    let bus = ChangeBus::new(Duration::from_millis(100));
    let subscription = bus.subscribe();
    bus.publish(["shows"]);
    bus.publish(["logs"]);
    for _ in 0..20 {
        bus.publish_after("requests", Duration::from_millis(20));
    }
    let first = subscription.recv_timeout(Duration::from_secs(5)).unwrap();
    let second = subscription.recv_timeout(Duration::from_secs(5)).unwrap();
    assert_eq!(first.changed, ["requests"]);
    assert_eq!(second.changed, ["logs", "shows"]);
    assert_eq!(
        subscription.recv_timeout(Duration::from_millis(150)),
        Err(RecvError::Timeout)
    );
    assert_eq!(bus.rev(), 2);
}

#[test]
fn shutting_down_while_something_is_held_back_sends_nothing_more() {
    let bus = ChangeBus::new(Duration::from_millis(100));
    let subscription = bus.subscribe();
    bus.publish(["shows"]);
    bus.shutdown();
    assert_eq!(subscription.recv_timeout(SECOND), Err(RecvError::Ended));
    assert_eq!(bus.rev(), 0);
}
