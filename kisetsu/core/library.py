"""The release files already on disk in a show's save folder.

A library carried over from another machine (a dual-boot PC, a restored backup)
names the feed the show was downloaded from, so discovery reads it before guessing.
"""
import logging
import os
import time
from typing import Dict, List

from kisetsu.core.matching import match_release_to_show, prepare_aliases
from kisetsu.core.rules import build_regex_pattern, effective_title, resolve_save_path
from kisetsu.db.models import Monitored, Settings

logger = logging.getLogger("kisetsu.core.library")

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi")

# How long a grab waits for the save folder to be matched to a feed, so a search
# that never completes cannot keep a show from downloading.
HOLD_SECONDS = 3600
_held: Dict[int, float] = {}


def saved_release_names(show: Monitored, settings: Settings) -> List[str]:
    """Names of the show's video files in its save folder, top level and one folder down."""
    path = resolve_save_path(
        settings.base_dir,
        effective_title(show, settings.title_language),
        show.save_folder,
    )
    if not path or not os.path.isdir(path):
        return []
    names: List[str] = []
    try:
        for entry in os.scandir(path):
            if entry.is_file():
                names.append(entry.name)
            elif entry.is_dir():
                names.extend(inner.name for inner in os.scandir(entry.path) if inner.is_file())
    except OSError as e:
        logger.debug(f"Could not read the save folder of '{show.display_name}': {e}")
        return []
    aliases = show.effective_aliases
    prepared = prepare_aliases(aliases)
    test_pattern = build_regex_pattern(aliases)
    parsed_cache: Dict[str, dict] = {}
    return [
        name for name in sorted(names)
        if name.lower().endswith(VIDEO_EXTENSIONS)
        and match_release_to_show(
            name,
            aliases,
            test_pattern=test_pattern,
            prepared_aliases=prepared,
            parsed_cache=parsed_cache,
            ignore_arc_marker=False,
        )[0]
    ]


def hold(show_id: int) -> bool:
    """Start holding a show's grabs; True when it was not held already."""
    new = show_id not in _held
    _held.setdefault(show_id, time.monotonic())
    return new


def release(show_id: int) -> None:
    _held.pop(show_id, None)


def is_held(show_id: int) -> bool:
    started = _held.get(show_id)
    return started is not None and time.monotonic() - started < HOLD_SECONDS
