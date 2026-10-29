"""Adding a show by hand: the user's AniList list, the feed search for one entry, and the confirmed add."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from kisetsu.clients.anilist import AniListDeferred, AniListError
from kisetsu.services import addshow
from kisetsu.web.deps import NotFound, read, runtime, write

router = APIRouter(prefix="/api/add")

# The list is fetched again on request at most this often; typing in the search never asks AniList.
LIST_REFRESH_SECONDS = 60.0
_background: Set[asyncio.Task] = set()


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anilist_id: int


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anilist_id: int
    feed_id: int
    item_ids: List[int]


def _age(client) -> float:
    if client.user_list_at is None:
        return float("inf")
    return (datetime.now(timezone.utc) - client.user_list_at).total_seconds()


def _entry(request: Request, anilist_id: int) -> Dict[str, Any]:
    entry = next((e for e in runtime(request).anilist.user_list if e["anilist_id"] == anilist_id), None)
    if entry is None or entry.get("list_status") not in addshow.LIST_STATUSES:
        raise NotFound("That entry is not on your AniList list. Refresh the list and try again.")
    return entry


@router.get("/list")
async def user_list(request: Request, refresh: bool = False):
    client = runtime(request).anilist
    username = await read(request, lambda ctx: ctx.settings.anilist_username.strip())
    if not username:
        raise ValueError("Set your AniList user name in Settings first.")
    notice = None
    if client.user_list_at is None or (refresh and _age(client) >= LIST_REFRESH_SECONDS):
        try:
            await client.fetch_user_list(username)
        except AniListDeferred as e:
            if client.user_list_at is None:
                return JSONResponse({"detail": f"AniList is busy. Try again in {max(1, round(e.retry_in))}s."}, status_code=503)
            notice = "AniList is busy, so this is the list from the last check."
        except AniListError as e:
            if client.user_list_at is None:
                return JSONResponse({"detail": str(e)}, status_code=502)
            notice = "AniList could not be reached, so this is the list from the last check."
    return {
        "entries": await read(request, addshow.list_view, client.user_list),
        "fetched_at": client.user_list_at.isoformat() if client.user_list_at else None,
        "refresh_in": max(0.0, LIST_REFRESH_SECONDS - _age(client)),
        "notice": notice,
    }


@router.post("/search")
async def search(body: Search, request: Request):
    entry = _entry(request, body.anilist_id)
    return await runtime(request).jobs.search_new_show(entry)


@router.post("/confirm")
async def confirm(body: Confirm, request: Request):
    entry = _entry(request, body.anilist_id)
    result = await write(request, addshow.confirm, entry, body.feed_id, body.item_ids)
    task = asyncio.create_task(runtime(request).jobs.after_manual_add())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return result
