"""Representative colours of a show's banner or cover, used to colour its page and its poster card."""

import asyncio
import colorsys
import io
import logging
import math
from typing import Callable, Optional, Tuple, TypeVar
from urllib.parse import urlparse

import httpx2

from kisetsu.clients import traffic

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
EDGE_FRACTION = 0.08
EDGE_SAMPLE = (96, 12)
EDGE_MIN_WEIGHT = 0.02

T = TypeVar("T")


def dominant_hues(data: bytes) -> Optional[Hues]:
    """Read the colours that represent an image, or None when it has no real colour.

    The image is reduced to a handful of colour clusters, which are grouped into
    hue families. The accent comes from the family that stands out most (how much of
    the image it covers, and how vivid it is, with skin tones counting for less);
    the tint comes from the family that covers the most, so a dull image yields
    near-neutral surfaces rather than a forced colour.
    """
    from PIL import Image

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


def _srgb_to_linear(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(v: float) -> float:
    v = min(1.0, max(0.0, v))
    return v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def _oklab(r: int, g: int, b: int) -> Tuple[float, float, float]:
    lr, lg, lb = (_srgb_to_linear(c / 255) for c in (r, g, b))
    l_ = (0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb) ** (1 / 3)
    m_ = (0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb) ** (1 / 3)
    s_ = (0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb) ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _oklab_to_hex(L: float, a: float, b: float) -> str:
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return "#" + "".join(f"{round(_linear_to_srgb(c) * 255):02x}" for c in rgb)


def edge_color(data: bytes) -> Optional[str]:
    """The colour along a poster's bottom edge as "#rrggbb", for the panel under it on the shows grid.

    An OKLab average of the bottom strip, weighted by how colourful each pixel is: a few vivid
    pixels do not take over (as the most common cluster would), and grey areas do not wash the
    colour out. Near-black and near-white pixels are left out; None when nothing else is left.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        width, height = image.size
        strip = image.crop((0, int(height * (1 - EDGE_FRACTION)), width, height))
        strip.thumbnail(EDGE_SAMPLE)
        pixels = [strip.getpixel((x, y)) for y in range(strip.height) for x in range(strip.width)]

    total = sum_l = sum_a = sum_b = 0.0
    for r, g, b in pixels:
        L, a, bb = _oklab(r, g, b)
        if L < 0.10 or L > 0.95:
            continue
        weight = math.hypot(a, bb) + EDGE_MIN_WEIGHT
        total += weight
        sum_l += weight * L
        sum_a += weight * a
        sum_b += weight * bb
    if not total:
        return None
    return _oklab_to_hex(sum_l / total, sum_a / total, sum_b / total)


def is_allowed_image_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and (host == "anilist.co" or host.endswith(".anilist.co"))


async def _read(url: str, measure: Callable[[bytes], Optional[T]]) -> Tuple[bool, Optional[T]]:
    if not is_allowed_image_url(url):
        return False, None
    answered = False
    try:
        # Redirects stay off: the host allow-list only covers the URL asked for.
        async with httpx2.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            async with client.stream("GET", url) as resp:
                answered = True
                traffic.record("Cover art", traffic.host_of(url), traffic.outcome_of(resp.status_code))
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        return False, None
        return True, await asyncio.to_thread(measure, bytes(data))
    except Exception as exc:
        if not answered:
            traffic.record("Cover art", traffic.host_of(url), traffic.ERROR)
        logger.debug("Could not read colours from %s: %s", url, exc)
        return False, None


async def _fetch_hues(url: str) -> Tuple[bool, Optional[Hues]]:
    return await _read(url, dominant_hues)


async def fetch_edge(url: str) -> Tuple[bool, Optional[str]]:
    """The bottom-edge colour of the poster at ``url``: (ok, colour), with ``ok`` as in fetch_hues."""
    return await _read(url, edge_color)


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
