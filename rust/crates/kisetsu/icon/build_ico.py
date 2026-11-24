"""Builds kisetsu/web/static/favicon.ico from the SVG masters in this folder.

Each of 16, 20, 24 and 32 px is drawn from its own hand-hinted master; 48 and 256 px are drawn
from the 32 px master. Sizes up to 48 px are stored as 32-bit bitmaps and 256 px as PNG.

Needs `pip install resvg-py pillow`. Run: python build_ico.py
"""

import io
import struct
from pathlib import Path

import resvg_py
from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[3] / "kisetsu" / "web" / "static" / "favicon.ico"
SIZES = {16: 16, 20: 20, 24: 24, 32: 32, 48: 32, 256: 32}


def render(size: int, master: int) -> Image.Image:
    svg = (HERE / f"kisetsu-{master}.svg").read_text(encoding="utf-8")
    png = bytes(resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size))
    return Image.open(io.BytesIO(png)).convert("RGBA")


def bitmap(image: Image.Image) -> bytes:
    """A 32-bit DIB as an icon entry: colour rows bottom-up, then the 1-bit AND mask."""
    size = image.width
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = image.load()
    colour = bytearray()
    for y in reversed(range(size)):
        for x in range(size):
            r, g, b, a = pixels[x, y]
            colour += bytes((b, g, r, a))
    stride = (size + 31) // 32 * 4
    mask = bytearray()
    for y in reversed(range(size)):
        row = bytearray(stride)
        for x in range(size):
            if pixels[x, y][3] == 0:
                row[x // 8] |= 0x80 >> (x % 8)
        mask += row
    return header + bytes(colour) + bytes(mask)


def main() -> None:
    entries = []
    for size, master in SIZES.items():
        image = render(size, master)
        if size >= 256:
            data = io.BytesIO()
            image.save(data, format="PNG", optimize=True)
            entries.append((size, data.getvalue()))
        else:
            entries.append((size, bitmap(image)))
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for size, data in entries:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in entries:
        out += data
    OUT.write_bytes(out)
    print(f"{OUT} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
