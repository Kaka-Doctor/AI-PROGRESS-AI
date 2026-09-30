"""Neon-tech visual identity for the AI Progress AI channel.

Dark studio background with electric cyan / hot magenta glow, a subtle
tech grid, ultra-bold Anton display type, and high-contrast accents.
Pure Pillow — renders identically locally and on GitHub Actions.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import FONTS_DIR

# --- Palette ---------------------------------------------------------------
BG_TOP = (5, 7, 18)          # near-black navy
BG_BOTTOM = (13, 16, 42)     # deep indigo
CYAN = (0, 229, 255)         # electric cyan — primary accent
MAGENTA = (255, 45, 149)     # hot magenta — secondary accent
PURPLE = (124, 77, 255)      # deep purple — fills
YELLOW = (255, 214, 0)       # highlight — big numbers / badges
WHITE = (245, 247, 255)
GREY = (148, 155, 180)
RED = (255, 61, 61)          # breaking badge
GREEN = (57, 255, 163)       # "live" dot

DISPLAY_FONT = FONTS_DIR / "Anton-Regular.ttf"
BODY_FONT = FONTS_DIR / "Inter.ttf"


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def display(size: int) -> ImageFont.FreeTypeFont:
    return font(DISPLAY_FONT, size)


def body(size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    f = font(BODY_FONT, size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except Exception:  # noqa: BLE001
            pass
    return f


# ---------------------------------------------------------------------------
# Background builders
# ---------------------------------------------------------------------------

def gradient_bg(size: tuple[int, int], top: tuple = BG_TOP,
                bottom: tuple = BG_BOTTOM) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, top)
    dr = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        dr.line([(0, y), (w, y)], fill=color)
    return img


def glow_orb(img: Image.Image, center: tuple[int, int], radius: int,
             color: tuple, alpha: int = 90) -> None:
    """Soft radial glow — drawn on an RGBA overlay and composited."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(overlay)
    steps = 28
    for i in range(steps, 0, -1):
        r = int(radius * i / steps)
        a = int(alpha * (1 - i / steps) ** 2)
        dr.ellipse([center[0] - r, center[1] - r,
                    center[0] + r, center[1] + r],
                   fill=color + (a,))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius // 10))
    base = img.convert("RGBA")
    base.alpha_composite(overlay)
    img.paste(base.convert(img.mode))


def tech_grid(img: Image.Image, alpha: int = 16,
              spacing: int = 120) -> None:
    """Faint horizon-style grid = newsroom/tech backdrop."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(overlay)
    w, h = img.size
    for x in range(0, w + spacing, spacing):
        dr.line([(x, 0), (x, h)], fill=CYAN + (alpha,), width=1)
    for y in range(0, h + spacing, spacing):
        dr.line([(0, y), (w, y)], fill=CYAN + (alpha,), width=1)
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))


def studio_bg(size: tuple[int, int], seed: int = 0) -> Image.Image:
    """The channel's signature backdrop: gradient + grid + twin glows."""
    img = gradient_bg(size).convert("RGBA")
    w, h = size
    rng = (seed * 7919 + 17) % 360
    glow_orb(img, (int(w * 0.16), int(h * (0.28 + 0.06 * math.sin(rng)))),
             int(h * 0.62), CYAN, alpha=60)
    glow_orb(img, (int(w * 0.88), int(h * 0.75)),
             int(h * 0.55), MAGENTA, alpha=46)
    out = img.convert("RGB")
    tech_grid(out)
    return out


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def fit_font(path: Path, text: str, max_width: int, start_size: int,
              min_size: int = 24) -> ImageFont.FreeTypeFont:
    size = start_size
    while size > min_size:
        f = font(path, size)
        if f.getbbox(text)[2] <= max_width:
            return f
        size -= 4
    return font(path, min_size)


def draw_text(dr: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
              f: ImageFont.FreeTypeFont, fill=WHITE,
              stroke_width: int = 0, stroke_fill=(0, 0, 0),
              shadow: tuple[int, int] | None = None,
              anchor: str = "la") -> None:
    x, y = xy
    if shadow:
        dr.text((x + shadow[0], y + shadow[1]), text, font=f,
                fill=(0, 0, 0), anchor=anchor)
    dr.text((x, y), text, font=f, fill=fill, anchor=anchor,
            stroke_width=stroke_width, stroke_fill=stroke_fill)


def wrap_text(text: str, f: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if f.getbbox(trial)[2] <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def rounded(dr: ImageDraw.ImageDraw, box, radius: int, fill=None,
            outline=None, width: int = 2) -> None:
    dr.rounded_rectangle(box, radius=radius, fill=fill, outline=outline,
                         width=width)


def chip(dr: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
         f: ImageFont.FreeTypeFont, fill, text_color=(5, 7, 18),
         pad: tuple[int, int] = (18, 8)) -> None:
    """Small rounded badge with bold text inside."""
    x, y = xy
    bb = f.getbbox(text)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    box = (x, y, x + tw + pad[0] * 2, y + th + pad[1] * 2 + 4)
    rounded(dr, box, radius=(box[3] - box[1]) // 2, fill=fill)
    dr.text((x + pad[0], y + pad[1] + 2), text, font=f, fill=text_color)
