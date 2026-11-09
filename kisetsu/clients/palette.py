"""Representative colours of a show's banner or cover, used to colour its page and its poster card."""

import asyncio
import colorsys
import io
import logging
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
MIN_ACCENT_CHROMA = 0.08
# A dark or grey image still has a colour to give when some of it is this vivid, such as neon lines on black.
VIVID_ACCENT_CHROMA = 0.3
MIN_TINT_CHROMA = 0.04
FAMILIES = 12
SECONDARY_MIN_DISTANCE = 60
SECONDARY_MIN_SCORE = 0.15
# Drawn skin is a light orange: these hues at this lightness or above. Pinks and salmons, and anything darker, are
# clothes, hair or light, and count in full.
SKIN_HUES = (18, 40)
SKIN_MIN_LIGHTNESS = 0.75
SKIN_WEIGHT = 0.4
# The part of an image (top and bottom, as fractions of its height) at the foot of the show page's banner, where the
# banner fades into the page: a banner's lower edge, or the middle of a poster, which the page crops to a wide band
# when it stands in for a banner.
BANNER_FADE = (0.65, 1.0)
COVER_FADE = (0.35, 0.65)

T = TypeVar("T")


def _families(image) -> Tuple[float, list]:
    """The image's hue families and how colourful the image is overall.

    The image is reduced to a handful of colour clusters, which are grouped by hue;
    near-black and near-white clusters belong to no family.
    """
    from PIL import Image

    quantized = image.quantize(colors=CLUSTERS, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    counts = quantized.getcolors() or []
    total = sum(count for count, _ in counts)
    if not total:
        return 0.0, []

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

    families = []
    for share, r, g, b, chroma_sum in sums.values():
        h, lightness, s = colorsys.rgb_to_hls(r / share / 255, g / share / 255, b / share / 255)
        degrees = h * 360
        skin = SKIN_HUES[0] <= degrees <= SKIN_HUES[1] and lightness >= SKIN_MIN_LIGHTNESS
        chroma = chroma_sum / share
        score = (share ** 0.6) * (chroma + 0.04) * (SKIN_WEIGHT if skin else 1.0)
        families.append({"share": share, "hue": degrees, "sat": s, "chroma": chroma, "skin": skin, "score": score})
    return colourfulness, families


def dominant_hues(data: bytes, fade: Tuple[float, float] = (0.0, 1.0)) -> Optional[Hues]:
    """Read the colours that represent an image, or None when it has no real colour.

    The accent comes from the hue family that stands out most in the whole image (how
    much of it the family covers, and how vivid it is, with skin counting for less).
    The tint comes from the family that covers most of the ``fade`` band, the part of
    the image the page's own colour takes over from, so the page carries on from it.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        width, height = image.size
        band = image.crop((0, int(height * fade[0]), width, max(int(height * fade[0]) + 1, int(height * fade[1]))))
        image.thumbnail(SAMPLE_SIZE)
        band.thumbnail(SAMPLE_SIZE)
        colourfulness, families = _families(image)
        _, band_families = _families(band)

    if not families:
        return None
    accent = max(families, key=lambda f: f["score"])
    if accent["chroma"] < MIN_ACCENT_CHROMA or (colourfulness < MIN_COLOURFULNESS and accent["chroma"] < VIVID_ACCENT_CHROMA):
        return None
    tinted = [f for f in band_families if f["chroma"] >= MIN_TINT_CHROMA and not f["skin"]]
    tint = max(tinted, key=lambda f: f["share"]) if tinted else accent
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


async def fetch_hues(banner: str, cover: str) -> Tuple[bool, Optional[Hues]]:
    """Read a show's colours from its banner, or from its cover when the banner has none.

    Returns (ok, hues). ``ok`` is False when an image could not be fetched or
    decoded and nothing was found, so the caller can retry later instead of
    remembering a failure.
    """
    ok = True
    for url, fade in ((banner, BANNER_FADE), (cover, COVER_FADE)):
        if not url:
            continue
        fetched, hues = await _read(url, lambda data, fade=fade: dominant_hues(data, fade))
        if hues is not None:
            return True, hues
        ok = ok and fetched
    return ok, None
