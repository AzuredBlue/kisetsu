import hashlib
from pathlib import Path
from typing import Dict, Optional

from fastapi.responses import HTMLResponse

SERVER_DIR = Path(__file__).parent
STATIC_DIR = SERVER_DIR / "static"
INDEX_PATH = SERVER_DIR / "ui" / "index.html"


def _asset_version(name: str) -> str:
    # The page is never cached but its stylesheet and script are, so their URLs carry a hash of their content.
    try:
        return hashlib.sha1((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


def get_web_ui_html(headers: Optional[Dict[str, str]] = None) -> HTMLResponse:
    html = INDEX_PATH.read_text(encoding="utf-8")
    html = html.replace("@@CSS_VERSION@@", _asset_version("app.css"))
    html = html.replace("@@JS_VERSION@@", _asset_version("app.js"))
    return HTMLResponse(content=html, headers=headers)
