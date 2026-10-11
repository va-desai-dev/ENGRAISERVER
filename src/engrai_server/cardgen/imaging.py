"""Geometry for Card Studio's image requests.

The image worker wants each side a multiple of 64. CardGen asks for whatever
its aspect-ratio table or a source image implies, so sizes are snapped here
rather than rejected, keeping the requested shape as closely as the grid allows.
"""

from __future__ import annotations

import struct

MAX_SIDE = 2048
GRID = 64


def snap_size(width: int, height: int, *, max_side: int = MAX_SIDE) -> tuple[int, int]:
    width, height = max(1, int(width)), max(1, int(height))
    scale = min(1.0, max_side / max(width, height))
    return tuple(  # type: ignore[return-value]
        max(GRID, min(max_side, round(side * scale / GRID) * GRID)) for side in (width, height)
    )


def requested_size(body: dict, fallback: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """The size a CardGen request names, by width/height or ``WxH``."""
    width, height = body.get("width"), body.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
        return snap_size(int(width), int(height))
    size = body.get("size")
    if isinstance(size, str) and "x" in size:
        left, _, right = size.lower().partition("x")
        if left.strip().isdigit() and right.strip().isdigit():
            return snap_size(int(left), int(right))
    return snap_size(*fallback) if fallback else None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Pixel size from a PNG, JPEG, WebP or GIF header, without decoding it."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if chunk == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 ":
            width, height = struct.unpack("<HH", data[26:30])
            return width & 0x3FFF, height & 0x3FFF
    if data[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            # SOF0..SOF15, excluding DHT (C4), JPG (C8) and DAC (CC).
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return width, height
            index += 2 + length
    return None
