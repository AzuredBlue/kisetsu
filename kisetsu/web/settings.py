"""The user's settings: one validated shape for the Settings page and the first-run wizard."""

import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError
from kisetsu.config import MAX_FEED_REFRESH_MINUTES, MAX_HUNT_INTERVAL_MINUTES, MIN_FEED_REFRESH_MINUTES, MIN_HUNT_INTERVAL_MINUTES
from kisetsu.db.schema import Settings, Show
from kisetsu.domain.patterns import compress_home_path, resolve_save_path
from kisetsu.services import library
from kisetsu.services.context import Context

ACCENT_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TINTS = ("off", "subtle", "full")
LANGUAGES = ("english", "romaji")
# The wizard may only set these; the rest of the page opens once setup is finished.
SETUP_FIELDS = {
    "qbit_host", "qbit_username", "qbit_password", "base_dir", "default_category",
    "default_seed_ratio", "title_language", "accent_color", "desktop_notifications",
}


class SettingsPatch(BaseModel):
    """Any subset of the settings. A field left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    qbit_host: Optional[str] = None
    qbit_username: Optional[str] = None
    qbit_password: Optional[str] = None
    base_dir: Optional[str] = None
    default_category: Optional[str] = None
    default_seed_ratio: Optional[float] = Field(default=None, ge=0)
    anilist_username: Optional[str] = None
    anilist_sync_minutes: Optional[int] = Field(default=None, ge=1, le=10080)
    feed_refresh_minutes: Optional[int] = Field(default=None, ge=MIN_FEED_REFRESH_MINUTES, le=MAX_FEED_REFRESH_MINUTES)
    hunt_interval_minutes: Optional[int] = Field(default=None, ge=MIN_HUNT_INTERVAL_MINUTES, le=MAX_HUNT_INTERVAL_MINUTES)
    early_air_tolerance_hours: Optional[int] = Field(default=None, ge=0, le=168)
    title_language: Optional[str] = None
    move_existing: Optional[bool] = None
    accent_color: Optional[str] = None
    accent_tint: Optional[str] = None
    desktop_notifications: Optional[bool] = None

    @field_validator("title_language", "accent_tint")
    @classmethod
    def _lowered(cls, value: Optional[str], info) -> Optional[str]:
        if value is None:
            return None
        value = value.strip().lower()
        allowed = LANGUAGES if info.field_name == "title_language" else TINTS
        if value not in allowed:
            raise ValueError(f"must be {' or '.join(allowed)}")
        return value

    @field_validator("accent_color")
    @classmethod
    def _colour(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not ACCENT_COLOR_RE.match(value):
            raise ValueError("must be a #rrggbb colour")
        return value.lower()

    def given(self) -> Dict[str, Any]:
        return self.model_dump(exclude_unset=True, exclude_none=True)


class SettingsError(ValueError):
    """The patch is well formed but cannot be applied; the message says why."""


def normalize_base_dir(raw: str) -> str:
    """The stored form of a base directory: it always ends in the {name} placeholder unless blank."""
    value = raw.strip()
    if value and "{name}" not in value:
        value = value.rstrip("/\\") + "/{name}"
    return value


def settings_view(settings: Settings) -> Dict[str, Any]:
    base_dir = settings.base_dir
    if base_dir and "{name}" not in base_dir:
        base_dir = f"{base_dir.rstrip('/')}/{{name}}"
    return {
        "qbit_host": settings.qbit_host,
        "qbit_username": settings.qbit_username,
        "qbit_password_set": bool(settings.qbit_password),
        "base_dir": compress_home_path(base_dir),
        "default_category": settings.default_category,
        "default_seed_ratio": settings.default_seed_ratio,
        "anilist_username": settings.anilist_username,
        "anilist_sync_minutes": settings.anilist_sync_minutes,
        "feed_refresh_minutes": settings.feed_refresh_minutes,
        "hunt_interval_minutes": settings.hunt_interval_minutes,
        "early_air_tolerance_hours": settings.early_air_tolerance_hours,
        "title_language": settings.title_language,
        "accent_color": settings.accent_color,
        "accent_tint": settings.accent_tint,
        "desktop_notifications": settings.desktop_notifications,
    }


def check_password_travels_with_host(settings: Settings, host: str, password: Optional[str]) -> None:
    """The stored password may only go to the stored host, so a new host needs it typed again."""
    if host.rstrip("/") != (settings.qbit_host or "").rstrip("/") and settings.qbit_password and not password:
        raise SettingsError("Enter the password to use a different qBittorrent host.")


def apply_settings(ctx: Context, patch: SettingsPatch) -> List[str]:
    """Store the patch; returns the log lines of a folder move the title language asked for."""
    s = ctx.settings
    given = patch.given()
    plan, unreachable = patch_moves(ctx, given)
    if "qbit_host" in given:
        host = given["qbit_host"].strip()
        check_password_travels_with_host(s, host, given.get("qbit_password"))
        s.qbit_host = host
    if "qbit_username" in given:
        s.qbit_username = given["qbit_username"].strip()
    if given.get("qbit_password"):
        s.qbit_password = given["qbit_password"]
    if "base_dir" in given:
        s.base_dir = normalize_base_dir(given["base_dir"])
    if "default_category" in given:
        s.default_category = given["default_category"].strip()
    for name in ("default_seed_ratio", "anilist_sync_minutes", "feed_refresh_minutes", "hunt_interval_minutes", "early_air_tolerance_hours",
                 "accent_color", "accent_tint", "desktop_notifications"):
        if name in given:
            setattr(s, name, given[name])
    if "anilist_username" in given:
        s.anilist_username = given["anilist_username"].strip()
    language = given.get("title_language")
    if language and language != s.title_language:
        s.title_language = language
    ctx.session.add(s)
    ctx.emit("settings", "shows")
    if unreachable:
        return ["qBittorrent could not be reached, so the downloads already made stayed in their old folders."]
    if not plan:
        return []
    ctx.checkpoint()
    logs, failed = library.relocate_torrents(ctx, plan)
    if failed:
        logs.append("Some downloads stayed in their old folders: " + ", ".join(failed) + ".")
    return logs


def move_plan(ctx: Context, language: str) -> List[Dict[str, Any]]:
    """The folder renames a language switch causes, refusing while qBittorrent is still moving some."""
    plan = library.plan_language_move(ctx, language)
    moving = sum(item["moving"] for item in plan)
    if moving:
        raise SettingsError(f"{moving} downloads are still moving to their folders. Try again in a moment.")
    return plan


def patch_moves(ctx: Context, given: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """The folder moves this patch causes, and whether qBittorrent could not be asked.

    A new base folder always moves the downloads; a new title language only when asked to.
    Planned before anything changes, since the plan reads where the downloads are now, and
    refused while qBittorrent is still moving some.
    """
    s = ctx.settings
    base_dir = normalize_base_dir(given["base_dir"]) if "base_dir" in given else s.base_dir
    language = given.get("title_language")
    rename = bool(language and language != s.title_language and given.get("move_existing"))
    if base_dir == s.base_dir and not rename:
        return [], False
    new_language = language if rename else s.title_language
    try:
        plan = library.plan_move(
            ctx, list(ctx.session.exec(select(Show)).all()),
            lambda show: resolve_save_path(base_dir, show.title(new_language), show.save_folder),
            anywhere=base_dir != s.base_dir,
        )
    except QbitError:
        if rename:
            raise
        return [], True
    moving = sum(item["moving"] for item in plan)
    if moving:
        raise SettingsError(f"{moving} downloads are still moving to their folders. Try again in a moment.")
    return plan, False
