"""Exporting everything to one file, and restoring from it."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from kisetsu.services import backup
from kisetsu.web.deps import read, runtime, write

router = APIRouter(prefix="/api/backup")

# The upload is the request body, so the password travels beside it, percent-encoded as headers are latin-1.
PASSWORD_HEADER = "x-kisetsu-qbit-password"


async def _stage(request: Request, scratch: Path) -> backup.Staged:
    """Store the uploaded file and check it."""
    archive = scratch / "upload.zip"
    size = 0
    with archive.open("wb") as out:
        async for chunk in request.stream():
            size += len(chunk)
            if size > backup.MAX_BACKUP_BYTES:
                raise backup.BackupError(backup.NOT_A_BACKUP)
            out.write(chunk)
    return await asyncio.to_thread(backup.unpack, archive, scratch)


@router.get("/export")
async def export(request: Request, theme: Optional[str] = None):
    rt = runtime(request)
    scratch = Path(tempfile.mkdtemp(prefix="kisetsu-export-"))
    target = scratch / "backup.zip"
    try:
        # On the engine thread, so no job writes halfway through the copy.
        await rt.engine.dry_run(backup.write_backup, target, theme)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return FileResponse(
        target,
        media_type="application/zip",
        filename=f"kisetsu-backup-{rt.engine.clock().strftime('%Y-%m-%d')}.zip",
        background=BackgroundTask(shutil.rmtree, scratch, ignore_errors=True),
    )


@router.post("/inspect")
async def inspect(request: Request):
    """What is in this file and what restoring it would replace. Changes nothing."""
    with tempfile.TemporaryDirectory(prefix="kisetsu-restore-") as scratch:
        staged = await _stage(request, Path(scratch))
        return await read(request, backup.preview, staged)


@router.post("/restore")
async def restore(request: Request):
    rt = runtime(request)
    password = unquote(request.headers.get(PASSWORD_HEADER, ""))
    with tempfile.TemporaryDirectory(prefix="kisetsu-restore-") as scratch:
        staged = await _stage(request, Path(scratch))
        async with rt.jobs.stopped():
            result = await write(request, backup.restore, staged, password)
    # The startup pass skips a recent sync, but the list in memory may be another user's.
    rt.jobs.poke("sync_anilist")
    return result
