import asyncio
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError
from kisetsu.db.schema import Show
from kisetsu.domain.patterns import compress_home_path
from kisetsu.services import shows
from kisetsu.web import settings as settings_model
from kisetsu.web.deps import read, runtime, write
from kisetsu.web.settings import SettingsError, SettingsPatch

router = APIRouter(prefix="/api/settings")

# What a change to these settings should set going again.
SYNC_FIELDS = {"anilist_username", "anilist_sync_minutes"}
GRAB_FIELDS = {"early_air_tolerance_hours", "title_language", "base_dir", "default_category", "default_seed_ratio"}
HUNT_FIELDS = {"hunt_interval_minutes", "early_air_tolerance_hours"}
FEED_FIELDS = {"feed_refresh_minutes"}
CONNECTION_FIELDS = {"qbit_host", "qbit_username", "qbit_password"}


class QbitTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qbit_host: Optional[str] = None
    qbit_username: Optional[str] = None
    qbit_password: Optional[str] = None


def _current(ctx):
    return settings_model.settings_view(ctx.settings)


def _credentials(ctx, body: QbitTest):
    s = ctx.settings
    host = (body.qbit_host or "").strip() or s.qbit_host
    username = (body.qbit_username or "").strip() or s.qbit_username
    password = body.qbit_password or ""
    if not password:
        settings_model.check_password_travels_with_host(s, host, None)
        password = s.qbit_password
    return host, username, password


def _language_preview(ctx, language: str):
    if language == ctx.settings.title_language:
        return {"shows": [], "torrents": 0}
    plan = settings_model.move_plan(ctx, language)
    entries = [
        {
            "name": item["show"].title(ctx.settings.title_language),
            "old": compress_home_path(item["old"]),
            "new": compress_home_path(item["new"]),
            "torrents": len(item["hashes"]),
        }
        for item in plan
        if item["hashes"]
    ]
    return {"shows": entries, "torrents": sum(entry["torrents"] for entry in entries)}


def _clear_all(ctx):
    everything = ctx.session.exec(select(Show)).all()
    for show in everything:
        shows.delete_show(ctx, show, reason="All shows cleared by user.")
    return len(everything)


@router.get("")
async def get_settings(request: Request):
    return await read(request, _current)


@router.post("")
async def update_settings(body: SettingsPatch, request: Request):
    rt = runtime(request)
    logs = await write(request, settings_model.apply_settings, body)
    changed = set(body.given())
    if changed & CONNECTION_FIELDS:
        rt.jobs.poke("settle")
    if changed & SYNC_FIELDS:
        rt.jobs.poke("sync_anilist")
    if changed & GRAB_FIELDS:
        rt.jobs.poke("grab")
    if changed & HUNT_FIELDS:
        rt.jobs.poke("hunt")
    if changed & FEED_FIELDS:
        rt.jobs.poke("refresh_feeds")
    return {"message": "Settings saved.", "logs": logs}


@router.get("/title-language-preview")
async def title_language_preview(lang: str, request: Request):
    lang = lang.strip().lower()
    if lang not in settings_model.LANGUAGES:
        raise SettingsError("lang must be english or romaji")
    return await write(request, _language_preview, lang)


@router.post("/test-qbit")
async def test_qbit(body: QbitTest, request: Request):
    host, username, password = await read(request, _credentials, body)
    try:
        versions = await asyncio.to_thread(runtime(request).probe, host, username, password)
    except QbitError as e:
        raise SettingsError(f"Connection failed: {e}")
    return {"status": "success", **versions}


@router.post("/sync-anilist")
async def sync_anilist(request: Request):
    runtime(request).jobs.poke("sync_anilist")
    return {"status": "queued"}


@router.post("/clear-all")
async def clear_all(request: Request):
    count = await write(request, _clear_all)
    return {"message": f"Cleared all {count} shows."}
