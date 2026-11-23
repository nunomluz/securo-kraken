"""Writes icon.png and logos/kraken.png: an original mark (a deep-indigo
rounded square with a white stylised wave). Standard library only.

Run from the repo root: python tools/make_icons.py
"""

import math
import struct
import zlib
from pathlib import Path

SIZE = 256
SS = 4  # samples per axis for smooth edges
INDIGO = (62, 28, 140)
WHITE = (255, 255, 255)
RADIUS = 52
MID = 128.0
AMPLITUDE = 30.0
THICKNESS = 17.0  # half the stroke width
LEFT, RIGHT = 52.0, 204.0


def _inside_rounded(x: float, y: float) -> bool:
    cx = min(max(x, RADIUS), SIZE - RADIUS)
    cy = min(max(y, RADIUS), SIZE - RADIUS)
    return (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS**2


def _wave_y(x: float, shift: float) -> float:
    return MID + shift + AMPLITUDE * math.sin((x - LEFT) / (RIGHT - LEFT) * 2 * math.pi)


def _in_wave(x: float, y: float) -> bool:
    if not LEFT <= x <= RIGHT:
        return False
    return any(abs(y - _wave_y(x, shift)) <= THICKNESS * 0.6 for shift in (-30.0, 30.0))


def _pixel(px: int, py: int) -> tuple[int, int, int, int]:
    covered = wave = 0
    for sy in range(SS):
        for sx in range(SS):
            x = px + (sx + 0.5) / SS
            y = py + (sy + 0.5) / SS
            if _inside_rounded(x, y):
                covered += 1
                wave += _in_wave(x, y)
    if not covered:
        return 0, 0, 0, 0
    mix = wave / covered
    rgb = tuple(round(INDIGO[i] * (1 - mix) + WHITE[i] * mix) for i in range(3))
    return (*rgb, round(255 * covered / (SS * SS)))


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def png_bytes() -> bytes:
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            rows.extend(_pixel(x, y))
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    data = png_bytes()
    (root / "logos").mkdir(exist_ok=True)
    for target in ("icon.png", "logos/kraken.png"):
        (root / target).write_bytes(data)
        print(f"wrote {target} ({len(data)} bytes)")
