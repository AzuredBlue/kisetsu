from types import SimpleNamespace

import pytest
import qbittorrentapi
import requests

from kisetsu.clients.qbittorrent import (
    Qbit,
    QbitAuthError,
    QbitDuplicate,
    QbitError,
    QbitReadOnly,
    QbitRejected,
    QbitUnavailable,
)


class Api:
    """The slice of qbittorrentapi.Client that Qbit calls, scripted per test."""

    def __init__(self):
        self.calls = []
        self.maindata = []
        self.rss_tree = {}
        self.prefs = {}
        self.add_result = "Ok."
        self.raise_on = {}
        self.app = SimpleNamespace(version="v5.3.0", web_api_version="2.16.2")
        self.torrent_categories = SimpleNamespace(categories={"Seasonal": {}}, create_category=lambda name: self.calls.append(("create_category", name)))

    def auth_log_in(self):
        self.calls.append(("login",))

    def _do(self, name, **kwargs):
        self.calls.append((name, kwargs))
        error = self.raise_on.get(name)
        if error:
            raise error

    def sync_maindata(self, rid=0):
        self._do("sync_maindata", rid=rid)
        return self.maindata.pop(0)

    def torrents_add(self, **kwargs):
        self._do("torrents_add", **kwargs)
        return self.add_result

    def torrents_stop(self, torrent_hashes=None):
        self._do("torrents_stop", hashes=torrent_hashes)

    def torrents_start(self, torrent_hashes=None):
        self._do("torrents_start", hashes=torrent_hashes)

    def torrents_delete(self, torrent_hashes=None, delete_files=False):
        self._do("torrents_delete", hashes=torrent_hashes, delete_files=delete_files)

    def torrents_tags(self):
        return ["a", "b"]

    def rss_items(self, include_feed_data=False):
        self._do("rss_items", include_feed_data=include_feed_data)
        return self.rss_tree

    def app_preferences(self):
        return self.prefs

    def rss_add_feed(self, url, item_path):
        self._do("rss_add_feed", url=url, path=item_path)

    def rss_remove_item(self, item_path):
        self._do("rss_remove_item", path=item_path)

    def rss_set_feed_url(self, url, item_path):
        self._do("rss_set_feed_url", url=url, path=item_path)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(api=None, **kwargs):
    api = api or Api()
    clock = Clock()
    factory_calls = []

    def factory():
        factory_calls.append(1)
        return api

    return Qbit("http://q", client_factory=factory, clock=clock, **kwargs), api, clock, factory_calls


def http_error(cls, status):
    error = cls("boom")
    error.response = SimpleNamespace(status_code=status)
    return error


def torrent(name="Show - 01.mkv", state="downloading", progress=0.5, tags="", **extra):
    values = dict(
        name=name, state=state, progress=progress, amount_left=100, size=200, save_path="/d", content_path="/d/x",
        category="Seasonal", tags=tags, added_on=1_700_000_000, completion_on=-1, availability=2.0, dlspeed=5,
    )
    values.update(extra)
    return values


# Connection and errors

def test_a_failed_login_is_an_auth_error_and_is_not_retried_for_a_while():
    api = Api()
    api.auth_log_in = lambda: (_ for _ in ()).throw(qbittorrentapi.LoginFailed("bad"))
    qbit, _, clock, factory_calls = make(api)
    with pytest.raises(QbitAuthError):
        qbit.versions()
    with pytest.raises(QbitAuthError, match="not retrying"):
        qbit.versions()
    assert len(factory_calls) == 1
    clock.now += 301
    with pytest.raises(QbitAuthError):
        qbit.versions()
    assert len(factory_calls) == 2


@pytest.mark.parametrize("cls,status", [(qbittorrentapi.Forbidden403Error, 403), (qbittorrentapi.Unauthorized401Error, 401)])
def test_a_forbidden_or_unauthorized_login_is_an_auth_error(cls, status):
    api = Api()
    api.auth_log_in = lambda: (_ for _ in ()).throw(http_error(cls, status))
    qbit, _, _, factory_calls = make(api)
    with pytest.raises(QbitAuthError):
        qbit.versions()
    with pytest.raises(QbitAuthError, match="not retrying"):
        qbit.versions()
    assert len(factory_calls) == 1


def test_an_unexpected_error_while_asking_the_version_is_a_plain_error_with_its_message():
    api = Api()
    api.app = None
    qbit, *_ = make(api)
    with pytest.raises(QbitError) as caught:
        qbit.versions()
    assert not isinstance(caught.value, QbitUnavailable) and "NoneType" in str(caught.value)


def test_an_unreachable_qbit_backs_off_with_growing_steps_then_recovers():
    api = Api()
    api.raise_on["sync_maindata"] = requests.exceptions.ConnectionError("refused")
    qbit, _, clock, factory_calls = make(api)
    for expected_wait in (30, 60, 120, 300, 300):
        with pytest.raises(QbitUnavailable):
            qbit.sync()
        with pytest.raises(QbitUnavailable, match="not retrying"):
            qbit.sync()
        clock.now += expected_wait - 1
        with pytest.raises(QbitUnavailable, match="not retrying"):
            qbit.sync()
        clock.now += 2
    del api.raise_on["sync_maindata"]
    api.maindata.append({"rid": 1, "full_update": True, "torrents": {}})
    assert qbit.sync().full
    api.raise_on["sync_maindata"] = requests.exceptions.ConnectionError("refused")
    api.maindata.append({"rid": 2})
    with pytest.raises(QbitUnavailable):
        qbit.sync()
    clock.now += 31
    del api.raise_on["sync_maindata"]
    assert qbit.sync() is not None


def test_an_http_answer_from_qbit_does_not_start_a_backoff():
    api = Api()
    api.raise_on["rss_items"] = http_error(qbittorrentapi.NotFound404Error, 404)
    qbit, _, _, factory_calls = make(api)
    with pytest.raises(QbitError) as caught:
        qbit.rss_feeds()
    assert not isinstance(caught.value, QbitUnavailable)
    del api.raise_on["rss_items"]
    assert qbit.rss_feeds() == []
    assert len(factory_calls) == 1


def test_a_gateway_error_counts_as_unavailable():
    api = Api()
    api.raise_on["rss_items"] = http_error(qbittorrentapi.HTTPError, 503)
    qbit, *_ = make(api)
    with pytest.raises(QbitUnavailable):
        qbit.rss_feeds()


def test_the_versions_call_reports_both_versions():
    qbit, *_ = make()
    assert qbit.versions() == {"app_version": "v5.3.0", "api_version": "2.16.2"}


def test_a_read_only_client_reads_but_never_writes():
    qbit, api, *_ = make(read_only=True)
    api.maindata.append({"rid": 1, "full_update": True, "torrents": {}})
    qbit.sync()
    assert qbit.rss_feeds() == []
    for write in (
        lambda: qbit.add_torrent("magnet:x"),
        lambda: qbit.pause(["h"]),
        lambda: qbit.delete(["h"], delete_files=True),
        lambda: qbit.rss_remove_rule("r"),
        lambda: qbit.ensure_category("Seasonal2"),
    ):
        with pytest.raises(QbitReadOnly):
            write()


# Adding torrents

def test_add_sends_the_explicit_path_category_tags_and_limits():
    qbit, api, *_ = make()
    qbit.add_torrent("magnet:x", save_path="/data/Show", category="Seasonal", tags=["kisetsu-managed", "op1"], paused=True, ratio_limit=1.5)
    kwargs = api.calls[-1][1]
    assert kwargs["urls"] == "magnet:x"
    assert kwargs["save_path"] == "/data/Show"
    assert kwargs["category"] == "Seasonal"
    assert kwargs["tags"] == "kisetsu-managed,op1"
    assert kwargs["is_paused"] is True
    assert kwargs["ratio_limit"] == 1.5
    assert kwargs["use_auto_torrent_management"] is False


def test_add_stops_the_torrent_once_the_share_limit_is_reached():
    qbit, api, *_ = make()
    qbit.add_torrent("magnet:x", ratio_limit=1.0)
    assert api.calls[-1][1]["share_limit_action"] == "Stop"


def test_a_zero_ratio_limit_is_sent_as_zero_and_no_limit_as_none():
    qbit, api, *_ = make()
    qbit.add_torrent("magnet:x", ratio_limit=0)
    assert api.calls[-1][1]["ratio_limit"] == 0 and api.calls[-1][1]["ratio_limit"] is not None
    qbit.add_torrent("magnet:y")
    assert api.calls[-1][1]["ratio_limit"] is None


def test_adding_a_torrent_qbit_already_has_is_a_duplicate():
    qbit, api, *_ = make()
    api.raise_on["torrents_add"] = qbittorrentapi.Conflict409Error("exists")
    with pytest.raises(QbitDuplicate):
        qbit.add_torrent("magnet:x")


@pytest.mark.parametrize("result", ["Fails.", {"failure_count": 1, "success_count": 0}])
def test_a_refused_torrent_is_rejected(result):
    qbit, api, *_ = make()
    api.add_result = result
    with pytest.raises(QbitRejected):
        qbit.add_torrent("magnet:x")


@pytest.mark.parametrize("result", ["Ok.", None, {"failure_count": 0, "success_count": 1}])
def test_an_accepted_torrent_is_not_an_error(result):
    qbit, api, *_ = make()
    api.add_result = result
    qbit.add_torrent("magnet:x")


# Syncing torrents

def test_a_full_sync_lists_the_torrents_with_parsed_fields():
    qbit, api, *_ = make()
    api.maindata.append({"rid": 5, "full_update": True, "torrents": {"ABC": torrent(tags="kisetsu-managed, op1")}})
    result = qbit.sync()
    assert result.full and result.changed == {"abc"}
    t = qbit.torrents["abc"]
    assert t.name == "Show - 01.mkv"
    assert t.tags == frozenset({"kisetsu-managed", "op1"})
    assert t.added_on.year == 2023
    assert t.completion_on is None
    assert not t.finished and not t.errored
    assert qbit.find_by_tag("op1") is t
    assert qbit.find_by_tag("nope") is None


def test_a_delta_merges_changed_fields_and_asks_from_the_last_rid():
    qbit, api, *_ = make()
    api.maindata.append({"rid": 5, "full_update": True, "torrents": {"abc": torrent()}})
    qbit.sync()
    api.maindata.append({"rid": 6, "torrents": {"abc": {"progress": 1.0, "amount_left": 0, "state": "stalledUP"}}})
    result = qbit.sync()
    assert not result.full and result.changed == {"abc"}
    assert api.calls[-1] == ("sync_maindata", {"rid": 5})
    t = qbit.torrents["abc"]
    assert t.finished and t.name == "Show - 01.mkv"


def test_an_empty_delta_changes_nothing_and_is_falsy():
    qbit, api, *_ = make()
    api.maindata.append({"rid": 5, "full_update": True, "torrents": {"abc": torrent()}})
    qbit.sync()
    api.maindata.append({"rid": 5})
    assert not qbit.sync()


def test_removed_torrents_leave_the_view():
    qbit, api, *_ = make()
    api.maindata.append({"rid": 1, "full_update": True, "torrents": {"abc": torrent(), "def": torrent()}})
    qbit.sync()
    api.maindata.append({"rid": 2, "torrents_removed": ["ABC"]})
    result = qbit.sync()
    assert result.removed == {"abc"}
    assert set(qbit.torrents) == {"def"}


def test_a_new_full_update_drops_torrents_that_vanished_while_it_was_away():
    qbit, api, *_ = make()
    api.maindata.append({"rid": 1, "full_update": True, "torrents": {"abc": torrent(), "def": torrent()}})
    qbit.sync()
    api.maindata.append({"rid": 1, "full_update": True, "torrents": {"def": torrent()}})
    result = qbit.sync()
    assert result.removed == {"abc"}
    assert set(qbit.torrents) == {"def"}


def test_a_forced_full_sync_asks_from_the_start():
    qbit, api, *_ = make()
    api.maindata.append({"rid": 9, "full_update": True, "torrents": {}})
    qbit.sync()
    api.maindata.append({"rid": 10, "full_update": True, "torrents": {}})
    qbit.sync(full=True)
    assert api.calls[-1] == ("sync_maindata", {"rid": 0})


def test_only_the_fields_in_use_are_kept():
    qbit, api, *_ = make()
    api.maindata.append({"rid": 1, "full_update": True, "torrents": {"abc": torrent()}})
    qbit.sync()
    assert set(qbit._raw["abc"]) <= {
        "name", "state", "progress", "amount_left", "size", "save_path", "content_path",
        "category", "tags", "added_on", "completion_on",
    }


def test_error_and_checking_states_are_recognised():
    qbit, api, *_ = make()
    api.maindata.append({"rid": 1, "full_update": True, "torrents": {
        "a": torrent(state="missingFiles"), "b": torrent(state="checkingUP"), "c": torrent(state="stoppedUP", progress=1.0, amount_left=0),
    }})
    qbit.sync()
    assert qbit.torrents["a"].errored
    assert qbit.torrents["b"].checking
    assert qbit.torrents["c"].finished and not qbit.torrents["c"].errored


# Torrent actions

def test_actions_pass_the_hashes_and_skip_empty_lists():
    qbit, api, *_ = make()
    qbit.pause(["a"])
    qbit.resume(["a", "b"])
    qbit.delete(["a"], delete_files=False)
    qbit.pause([])
    qbit.delete([], delete_files=True)
    names = [call[0] for call in api.calls if call[0] != "login"]
    assert names == ["torrents_stop", "torrents_start", "torrents_delete"]
    assert api.calls[-1][1] == {"hashes": ["a"], "delete_files": False}


def test_ensure_category_creates_only_a_missing_one():
    qbit, api, *_ = make()
    qbit.ensure_category("Seasonal")
    qbit.ensure_category("New")
    qbit.ensure_category("  ")
    assert [c for c in api.calls if c[0] == "create_category"] == [("create_category", "New")]


# RSS

TREE = {
    "Plain": {"uid": "1", "url": "https://a/rss"},
    "Group": {"Nested": {"uid": "2", "url": "https://b/rss"}},
}


def test_rss_feeds_walk_folders_with_backslash_paths():
    qbit, api, *_ = make()
    api.rss_tree = TREE
    feeds = {feed.url: feed.path for feed in qbit.rss_feeds()}
    assert feeds == {"https://a/rss": "Plain", "https://b/rss": "Group\\Nested"}
    assert api.calls[-1][1]["include_feed_data"] is False
