import io

import pytest
from PIL import Image

from kisetsu.clients import palette


def png(color) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_grey_image_has_no_hue_and_a_red_one_gives_its_hue():
    assert palette.dominant_hues(png((128, 128, 128))) is None
    accent_hue, _sat, _secondary, tint_hue, _tint_sat = palette.dominant_hues(png((220, 20, 20)))
    assert accent_hue in (*range(0, 15), *range(345, 360))
    assert tint_hue in (*range(0, 15), *range(345, 360))


@pytest.mark.parametrize("url,allowed", [
    ("https://s4.anilist.co/file/banner.jpg", True),
    ("https://anilist.co/x.png", True),
    ("http://s4.anilist.co/x.png", False),
    ("https://anilist.co.evil.example/x.png", False),
    ("https://evil.example/anilist.co", False),
])
def test_only_https_anilist_images_are_fetched(url, allowed):
    assert palette.is_allowed_image_url(url) is allowed
