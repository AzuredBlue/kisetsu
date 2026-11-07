import pytest

from kisetsu.clients.qbittorrent import QbitDuplicate
from kisetsu.clients.sandbox import SandboxQbit, SandboxState
from kisetsu.db.schema import DownloadState, EpisodeStatus
from tests.scenarios.test_downloads import V2, grab, settle
from tests.scenarios.test_downloads import setup as setup_show

REAL = "a" * 40


class ReadOnlyApi:
    """The real qBittorrent: it holds one torrent and fails the test on any write."""

    READS = {"auth_log_in", "sync_maindata", "torrents_tags", "app_default_save_path"}

    def __init__(self):
        self.called = []

    def auth_log_in(self):
        pass

    def sync_maindata(self, rid=0):
        return {"rid": 1, "full_update": True, "torrents": {REAL: {"name": "Real Show - 01", "state": "uploading", "progress": 1.0, "amount_left": 0, "tags": "mine", "save_path": "/real"}}}

    def torrents_tags(self):
        return ["mine"]

    def app_default_save_path(self):
        return "/downloads"

    def __getattr__(self, name):
        self.called.append(name)
        raise AssertionError(f"The sandbox reached qBittorrent with '{name}'.")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def client(state=None, api=None):
    api = api or ReadOnlyApi()
    state = state or SandboxState(clock=Clock(), download_seconds=60)
    return SandboxQbit("http://q", state=state, client_factory=lambda: api), api


def test_no_write_reaches_the_real_qbittorrent():
    qbit, api = client()
    qbit.sync()
    qbit.add_torrent("https://x/1.torrent", save_path="/anime", category="Seasonal", tags=["t"], paused=True, ratio_limit=0)
    qbit.sync()
    fake = next(h for h in qbit.torrents if h != REAL)
    for hashes in ([REAL], [fake]):
        qbit.pause(hashes)
        qbit.resume(hashes)
        qbit.recheck(hashes)
        qbit.set_location(hashes, "/elsewhere")
        qbit.add_tags(hashes, ["x"])
        qbit.remove_tags(hashes, ["x"])
        qbit.delete(hashes, delete_files=True)
    qbit.delete_tags(["mine"])
    qbit.ensure_category("Seasonal")
    qbit.sync()
    assert api.called == []
    assert qbit.torrents == {}


def test_a_pretend_download_runs_then_finishes_and_seeds_or_stops():
    qbit, _ = client()
    qbit.add_torrent("https://x/1.torrent", save_path="/anime", tags=["op"])
    qbit.add_torrent("https://x/2.torrent", save_path="/anime", ratio_limit=0)
    qbit.sync()
    one = qbit.find_by_tag("op")
    assert one.state == "downloading" and one.progress == 0 and not one.finished
    qbit.state.clock.now += 30
    qbit.sync()
    assert qbit.torrents[one.hash].progress == pytest.approx(0.5)
    qbit.state.clock.now += 30
    changed = qbit.sync().changed
    assert one.hash in changed
    states = sorted(t.state for h, t in qbit.torrents.items() if h != REAL)
    assert states == ["stoppedUP", "uploading"]
    assert all(t.finished and t.completion_on for h, t in qbit.torrents.items() if h != REAL)


def test_a_paused_add_waits_until_resumed_and_a_duplicate_is_refused():
    qbit, _ = client()
    qbit.add_torrent("https://x/1.torrent", paused=True)
    with pytest.raises(QbitDuplicate):
        qbit.add_torrent("https://x/1.torrent")
    qbit.sync()
    (h,) = [h for h in qbit.torrents if h != REAL]
    qbit.state.clock.now += 100
    qbit.sync()
    assert qbit.torrents[h].state == "stoppedDL" and qbit.torrents[h].progress == 0
    qbit.resume([h])
    qbit.state.clock.now += 6
    qbit.sync()
    assert qbit.torrents[h].state == "downloading" and qbit.torrents[h].progress == pytest.approx(0.1)


def test_changes_to_real_torrents_are_only_pretended_and_survive_a_new_client():
    qbit, _ = client()
    qbit.sync()
    qbit.add_tags([REAL], ["kisetsu"])
    qbit.set_location([REAL], "/moved")
    qbit.sync()
    assert qbit.find_by_tag("kisetsu").save_path == "/moved"
    assert "kisetsu" in qbit.tags()
    qbit.add_torrent("https://x/1.torrent")
    # The engine builds a new client when the qBittorrent settings change.
    again, _ = client(state=qbit.state)
    again.sync()
    assert len(again.torrents) == 2
    result = (again.delete([REAL], delete_files=True), again.sync())[1]
    assert result.removed == {REAL} and REAL not in again.torrents


def test_a_download_and_its_v2_play_out_through_the_sandbox(world):
    state = SandboxState(clock=lambda: world.clock().timestamp(), download_seconds=60)
    world.qbit, _ = client(state=state)
    feed, show, ep = setup_show(world)
    v1, row1 = grab(world, show, feed, ep)
    assert v1.state == DownloadState.DOWNLOADING
    settle(world, minutes=2)
    assert v1.state == DownloadState.DONE and world.episode(show, 1).status == EpisodeStatus.COMPLETED
    v2, _ = grab(world, show, feed, world.episode(show, 1), V2)
    assert v2.state == DownloadState.VERIFYING
    settle(world)
    settle(world, minutes=2)
    settle(world)
    world.session.expire_all()
    assert (v1.state, v2.state) == (DownloadState.SUPERSEDED, DownloadState.DONE)
    assert v1.info_hash not in world.qbit.torrents


def test_an_add_of_a_release_qbittorrent_already_holds_is_refused_by_its_known_hash(caplog):
    state = SandboxState(clock=Clock(), release_for=lambda url: ("Real Show - 01", REAL.upper()))
    qbit, api = client(state=state)
    qbit.sync()
    with caplog.at_level("INFO", logger="kisetsu.sandbox"), pytest.raises(QbitDuplicate):
        qbit.add_torrent("https://x/1.torrent")
    assert state.fakes == {} and api.called == []
    assert "[sandbox] Would add 'Real Show - 01', but qBittorrent already has it" in caplog.text
