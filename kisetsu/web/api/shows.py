import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from kisetsu.clients.qbittorrent import QbitError
from kisetsu.db.schema import Episode
from kisetsu.services import feeds as feeds_service, library, manual, shows
from kisetsu.web import views
from kisetsu.web.deps import NotFound, read, runtime, show_or_404, write

logger = logging.getLogger("kisetsu.web.api.shows")

router = APIRouter(prefix="/api/shows")

# How often, half a second apart, a delete asks qBittorrent whether the torrents are gone before removing the folder.
GONE_CHECKS = 20


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
    show = show_or_404(ctx, show_id)
    torrents = library.show_torrent_preview(ctx, show)
    return {"torrents": torrents, "folder": library.folder_plan(ctx, show, [t["hash"] for t in torrents])}


def _delete(ctx, show_id: int, delete_torrents: bool, delete_files: bool):
    show = show_or_404(ctx, show_id)
    name = show.title(ctx.settings.title_language)
    hashes = library.find_show_torrents(ctx, show) if delete_torrents else []
    # The folder goes only with its files: keeping the files keeps the folder.
    folder = library.folder_plan(ctx, show, hashes) if delete_torrents and delete_files else None
    removed = shows.delete_show(
        ctx, show, reason="Show deleted by user.", dismiss=True, delete_torrents=delete_torrents, delete_files=delete_files, hashes=hashes,
    )
    return name, removed, hashes, folder, ctx.settings.base_dir


def _want(ctx, show_id: int, episode_id: int):
    show = show_or_404(ctx, show_id)
    episode = ctx.session.get(Episode, episode_id)
    if episode is None or episode.show_id != show.id:
        raise NotFound("Episode not found.")
    return shows.want_episode(ctx, show, episode)


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


@router.post("/{show_id}/episodes/{episode_id}/want")
async def want(show_id: int, episode_id: int, request: Request):
    message = await write(request, _want, show_id, episode_id)
    runtime(request).jobs.request_backfill([show_id])
    return {"message": message}


@router.get("/{show_id}/delete-preview")
async def delete_preview(show_id: int, request: Request):
    return await write(request, _preview, show_id)


@router.delete("/{show_id}")
async def delete(show_id: int, request: Request, delete_torrents: bool = False, delete_files: bool = False):
    name, removed, hashes, plan, base_dir = await write(request, _delete, show_id, delete_torrents, delete_files)
    folder = None
    if plan and plan["exists"]:
        error = plan["reason"]
        if plan["remove"]:
            # qBittorrent removes the files in the background; the folder goes once it lists none of the torrents.
            error = None
            for _ in range(GONE_CHECKS):
                try:
                    if await write(request, library.torrents_gone, hashes):
                        break
                except QbitError as e:
                    error = f"qBittorrent could not confirm the torrents are gone ({e}), so the folder was kept."
                    break
                await asyncio.sleep(0.5)
            if error is None:
                error = await asyncio.to_thread(library.remove_folder, plan["path"], base_dir)
        folder = {"path": plan["path"], "removed": plan["remove"] and error is None, "reason": error}
        if folder["removed"]:
            logger.info(f"Deleted '{name}' and its folder {plan['path']}.")
        else:
            logger.warning(f"Deleted '{name}' but kept its folder {plan['path']}: {error}")
    return {"deleted_torrents": removed, "folder": folder}
