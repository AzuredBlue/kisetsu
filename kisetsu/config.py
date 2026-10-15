import os
from pathlib import Path

APP_NAME = "kisetsu"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
# `kisetsu --sandbox` points this at a throwaway database.
DB_PATH = Path(os.environ["KISETSU_DB_PATH"]) if os.environ.get("KISETSU_DB_PATH") else CONFIG_DIR / "anime.db"
# A sandbox instance runs no background work and refuses anything that could change the real qBittorrent.
SANDBOX = os.environ.get("KISETSU_SANDBOX") == "1"

DEFAULT_QBIT_HOST = "http://localhost:8080"
DEFAULT_QBIT_USERNAME = "admin"
DEFAULT_QBIT_PASSWORD = "adminadmin"
DEFAULT_BASE_DIR = ""
DEFAULT_CATEGORY = ""
DEFAULT_SEED_RATIO = 1.0
DEFAULT_REFRESH_INTERVAL_MINUTES = 360
DEFAULT_STALL_WAIT_HOURS = 24

DEFAULT_DOWNLOAD_MODE = "direct"
# Stored for old settings files; nothing reads it any more (the whole season is eligible).
DEFAULT_BACKFILL_WINDOW_DAYS = 14
# How long after an air time the scheduler keeps polling at the fast hunting interval.
HUNTING_RECENT_DAYS = 14
# How long the newest aired episode may stay unfound before it counts as missed.
MISSED_AFTER_HOURS = 24
DEFAULT_EARLY_AIR_TOLERANCE_HOURS = 6
# How often the direct engine refreshes the feeds of the shows it is hunting.
DEFAULT_HUNT_INTERVAL_MINUTES = 5
MIN_HUNT_INTERVAL_MINUTES = 2
MAX_HUNT_INTERVAL_MINUTES = 60

RULE_OBSERVER_INTERVAL_SECONDS = 300

FUZZY_MATCH_THRESHOLD = 85.0
