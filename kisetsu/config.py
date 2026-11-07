import os
import sys
from pathlib import Path

APP_NAME = "kisetsu"


def _config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    return Path.home() / ".config" / APP_NAME


CONFIG_DIR = _config_dir()
# `kisetsu --sandbox` points this at a throwaway database.
DB_PATH = Path(os.environ["KISETSU_DB_PATH"]) if os.environ.get("KISETSU_DB_PATH") else CONFIG_DIR / "kisetsu.db"
# The database of the releases before the redesign; only `kisetsu import-legacy` reads it.
LEGACY_DB_PATH = CONFIG_DIR / "anime.db"
# A sandbox instance reads qBittorrent but only logs and pretends the changes it would make (clients/sandbox.py).
SANDBOX = os.environ.get("KISETSU_SANDBOX") == "1"

DEFAULT_QBIT_HOST = "http://localhost:8080"
DEFAULT_QBIT_USERNAME = "admin"
DEFAULT_QBIT_PASSWORD = "adminadmin"
DEFAULT_BASE_DIR = ""
DEFAULT_CATEGORY = ""
DEFAULT_SEED_RATIO = 1.0
# The preferred quality: taken when a feed has it, else the closest resolution above it; lower ones are never taken.
# Releases that name no resolution are taken last. (Stored as Settings.min_resolution, as it is also the floor.)
DEFAULT_MIN_RESOLUTION = 1080
MIN_RESOLUTIONS = (480, 720, 1080)
DEFAULT_ANILIST_SYNC_MINUTES = 360
# How often every feed is read for new releases.
DEFAULT_FEED_REFRESH_MINUTES = 15
MIN_FEED_REFRESH_MINUTES = 5
MAX_FEED_REFRESH_MINUTES = 240

# How long after an air time the scheduler keeps polling at the fast hunting interval.
HUNTING_RECENT_DAYS = 14
DEFAULT_EARLY_AIR_TOLERANCE_HOURS = 6
# How often the direct engine refreshes the feeds of the shows it is hunting.
DEFAULT_HUNT_INTERVAL_MINUTES = 5
MIN_HUNT_INTERVAL_MINUTES = 2
MAX_HUNT_INTERVAL_MINUTES = 60

FUZZY_MATCH_THRESHOLD = 85.0
