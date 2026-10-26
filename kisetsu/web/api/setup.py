from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlmodel import delete, select

from kisetsu.clients.anilist import AniListError
from kisetsu.db.schema import AppState, Feed, Settings
from kisetsu.services import feeds as feeds_service
from kisetsu.services.context import SETUP_KEY, set_state, setup_completed
from kisetsu.web import settings as settings_model
from kisetsu.web.deps import NotFound, read, runtime, write
from kisetsu.web.settings import SETUP_FIELDS, SettingsError, SettingsPatch

router = APIRouter(prefix="/api/setup")


class Finish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anilist_username: str = ""


def _status(ctx):
    return setup_completed(ctx)


def _save(ctx, patch: SettingsPatch):
    """Store one wizard step. Only open until setup is finished; afterwards the Settings page owns this."""
    if setup_completed(ctx):
        raise _AlreadyDone()
    outside = set(patch.given()) - SETUP_FIELDS
    if outside:
        raise SettingsError(f"Not part of setup: {', '.join(sorted(outside))}.")
    settings_model.apply_settings(ctx, patch)


def _finish(ctx, name: str):
    if setup_completed(ctx):
        raise _AlreadyDone()
    ctx.settings.anilist_username = name
    ctx.session.add(ctx.settings)
    set_state(ctx, SETUP_KEY, "1")
    ctx.emit("settings", "status")


def _reset(ctx):
    for feed in ctx.session.exec(select(Feed)).all():
        feeds_service.forget_feed(ctx, feed)
    ctx.session.execute(delete(Settings))
    ctx.session.execute(delete(AppState))
    ctx.session.flush()
    ctx.session.add(Settings(id=1))
    ctx.caches.known_items = None
    ctx.emit("*")


class _AlreadyDone(Exception):
    pass


@router.get("/status")
async def status(request: Request):
    rt = runtime(request)
    return {"setup_completed": await read(request, _status), "sandbox": rt.sandbox}


@router.post("/save")
async def save(body: SettingsPatch, request: Request):
    try:
        await write(request, _save, body)
    except _AlreadyDone:
        return _conflict()
    return {"status": "success"}


@router.post("/complete")
async def complete(body: Finish, request: Request):
    rt = runtime(request)
    name = body.anilist_username.strip()
    if not name:
        raise SettingsError("Enter your AniList username.")
    if await read(request, _status):
        return _conflict()
    try:
        exists = await rt.anilist.user_exists(name)
    except AniListError as e:
        return _gateway(f"Couldn't reach AniList: {e}")
    if not exists:
        raise SettingsError(f"AniList user '{name}' was not found.")
    try:
        await write(request, _finish, name)
    except _AlreadyDone:
        return _conflict()
    rt.jobs.poke("startup")
    return {"status": "success", "sandbox": rt.sandbox}


@router.post("/reset")
async def reset(request: Request):
    if not runtime(request).sandbox:
        raise NotFound()
    await write(request, _reset)
    return {"status": "success"}


def _conflict():
    return JSONResponse({"detail": "Setup is already finished."}, status_code=409)


def _gateway(message: str):
    return JSONResponse({"detail": message}, status_code=502)
