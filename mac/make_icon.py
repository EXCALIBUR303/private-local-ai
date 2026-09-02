"""
Draw the application icon.

A tiny PNG encoder (zlib + struct) keeps the build free of Pillow. The mark is
the same amber core the interface uses, so the Dock icon and the app agree.
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

BG_TOP = (0x16, 0x14, 0x1B)
BG_BOT = (0x0A, 0x09, 0x0D)
AMBER = (0xF0, 0xA1, 0x3C)
SS = 3  # supersampling factor


def write_png(path: Path, w: int, h: int, rgba: bytearray) -> None:
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)                       # filter type 0
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def rounded_alpha(x: float, y: float, size: float, radius: float) -> bool:
    """Point-in-rounded-square test, in icon space."""
    inset = size * 0.055                    # macOS icons breathe inside the grid
    lo, hi = inset, size - inset
    if not (lo <= x <= hi and lo <= y <= hi):
        return False
    r = radius
    cx = min(max(x, lo + r), hi - r)
    cy = min(max(y, lo + r), hi - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def draw(size: int) -> bytearray:
    """Render one icon at `size` pixels, supersampled then box-filtered."""
    S = size * SS
    radius = S * 0.205
    cx = cy = S / 2
    R = S * 0.30

    acc = bytearray(S * S * 4)
    for py in range(S):
        for px in range(S):
            i = (py * S + px) * 4
            if not rounded_alpha(px + 0.5, py + 0.5, S, radius):
                continue
            t = py / S
            r = int(BG_TOP[0] * (1 - t) + BG_BOT[0] * t)
            g = int(BG_TOP[1] * (1 - t) + BG_BOT[1] * t)
            b = int(BG_TOP[2] * (1 - t) + BG_BOT[2] * t)
            acc[i:i + 4] = bytes((r, g, b, 255))

    def blend(px: int, py: int, colour, alpha: float) -> None:
        if not (0 <= px < S and 0 <= py < S) or alpha <= 0:
            return
        i = (py * S + px) * 4
        if acc[i + 3] == 0:
            return                           # stay inside the rounded square
        a = min(1.0, alpha)
        for k in range(3):
            acc[i + k] = int(acc[i + k] * (1 - a) + colour[k] * a)

    # centre glow
    glow = R * 0.95
    for py in range(int(cy - glow), int(cy + glow) + 1):
        for px in range(int(cx - glow), int(cx + glow) + 1):
            d = math.hypot(px - cx, py - cy)
            if d < glow:
                blend(px, py, AMBER, 0.55 * (1 - d / glow) ** 2.4)

    # two orbiting arcs and a bezel, matching the interface's core
    def arc(radius_px: float, start: float, extent: float, width: float, alpha: float):
        steps = max(64, int(radius_px * extent * 1.6))
        for s in range(steps):
            a = start + extent * (s / steps)
            for w in range(int(-width), int(width) + 1):
                rr = radius_px + w * 0.5
                blend(int(cx + math.cos(a) * rr), int(cy + math.sin(a) * rr),
                      AMBER, alpha * (1 - abs(w) / (width + 1)))

    arc(R * 1.34, math.radians(205), math.radians(160), S * 0.010, 0.95)
    arc(R * 1.00, math.radians(30), math.radians(150), S * 0.010, 0.85)
    arc(R * 1.62, math.radians(70), math.radians(70), S * 0.008, 0.45)

    core = R * 0.20
    for py in range(int(cy - core) - 1, int(cy + core) + 2):
        for px in range(int(cx - core) - 1, int(cx + core) + 2):
            d = math.hypot(px - cx, py - cy)
            if d <= core:
                blend(px, py, (255, 240, 214), min(1.0, (core - d) * 1.2))

    # box-filter down to the requested size
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(SS):
                for dx in range(SS):
                    j = ((y * SS + dy) * S + (x * SS + dx)) * 4
                    r += acc[j]; g += acc[j + 1]; b += acc[j + 2]; a += acc[j + 3]
            n = SS * SS
            i = (y * size + x) * 4
            out[i:i + 4] = bytes((r // n, g // n, b // n, a // n))
    return out


def main(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    plan = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
            (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
            (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]
    seen: dict[int, bytearray] = {}
    for size, name in plan:
        if size not in seen:
            seen[size] = draw(size)
            print(f"  drew {size}px", flush=True)
        write_png(dest / f"icon_{name}.png", size, size, seen[size])


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "mac/AppIcon.iconset"))
