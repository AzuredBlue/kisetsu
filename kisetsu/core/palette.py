"""Representative colours of a show's banner or cover, used to colour its page."""

import asyncio
import colorsys
import io
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx2
from PIL import Image

logger = logging.getLogger(__name__)

# accent hue, accent saturation, secondary hue, tint hue, tint saturation
Hues = Tuple[int, float, int, int, float]

MAX_IMAGE_BYTES = 5 * 1024 * 1024
SAMPLE_SIZE = (96, 54)
CLUSTERS = 16
MIN_COLOURFULNESS = 0.03
FAMILIES = 12
SECONDARY_MIN_DISTANCE = 60
SECONDARY_MIN_SCORE = 0.15


def dominant_hues(data: bytes) -> Optional[Hues]:
    """Read the colours that represent an image, or None when it has no real colour.

    The image is reduced to a handful of colour clusters, which are grouped into
    hue families. The accent comes from the family that stands out most (how much of
    the image it covers, and how vivid it is, with skin tones counting for less);
    the tint comes from the family that covers the most, so a dull image yields
    near-neutral surfaces rather than a forced colour.
    """
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail(SAMPLE_SIZE)
        quantized = image.quantize(colors=CLUSTERS, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette() or []
        counts = quantized.getcolors() or []

    total = sum(count for count, _ in counts)
    if not total:
        return None

    sums = {}
    colourfulness = 0.0
    for count, index in counts:
        r, g, b = palette[index * 3:index * 3 + 3]
        h, lightness, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        share = count / total
        chroma = s * (1 - abs(2 * lightness - 1))
        colourfulness += share * chroma
        if lightness < 0.10 or lightness > 0.93:
            continue
        family = sums.setdefault(int(h * FAMILIES) % FAMILIES, [0.0, 0.0, 0.0, 0.0, 0.0])
        family[0] += share
        family[1] += share * r
        family[2] += share * g
        family[3] += share * b
        family[4] += share * chroma
    if not sums or colourfulness < MIN_COLOURFULNESS:
        return None

    families = []
    for share, r, g, b, chroma_sum in sums.values():
        h, lightness, s = colorsys.rgb_to_hls(r / share / 255, g / share / 255, b / share / 255)
        degrees = h * 360
        skin = 10 <= degrees <= 45 and lightness > 0.6 and s < 0.6
        score = (share ** 0.6) * (chroma_sum / share + 0.04) * (0.6 if skin else 1.0)
        families.append({"share": share, "hue": degrees, "sat": s, "score": score})

    accent = max(families, key=lambda f: f["score"])
    tint = max(families, key=lambda f: f["share"])
    secondary = None
    for family in families:
        distance = abs(family["hue"] - accent["hue"])
        distance = min(distance, 360 - distance)
        if distance >= SECONDARY_MIN_DISTANCE and family["score"] >= accent["score"] * SECONDARY_MIN_SCORE:
            if secondary is None or family["score"] > secondary["score"]:
                secondary = family
    secondary_hue = secondary["hue"] if secondary else (accent["hue"] + 35) % 360
    return (
        round(accent["hue"]) % 360,
        accent["sat"],
        round(secondary_hue) % 360,
        round(tint["hue"]) % 360,
        tint["sat"],
    )


def is_allowed_image_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and (host == "anilist.co" or host.endswith(".anilist.co"))


async def _fetch_hues(url: str) -> Tuple[bool, Optional[Hues]]:
    if not is_allowed_image_url(url):
        return False, None
    try:
        # Redirects stay off: the host allow-list only covers the URL asked for.
        async with httpx2.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        return False, None
        return True, await asyncio.to_thread(dominant_hues, bytes(data))
    except Exception as exc:
        logger.debug("Could not read colours from %s: %s", url, exc)
        return False, None


async def fetch_hues(*urls: str) -> Tuple[bool, Optional[Hues]]:
    """Read the colours of the first image that has any, trying the URLs in order.

    Returns (ok, hues). ``ok`` is False when an image could not be fetched or
    decoded and nothing was found, so the caller can retry later instead of
    remembering a failure.
    """
    ok = True
    for url in urls:
        if not url:
            continue
        fetched, hues = await _fetch_hues(url)
        if hues is not None:
            return True, hues
        ok = ok and fetched
    return ok, None
