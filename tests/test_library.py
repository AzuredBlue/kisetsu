from kisetsu.core import library
from kisetsu.db.models import Monitored, Settings


def _show():
    return Monitored(id=1, anilist_id=1, display_name="Sousou no Frieren", aliases_json='["Sousou no Frieren"]')


def _settings(tmp_path):
    return Settings(id=1, base_dir=str(tmp_path / "{name}"), download_mode="direct")


def test_saved_release_names_lists_this_shows_video_files(tmp_path):
    folder = tmp_path / "Sousou no Frieren"
    (folder / "Batch").mkdir(parents=True)
    (folder / "[SubsPlease] Sousou no Frieren - 07 (1080p) [ABCD1234].mkv").write_text("x")
    (folder / "Batch" / "[SubsPlease] Sousou no Frieren - 08 (1080p) [EF012345].mkv").write_text("x")
    (folder / "[SubsPlease] Sousou no Frieren - 09 (1080p) [EF012346].srt").write_text("x")
    (folder / "[SubsPlease] Another Show - 01 (1080p) [EF012347].mkv").write_text("x")

    names = library.saved_release_names(_show(), _settings(tmp_path))

    assert names == [
        "[SubsPlease] Sousou no Frieren - 07 (1080p) [ABCD1234].mkv",
        "[SubsPlease] Sousou no Frieren - 08 (1080p) [EF012345].mkv",
    ]


def test_saved_release_names_is_empty_without_a_folder(tmp_path):
    assert library.saved_release_names(_show(), _settings(tmp_path)) == []
    assert library.saved_release_names(_show(), Settings(id=1, base_dir="", download_mode="direct")) == []


def test_a_hold_lapses_after_its_time(monkeypatch):
    library._held.clear()
    assert library.hold(5) is True
    assert library.hold(5) is False
    assert library.is_held(5)
    library._held[5] -= library.HOLD_SECONDS + 1
    assert not library.is_held(5)
    library.release(5)
    assert library.hold(5) is True
    library.release(5)
