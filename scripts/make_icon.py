"""Draw the LLM Control Room icon (a pulse line on a blue tile) into assets/llm-control-room.ico.

Standard library only: pixels are computed here and stored as PNG entries inside the .ico.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SS = 4
PTS = [(0.22, 0.60), (0.38, 0.60), (0.46, 0.36), (0.56, 0.74), (0.64, 0.50), (0.78, 0.50)]


def seg_dist(px, py, a, b):
    ax, ay, bx, by = *a, *b
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def pixel(x, y, s):
    r = s * 0.22
    cx, cy = min(max(x, r), s - r), min(max(y, r), s - r)
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return None
    pts = [(a * s, b * s) for a, b in PTS]
    if min(seg_dist(x, y, pts[i], pts[i + 1]) for i in range(len(pts) - 1)) <= s * 0.045:
        return (255, 255, 255)
    if math.hypot(x - s * 0.5, y - s * 0.2) <= s * 0.05:
        return (255, 255, 255)
    return (47, 93, 216)


def render(size):
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = pixel(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS, size)
                    if c:
                        acc[0] += c[0]
                        acc[1] += c[1]
                        acc[2] += c[2]
                        acc[3] += 1
            n = SS * SS
            if acc[3]:
                row += bytes(
                    [acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], 255 * acc[3] // n]
                )
            else:
                row += bytes(4)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main():
    sizes = [16, 32, 48, 64, 128, 256]
    imgs = [render(s) for s in sizes]
    out = Path(__file__).resolve().parent.parent / "assets"
    out.mkdir(exist_ok=True)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    ents = b""
    for s, d in zip(sizes, imgs, strict=True):
        ents += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(d), off)
        off += len(d)
    (out / "llm-control-room.ico").write_bytes(head + ents + b"".join(imgs))
    (out / "llm-control-room.png").write_bytes(imgs[-1])


main()
