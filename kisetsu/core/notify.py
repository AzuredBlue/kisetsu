import hashlib
import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import httpx2

from kisetsu.config import CONFIG_DIR
from kisetsu.core.palette import MAX_IMAGE_BYTES, is_allowed_image_url

logger = logging.getLogger("kisetsu.core.notify")

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
        temp = path.with_suffix(".tmp")
        temp.write_bytes(resp.content)
        temp.replace(path)
        return path
    except Exception as e:
        logger.debug(f"Could not cache cover {url}: {e}")
        return None


def _run(binary: str, title: str, body: str, cover_url: Optional[str]) -> None:
    cover = _cached_cover(cover_url) if cover_url else None
    command = [binary, "--app-name=Kisetsu", f"--icon={cover or FALLBACK_ICON}", title, body]
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"Desktop notification failed: {e}")


def notify_episode(
    show_name: str,
    episode: int,
    body: str = "Download finished",
    cover_url: Optional[str] = None,
) -> None:
    """Show a desktop notification through notify-send; never raises or blocks the caller."""
    binary = shutil.which("notify-send")
    if not binary:
        logger.debug("notify-send not found; skipping desktop notification")
        return
    threading.Thread(
        target=_run, args=(binary, f"{show_name} - Episode {episode}", body, cover_url), daemon=True
    ).start()
