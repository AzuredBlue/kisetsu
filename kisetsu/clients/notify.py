"""Desktop notifications when an episode downloads; never raises and never blocks the caller."""

import hashlib
import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import httpx2

from kisetsu.clients.palette import MAX_IMAGE_BYTES, is_allowed_image_url
from kisetsu.config import CONFIG_DIR

logger = logging.getLogger("kisetsu.clients.notify")

COVER_DIR = CONFIG_DIR / "covers"
FALLBACK_ICON = "folder-download"


def _cached_cover(url: str) -> Optional[Path]:
    """The cover as a local file, downloaded once; None when it can't be had."""
    if not is_allowed_image_url(url):
        return None
    path = COVER_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.img"
    if path.exists():
        return path
    try:
        # Redirects stay off: the host allow-list only covers the URL asked for.
        with httpx2.Client(timeout=10.0, follow_redirects=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            if len(resp.content) > MAX_IMAGE_BYTES:
                return None
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        # One temp file per thread: two episodes of a show can finish together.
        temp = path.with_name(f"{path.stem}.{threading.get_ident()}.tmp")
        temp.write_bytes(resp.content)
        temp.replace(path)
        return path
    except Exception as e:
        logger.debug(f"Could not cache cover {url}: {e}")
        return None


def _send_with_library(title: str, body: str, cover: Optional[Path]) -> bool:
    try:
        import asyncio

        from desktop_notifier import DesktopNotifier, Icon
    except ImportError:
        return False
    try:
        notifier = DesktopNotifier(app_name="Kisetsu")
        icon = Icon(path=cover) if cover else None
        asyncio.run(notifier.send(title=title, message=body, icon=icon))
        return True
    except Exception as e:
        logger.debug(f"Desktop notification failed: {e}")
        return False


def _run(title: str, body: str, cover_url: Optional[str]) -> None:
    cover = _cached_cover(cover_url) if cover_url else None
    if _send_with_library(title, body, cover):
        return
    binary = shutil.which("notify-send")
    if not binary:
        logger.debug("No notification backend available; skipping desktop notification")
        return
    command = [binary, "--app-name=Kisetsu", f"--icon={cover or FALLBACK_ICON}", title, body]
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"Desktop notification failed: {e}")


def notify(title: str, body: str, cover_url: Optional[str] = None) -> None:
    """Show a desktop notification from a background thread."""
    threading.Thread(target=_run, args=(title, body, cover_url), daemon=True).start()
