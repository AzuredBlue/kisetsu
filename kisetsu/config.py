from pathlib import Path

APP_NAME = "kisetsu"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
DB_PATH = CONFIG_DIR / "anime.db"

DEFAULT_QBIT_HOST = "http://localhost:8080"
DEFAULT_QBIT_USERNAME = "admin"
DEFAULT_QBIT_PASSWORD = "adminadmin"
DEFAULT_BASE_DIR = ""
DEFAULT_CATEGORY = ""
DEFAULT_SEED_RATIO = 1.0
DEFAULT_REFRESH_INTERVAL_MINUTES = 360
DEFAULT_STALL_WAIT_HOURS = 24

DEFAULT_DOWNLOAD_MODE = "rules"
# Stored for old settings files; nothing reads it any more (the whole season is eligible).
DEFAULT_BACKFILL_WINDOW_DAYS = 14
# How long after an air time the scheduler keeps polling at the fast hunting interval.
HUNTING_RECENT_DAYS = 14
DEFAULT_EARLY_AIR_TOLERANCE_HOURS = 6

RULE_OBSERVER_INTERVAL_SECONDS = 300

FUZZY_MATCH_THRESHOLD = 85.0
