"""Draw the phone app icons with the standard library: a blue circle with a check on a dark square.

    python scripts/make_icons.py

The check is the product: nothing happens until you approve. The mark stays inside the middle 80% of
the square, because Android may crop an installed icon to a circle.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "chief" / "web" / "icons"
BACKGROUND = (15, 17, 21)
CIRCLE = (91, 140, 255)
CHECK = (255, 255, 255)
RADIUS = 0.33
CHECK_POINTS = ((0.36, 0.51), (0.46, 0.61), (0.65, 0.41))
CHECK_WIDTH = 0.032
SAMPLES = 3  # per side, for smooth edges


def _segment_distance(px: float, py: float, a: tuple, b: tuple) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _color_at(x: float, y: float) -> tuple:
    if math.hypot(x - 0.5, y - 0.5) > RADIUS:
        return BACKGROUND
    near = min(_segment_distance(x, y, CHECK_POINTS[i], CHECK_POINTS[i + 1]) for i in range(2))
    return CHECK if near <= CHECK_WIDTH else CIRCLE


def _pixel(col: int, row: int, size: int) -> bytes:
    colors = [
        _color_at((col + (i + 0.5) / SAMPLES) / size, (row + (j + 0.5) / SAMPLES) / size)
        for j in range(SAMPLES)
        for i in range(SAMPLES)
    ]
    return bytes(sum(c[k] for c in colors) // len(colors) for k in range(3))


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(size: int) -> bytes:
    rows = b"".join(b"\0" + b"".join(_pixel(col, row, size) for col in range(size)) for row in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(rows, 9)) + _chunk(b"IEND", b"")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        (OUT / name).write_bytes(png(size))
        print(OUT / name)


if __name__ == "__main__":
    main()
