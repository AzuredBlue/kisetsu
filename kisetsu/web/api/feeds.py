from typing import List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlmodel import select

from kisetsu.db.schema import Feed
from kisetsu.services import feeds
from kisetsu.web import views
from kisetsu.web.deps import feed_or_404, read, runtime, write

router = APIRouter(prefix="/api/feeds")

ITEMS_LIMIT_MAX = 200


def _clean(value: Optional[str], what: str) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError(f"The feed {what} can't be empty.")
    return value


class NewFeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str

    @field_validator("name", "url")
    @classmethod
    def _required(cls, value: str) -> str:
        return _clean(value, "name and URL")

    @field_validator("url")
    @classmethod
    def _http(cls, value: str) -> str:
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("The feed URL must start with http:// or https://")
        return value


class FeedPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    url: Optional[str] = None

    @field_validator("name", "url")
    @classmethod
    def _stripped(cls, value: Optional[str]) -> Optional[str]:
        return _clean(value, "name and URL")

    @field_validator("url")
    @classmethod
    def _http(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.lower().startswith(("http://", "https://")):
            raise ValueError("The feed URL must start with http:// or https://")
        return value


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: List[int]


def _url_taken(ctx, url: str) -> bool:
    return ctx.session.exec(select(Feed.id).where(Feed.url == url)).first() is not None


def _add(ctx, body: NewFeed):
    if _url_taken(ctx, body.url):
        raise ValueError("That feed is already in the list.")
    return feeds.add_feed(ctx, body.name, body.url).id


def _edit(ctx, feed_id: int, body: FeedPatch):
    feed = feed_or_404(ctx, feed_id)
    if body.url and body.url != feed.url and _url_taken(ctx, body.url):
        raise ValueError("That feed is already in the list.")
    feeds.edit_feed(ctx, feed, name=body.name, url=body.url)


def _delete(ctx, feed_id: int):
    return feeds.forget_feed(ctx, feed_or_404(ctx, feed_id))


def _import(ctx):
    return [feed.id for feed in feeds.import_from_qbit(ctx)]


@router.get("")
async def list_feeds(request: Request):
    return await read(request, views.feeds_view)


@router.get("/items")
async def feed_items(request: Request, feed_id: Optional[int] = None, matched: bool = False, limit: int = 100, offset: int = 0):
    return await read(request, views.feed_items_view, feed_id, matched, max(1, min(limit, ITEMS_LIMIT_MAX)), max(0, offset))


@router.post("")
async def add_feed(body: NewFeed, request: Request):
    feed_id = await write(request, _add, body)
    runtime(request).jobs.poke("refresh_feeds")
    return {"id": feed_id}


@router.post("/reorder")
async def reorder(body: Order, request: Request):
    await write(request, feeds.reorder_feeds, body.ids)
    runtime(request).jobs.poke("grab")
    return {"status": "success"}


@router.post("/sync")
async def sync_from_qbit(request: Request):
    added = await write(request, _import)
    if added:
        runtime(request).jobs.poke("refresh_feeds")
    return {"added": len(added)}


@router.patch("/{feed_id}")
async def edit_feed(feed_id: int, body: FeedPatch, request: Request):
    await write(request, _edit, feed_id, body)
    if body.url:
        runtime(request).jobs.poke("refresh_feeds")
    return {"status": "success"}


@router.delete("/{feed_id}")
async def delete_feed(feed_id: int, request: Request):
    shows = await write(request, _delete, feed_id)
    runtime(request).jobs.poke("grab")
    return {"released_shows": shows}
