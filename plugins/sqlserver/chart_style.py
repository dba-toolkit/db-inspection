"""Dependency-light PNG charts for SQL Server inspection reports.

Uses Pillow from the bundled document runtime so report generation never
silently loses charts when matplotlib is unavailable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1344, 520
NAVY = "#163A5F"
BLUE = "#2E74B5"
ORANGE = "#B26A00"
RED = "#9B1C1C"
GREEN = "#276749"
GRID = "#D9E0E8"
TEXT = "#202124"
MUTED = "#66717E"
PALETTE = [BLUE, ORANGE, GREEN, RED, "#6F42C1", "#168AAD"]


def _font(size: int, bold: bool = False):
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


TITLE_FONT = _font(27, True)
LABEL_FONT = _font(17)
SMALL_FONT = _font(15)
LEGEND_FONT = _font(16)


def _canvas(title: str):
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.text((46, 24), title, fill=NAVY, font=TITLE_FONT)
    draw.line((46, 68, WIDTH - 46, 68), fill=GRID, width=2)
    return image, draw


def _short(value: float) -> str:
    value = float(value)
    if value.is_integer() and abs(value) < 1_000:
        return f"{int(value)}"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def line_chart(path: Path, title: str, labels: list[str], series: list[tuple[str, list[float | None]]]) -> None:
    image, draw = _canvas(title)
    left, top, right, bottom = 92, 108, WIDTH - 42, HEIGHT - 72
    values = [float(v) for _, data in series for v in data if v is not None]
    ymax = max(values or [1.0])
    ymin = min(0.0, min(values or [0.0]))
    span = max(ymax - ymin, 1.0)
    ymax += span * 0.12
    for i in range(6):
        y = top + (bottom - top) * i / 5
        value = ymax - (ymax - ymin) * i / 5
        draw.line((left, y, right, y), fill=GRID, width=1)
        draw.text((10, y - 9), _short(value), fill=MUTED, font=SMALL_FONT)
    draw.line((left, top, left, bottom), fill=MUTED, width=2)
    draw.line((left, bottom, right, bottom), fill=MUTED, width=2)
    count = max(len(labels), 1)
    for idx, label in enumerate(labels):
        x = left + (right - left) * idx / max(count - 1, 1)
        if idx == 0 or idx == count - 1 or count <= 8 or idx % max(1, count // 6) == 0:
            draw.text((x - 28, bottom + 13), str(label)[-8:], fill=MUTED, font=SMALL_FONT)
    for sidx, (name, data) in enumerate(series):
        color = PALETTE[sidx % len(PALETTE)]
        points: list[tuple[float, float]] = []
        for idx, value in enumerate(data):
            if value is None:
                continue
            x = left + (right - left) * idx / max(count - 1, 1)
            y = bottom - (float(value) - ymin) / (ymax - ymin) * (bottom - top)
            points.append((x, y))
        if len(points) > 1:
            draw.line(points, fill=color, width=4, joint="curve")
        for x, y in points:
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color)
        lx = left + sidx * 270
        draw.rounded_rectangle((lx, 78, lx + 24, 91), radius=4, fill=color)
        draw.text((lx + 32, 73), name, fill=TEXT, font=LEGEND_FONT)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)


def bar_chart(path: Path, title: str, labels: list[str], values: list[float],
              colors: list[str] | None = None, suffix: str = "") -> None:
    image, draw = _canvas(title)
    left, top, right, bottom = 112, 106, WIDTH - 42, HEIGHT - 92
    maximum = max([float(v) for v in values] or [1.0])
    maximum = max(maximum * 1.18, 1.0)
    for i in range(6):
        x = left + (right - left) * i / 5
        value = maximum * i / 5
        draw.line((x, top, x, bottom), fill=GRID, width=1)
        draw.text((x - 18, bottom + 13), _short(value), fill=MUTED, font=SMALL_FONT)
    count = max(len(labels), 1)
    slot = (bottom - top) / count
    for idx, (label, value) in enumerate(zip(labels, values)):
        y1 = top + slot * idx + slot * 0.18
        y2 = top + slot * (idx + 1) - slot * 0.18
        x2 = left + (right - left) * float(value) / maximum
        color = (colors or PALETTE)[idx % len(colors or PALETTE)]
        draw.rounded_rectangle((left, y1, max(left + 2, x2), y2), radius=5, fill=color)
        rendered = str(label)
        if len(rendered) > 20:
            rendered = rendered[:18] + "…"
        draw.text((8, y1 + max(0, (y2 - y1 - 18) / 2)), rendered, fill=TEXT, font=SMALL_FONT)
        draw.text((min(x2 + 8, right - 78), y1 + max(0, (y2 - y1 - 18) / 2)), _short(value) + suffix, fill=TEXT, font=SMALL_FONT)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
