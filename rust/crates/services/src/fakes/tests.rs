use jiff::SignedDuration;
use kisetsu_clients::qbittorrent::{AddTorrent, ContentLayout, Qbit, QbitError};

use super::*;
use crate::context::{AniListSource, FeedSource, TagSource};

const HASH: &str = "0123456789abcdef0123456789abcdef01234567";

fn add(url: &str, save_path: &str) -> AddTorrent {
    AddTorrent {
        save_path: save_path.to_owned(),
        ..AddTorrent::new(url)
    }
}

#[test]
fn a_torrent_is_known_by_the_hash_its_address_carries() {
    assert_eq!(hash_for(&format!("https://x.test/{HASH}.torrent")), HASH);
    assert_eq!(
        hash_for(&format!("magnet:?xt=urn:btih:{HASH}&dn=Name")),
        HASH
    );
    let other = hash_for("https://x.test/other");
    assert_eq!(other.len(), 40);
    assert_ne!(other, hash_for("https://x.test/another"));
}

#[test]
fn a_sync_reports_what_changed_since_the_last_one() {
    let mut qbit = FakeQbit::new(FakeClock::default());
    qbit.add_torrent(&add("https://x.test/a", "/dl")).unwrap();
    let first = qbit.sync(false).unwrap();
    assert!(first.full);
    assert_eq!(first.changed.len(), 1);
    assert!(!qbit.sync(false).unwrap().has_changes());
    let hash = hash_for("https://x.test/a");
    qbit.finish(&hash);
    let second = qbit.sync(false).unwrap();
    assert!(!second.full && second.changed.contains(&hash));
    qbit.remove(&hash);
    assert!(qbit.sync(false).unwrap().removed.contains(&hash));
    assert!(qbit.sync(true).unwrap().full);
}

#[test]
fn an_added_torrent_starts_in_its_folder_and_a_duplicate_is_refused() {
    let mut qbit = FakeQbit::new(FakeClock::default());
    qbit.add_torrent(&AddTorrent {
        paused: true,
        ..add("https://x.test/a.mkv", "/dl")
    })
    .unwrap();
    let torrent = qbit.torrent_for("https://x.test/a.mkv").unwrap();
    assert_eq!(torrent.state, "stoppedDL");
    assert_eq!(torrent.content_path, "/dl/a.mkv");
    assert!(matches!(
        qbit.add_torrent(&add("https://x.test/a.mkv", "/dl")),
        Err(QbitError::Duplicate(_))
    ));
    qbit.add_torrent(&AddTorrent {
        content_layout: Some(ContentLayout::NoSubfolder),
        ..add("https://x.test/b", "/dl")
    })
    .unwrap();
    assert_eq!(
        qbit.torrent_for("https://x.test/b").unwrap().content_path,
        "/dl"
    );
}

#[test]
fn torrents_added_while_hidden_appear_on_reveal() {
    let mut qbit = FakeQbit::new(FakeClock::default());
    qbit.hide_added = true;
    qbit.add_torrent(&add("https://x.test/a", "/dl")).unwrap();
    assert!(qbit.torrent_for("https://x.test/a").is_none());
    assert!(qbit.add_torrent(&add("https://x.test/a", "/dl")).is_err());
    qbit.reveal();
    assert!(qbit.torrent_for("https://x.test/a").is_some());
}

#[test]
fn a_replaced_method_answers_with_the_queued_error_and_is_not_logged() {
    let mut qbit = FakeQbit::new(FakeClock::default());
    qbit.overrides
        .entry("ensure_category".to_owned())
        .or_default()
        .push_back(QbitError::Conflict("no".to_owned()));
    assert!(qbit.ensure_category("Seasonal").is_err());
    assert!(qbit.calls.is_empty());
    qbit.ensure_category("Seasonal").unwrap();
    assert_eq!(qbit.calls.len(), 1);
    assert!(qbit.categories.contains("Seasonal"));
}

#[test]
fn an_unavailable_qbittorrent_logs_the_call_and_fails_it() {
    let mut qbit = FakeQbit::new(FakeClock::default());
    qbit.unavailable = Some(QbitError::Unavailable("down".to_owned()));
    assert!(qbit.sync(false).is_err());
    assert_eq!(qbit.calls, [QbitCall::Sync]);
}

#[test]
fn tags_move_with_the_torrents_that_carry_them() {
    let mut qbit = FakeQbit::new(FakeClock::default());
    qbit.add_torrent(&add("https://x.test/a", "/dl")).unwrap();
    let hash = hash_for("https://x.test/a");
    qbit.add_tags(
        std::slice::from_ref(&hash),
        &["one".to_owned(), "two".to_owned()],
    )
    .unwrap();
    qbit.remove_tags(std::slice::from_ref(&hash), &["one".to_owned()])
        .unwrap();
    assert_eq!(qbit.tags().unwrap(), ["one", "two"]);
    qbit.delete_tags(&["two".to_owned()]).unwrap();
    assert!(qbit.truth[&hash].tags.is_empty());
}

#[test]
fn a_grouped_search_finds_what_any_of_its_names_would() {
    let mut feeds = FakeFeeds::default();
    feeds.searchable.insert(
        "https://site/rss".to_owned(),
        vec![
            article("[Grp] Frieren - 01", None, None, None, None),
            article("[Grp] Bleach - 01", None, None, None, None),
            article("[Grp] Other - 01", None, None, None, None),
        ],
    );
    let found = feeds
        .search("https://site/rss", "((frieren)|(bleach))")
        .unwrap()
        .unwrap();
    assert_eq!(found.len(), 2);
    assert!(
        feeds
            .search("https://elsewhere/rss", "x")
            .unwrap()
            .is_none()
    );
    assert_eq!(feeds.searched().len(), 2);
}

#[test]
fn the_upcoming_schedule_starts_at_the_given_time() {
    let mut anilist = FakeAniList::new(FakeClock::default());
    anilist.set_schedule(1, START, 3, SignedDuration::from_hours(24));
    let since = START.checked_add(SignedDuration::from_hours(24)).unwrap();
    let upcoming = anilist
        .fetch_upcoming_airing_schedules(&[1, 2], since)
        .unwrap();
    assert_eq!(upcoming[&1].len(), 2);
    assert!(upcoming[&2].is_empty());
}

#[test]
fn download_tags_count_up_in_hex() {
    let mut tags = CountedTags::default();
    assert_eq!(
        tags.next_tag(),
        "kisetsu-op-00000000000000000000000000000001"
    );
    assert_eq!(
        tags.next_tag(),
        "kisetsu-op-00000000000000000000000000000002"
    );
}
