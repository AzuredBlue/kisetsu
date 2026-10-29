"""Adding a show by hand: the user's AniList list, the feed search for one entry, and the confirmed add."""

import asyncio
from typing import Any, Dict, List, Set

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from kisetsu.services import addshow
from kisetsu.web.deps import NotFound, read, runtime, write

router = APIRouter(prefix="/api/add")

_background: Set[asyncio.Task] = set()


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anilist_id: int


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anilist_id: int
    feed_id: int
    item_ids: List[int]


def _entry(request: Request, anilist_id: int) -> Dict[str, Any]:
    entry = next((e for e in runtime(request).anilist.user_list if e["anilist_id"] == anilist_id), None)
    if entry is None or entry.get("list_status") not in addshow.LIST_STATUSES:
        raise NotFound("That entry is not on your AniList list. Press Sync Now and try again.")
    return entry


@router.get("/list")
async def user_list(request: Request):
    client = runtime(request).anilist
    username = await read(request, lambda ctx: ctx.settings.anilist_username.strip())
    if not username:
        raise ValueError("Set your AniList user name in Settings first.")
    if client.user_list_at is None:
        raise ValueError("Your AniList list has not been read yet. Press Sync Now and try again.")
    return {
        "entries": await read(request, addshow.list_view, client.user_list),
        "fetched_at": client.user_list_at.isoformat(),
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
