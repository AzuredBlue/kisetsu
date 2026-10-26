from typing import List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from kisetsu.db.schema import Episode
from kisetsu.services import feeds as feeds_service, library, manual, shows
from kisetsu.web import views
from kisetsu.web.deps import NotFound, read, runtime, show_or_404, write

router = APIRouter(prefix="/api/shows")


class EditShow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    save_folder: Optional[str] = None
    aliases: Optional[List[str]] = None
    category: Optional[str] = None
    seed_ratio: Optional[float] = None
    # None leaves the feed alone, 0 returns the show to Auto-Discover, any other id pins that feed.
    feed_id: Optional[int] = None
    release_lock: bool = False
    episode_offset: Optional[int] = None


class Download(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    expect_episode: Optional[int] = None
    fuzzy: bool = False


def _show_view(ctx, show_id: int):
    return views.show_view(ctx, show_or_404(ctx, show_id))


def _feed_matches(ctx, show_id: int):
    return manual.feed_matches(ctx, show_or_404(ctx, show_id))


def _edit(ctx, show_id: int, body: EditShow):
    result = shows.edit_show(ctx, show_or_404(ctx, show_id), **body.model_dump())
    return {"message": result.message, "canceled": result.canceled}


def _pause(ctx, show_id: int):
    shows.pause(ctx, show_or_404(ctx, show_id))


def _resume(ctx, show_id: int):
    shows.resume(ctx, show_or_404(ctx, show_id))


def _rediscover(ctx, show_id: int):
    feeds_service.release(ctx, show_or_404(ctx, show_id))


def _preview(ctx, show_id: int):
    return library.show_torrent_preview(ctx, show_or_404(ctx, show_id))


def _delete(ctx, show_id: int, delete_torrents: bool, delete_files: bool):
    show = show_or_404(ctx, show_id)
    return shows.delete_show(ctx, show, reason="Show deleted by user.", dismiss=True, delete_torrents=delete_torrents, delete_files=delete_files)


def _download(ctx, show_id: int, body: Download):
    return manual.manual_grab(ctx, show_or_404(ctx, show_id), body.title, fuzzy=body.fuzzy, expect_episode=body.expect_episode)


def _restore(ctx, show_id: int, episode_id: int):
    show = show_or_404(ctx, show_id)
    episode = ctx.session.get(Episode, episode_id)
    if episode is None or episode.show_id != show.id:
        raise NotFound("Episode not found.")
    return manual.restore_episode(ctx, show, episode)


@router.get("")
async def list_shows(request: Request):
    return await read(request, views.shows_view)


@router.post("/pause-all")
async def pause_all(request: Request):
    return {"paused": await write(request, shows.pause_all)}


@router.post("/resume-all")
async def resume_all(request: Request):
    count = await write(request, shows.resume_all)
    runtime(request).jobs.poke("grab")
    return {"resumed": count}


@router.post("/auto-discover-all")
async def auto_discover_all(request: Request):
    count = await write(request, shows.auto_discover_all)
    runtime(request).jobs.poke("grab")
    return {"released": count}


@router.get("/{show_id}")
async def get_show(show_id: int, request: Request):
    return await read(request, _show_view, show_id)


@router.get("/{show_id}/feed-matches")
async def feed_matches(show_id: int, request: Request):
    return await read(request, _feed_matches, show_id)


@router.post("/{show_id}/edit")
async def edit(show_id: int, body: EditShow, request: Request):
    result = await write(request, _edit, show_id, body)
    runtime(request).jobs.poke("grab")
    return result


@router.post("/{show_id}/pause")
async def pause(show_id: int, request: Request):
    await write(request, _pause, show_id)
    return {"status": "paused"}


@router.post("/{show_id}/resume")
async def resume(show_id: int, request: Request):
    await write(request, _resume, show_id)
    runtime(request).jobs.poke("grab")
    return {"status": "resumed"}


@router.post("/{show_id}/rediscover")
async def rediscover(show_id: int, request: Request):
    await write(request, _rediscover, show_id)
    runtime(request).jobs.poke("grab")
    return {"status": "released"}


@router.post("/{show_id}/search-releases")
async def search_releases(show_id: int, request: Request):
    await read(request, _show_view, show_id)
    return {"found": await runtime(request).jobs.search_older(show_id)}


@router.post("/{show_id}/download")
async def download(show_id: int, body: Download, request: Request):
    return {"message": await write(request, _download, show_id, body)}


@router.post("/{show_id}/episodes/{episode_id}/restore")
async def restore(show_id: int, episode_id: int, request: Request):
    return {"message": await write(request, _restore, show_id, episode_id)}


@router.get("/{show_id}/delete-preview")
async def delete_preview(show_id: int, request: Request):
    return await write(request, _preview, show_id)


@router.delete("/{show_id}")
async def delete(show_id: int, request: Request, delete_torrents: bool = False, delete_files: bool = False):
    removed = await write(request, _delete, show_id, delete_torrents, delete_files)
    return {"deleted_torrents": removed}
