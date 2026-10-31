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


def poster(top, bottom) -> bytes:
    image = Image.new("RGB", (40, 60), top)
    image.paste(bottom, (0, 52, 40, 60))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_edge_colour_comes_from_the_bottom_of_the_poster():
    edge = palette.edge_color(poster((220, 20, 20), (30, 90, 200)))
    r, b = int(edge[1:3], 16), int(edge[5:7], 16)
    assert abs(r - 30) <= 2 and abs(b - 200) <= 2


def test_vivid_pixels_outweigh_grey_ones_and_a_white_edge_has_no_colour():
    image = Image.new("RGB", (40, 60), (0, 0, 0))
    image.paste((128, 128, 128), (0, 52, 30, 60))
    image.paste((220, 20, 20), (30, 52, 40, 60))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    edge = palette.edge_color(buffer.getvalue())
    assert int(edge[1:3], 16) > int(edge[5:7], 16) + 40
    assert palette.edge_color(poster((220, 20, 20), (255, 255, 255))) is None


@pytest.mark.parametrize("url,allowed", [
    ("https://s4.anilist.co/file/banner.jpg", True),
    ("https://anilist.co/x.png", True),
    ("http://s4.anilist.co/x.png", False),
    ("https://anilist.co.evil.example/x.png", False),
    ("https://evil.example/anilist.co", False),
])
def test_only_https_anilist_images_are_fetched(url, allowed):
    assert palette.is_allowed_image_url(url) is allowed
