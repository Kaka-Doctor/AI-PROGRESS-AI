"""Render the episode slides (1920x1080) and the channel's EPIC thumbnail
(1280x720) — a bold, high-contrast, clickable design.

Slide kinds (one PNG per script section, Ken-Burns animated by video.py):
  * title card  — channel brand + date + top-story hook
  * story card  — giant Anton headline + source badge + on-screen chips
  * take card   — "WHY IT MATTERS" analysis beat
  * outro card  — subscribe CTA
  * image card  — article og:image full-bleed with a lower-third headline
    (only when broll.py actually downloaded a real article image)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from . import theme as T
from .config import Settings
from .news import Story
from .scriptgen import Script

log = logging.getLogger("slides")

W, H = 1920, 1080


def _channel_bar(dr: ImageDraw.ImageDraw, x: int, y: int, scale: float = 1.0,
                 on_dark: bool = True) -> None:
    """'AI PROGRESS' yellow chip + 'DAILY' text — the channel mark."""
    f = T.body(int(26 * scale), weight=700)
    T.chip(dr, (x, y), "AI PROGRESS", f, T.YELLOW)
    bb = f.getbbox("AI PROGRESS")
    dr.text((x + bb[2] + 58 * scale, y + 2), "DAILY",
            font=T.body(int(26 * scale), weight=700),
            fill=T.WHITE if on_dark else T.BG_TOP)


def _date_str() -> str:
    return datetime.now(timezone.utc).strftime("%B %d, %Y")


class SlideRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.size = (settings.width, settings.height)

    # ------------------------------------------------------------------ title
    def _title_card(self, script: Script, stories: list[Story]) -> Image.Image:
        img = T.studio_bg(self.size, seed=1)
        dr = ImageDraw.Draw(img)
        _channel_bar(dr, 110, 96)

        date_f = T.body(34, weight=600)
        dr.text((110, 200), _date_str().upper(), font=date_f, fill=T.CYAN)

        head = (script.hook or stories[0].title).upper()
        f = T.fit_font(T.DISPLAY_FONT, head, W - 220, 168, min_size=64)
        lines = T.wrap_text(head, f, W - 220)[:3]
        y = 330
        for i, line in enumerate(lines):
            color = T.WHITE if i % 2 == 0 else T.YELLOW
            T.draw_text(dr, (110, y), line, f, fill=color,
                        stroke_width=3, stroke_fill=(0, 0, 0),
                        shadow=(6, 8))
            y += int(f.size * 1.12)

        badge = T.body(30, weight=700)
        T.chip(dr, (110, H - 150), f"TOP {len(stories)} AI STORIES", badge,
               T.MAGENTA, text_color=T.WHITE)
        T.rounded(dr, (470, H - 150, 810, H - 82), radius=18,
                  fill=(16, 20, 46), outline=T.CYAN, width=3)
        dr.text((492, H - 138), "US + CHINA LABS", font=T.body(30, weight=700),
                fill=T.WHITE)
        return img

    # ------------------------------------------------------------------ story
    def _story_card(self, title: str, story: Story | None,
                    index: int, total: int,
                    on_screen: list[str]) -> Image.Image:
        img = T.studio_bg(self.size, seed=index * 31 + 7)
        dr = ImageDraw.Draw(img)

        # left accent rail
        dr.rectangle([0, 0, 14, H], fill=T.CYAN)

        badge = T.body(30, weight=700)
        T.chip(dr, (110, 88), f"STORY {index} / {total}", badge, T.CYAN)
        if story is not None:
            src_f = T.body(28, weight=600)
            text = story.source.upper()
            if story.official:
                text += "  •  OFFICIAL"
            bb = src_f.getbbox(f"STORY {index} / {total}")
            dr.text((110 + bb[2] + 46, 96), text, font=src_f, fill=T.GREY)

        head = (title or "Top Story").upper()
        f = T.fit_font(T.DISPLAY_FONT, head, W - 260, 148, min_size=56)
        lines = T.wrap_text(head, f, W - 260)[:4]
        y = 260
        for line in lines:
            T.draw_text(dr, (120, y), line, f, fill=T.WHITE,
                        stroke_width=3, stroke_fill=(0, 0, 0), shadow=(5, 7))
            y += int(f.size * 1.1)

        if story is not None:
            when = T.body(26, weight=500)
            dr.text((122, y + 26), story.domain.upper() + "   "
                    + story.published.strftime("%b %d, %H:%M UTC"),
                    font=when, fill=T.GREY)

        # on-screen chips along the bottom
        cf = T.body(30, weight=700)
        x = 110
        for phrase in on_screen[:3]:
            bb = cf.getbbox(phrase.upper())
            if x + bb[2] + 40 > W - 60:
                break
            T.rounded(dr, (x, H - 130, x + bb[2] + 44, H - 62), radius=18,
                      outline=T.CYAN, width=3)
            dr.text((x + 22, H - 118), phrase.upper(), font=cf, fill=T.WHITE)
            x += bb[2] + 44 + 22
        return img

    # ------------------------------------------------------------------- take
    def _take_card(self, title: str, on_screen: list[str]) -> Image.Image:
        img = T.gradient_bg(self.size, T.BG_TOP, (24, 8, 38))
        T.glow_orb(img, (int(W * 0.85), int(H * 0.2)), int(H * 0.5),
                   T.MAGENTA, alpha=52)
        T.tech_grid(img, alpha=12)
        dr = ImageDraw.Draw(img)
        dr.rectangle([0, 0, 14, H], fill=T.MAGENTA)

        T.chip(dr, (110, 96), "WHY IT MATTERS", T.body(30, weight=700),
               T.MAGENTA, text_color=T.WHITE)

        head = (title or "The Big Picture").upper()
        f = T.fit_font(T.DISPLAY_FONT, head, W - 260, 110, min_size=44)
        lines = T.wrap_text(head, f, W - 260)[:2]
        y = 220
        for line in lines:
            T.draw_text(dr, (120, y), line, f, fill=T.WHITE, shadow=(5, 7))
            y += int(f.size * 1.12)

        # decorative giant quote glyph on the right
        qf = T.display(360)
        dr.text((W - 430, 160), "\u201c", font=qf, fill=(96, 22, 66))

        # takeaway phrases as big glowing bars, not lazy bullets
        phrases = (on_screen or ["The race shifts again"])[:4]
        y = max(y + 40, 470)
        for k, phrase in enumerate(phrases):
            bf = T.fit_font(T.BODY_FONT, phrase.upper(), W - 560, 56,
                            min_size=30)
            bh = int(bf.size * 1.7)
            accent = [T.CYAN, T.YELLOW, T.MAGENTA, T.GREEN][k % 4]
            dr.rectangle([150, y, 176, y + bh], fill=accent)
            T.rounded(dr, (176, y, W - 300, y + bh), radius=14,
                      fill=(14, 17, 42), outline=accent, width=2)
            dr.text((214, y + bh // 2 - int(bf.size * 0.62)), phrase.upper(),
                    font=bf, fill=T.WHITE)
            y += bh + 26
        return img

    # ------------------------------------------------------------------ image
    def _image_card(self, headline: str, image_path: Path,
                    source: str) -> Image.Image:
        """Full-bleed article image with a dark lower-third + headline."""
        base = Image.open(image_path).convert("RGB")
        base = ImageEnhance.Contrast(base).enhance(1.06)
        base = ImageEnhance.Color(base).enhance(1.12)
        # cover-crop to 16:9
        bw, bh = base.size
        target = W / H
        if bw / bh > target:
            nw = int(bh * target)
            base = base.crop(((bw - nw) // 2, 0, (bw + nw) // 2, bh))
        else:
            nh = int(bw / target)
            top = max(0, (bh - nh) // 3)
            base = base.crop((0, top, bw, top + nh))
        img = base.resize((W, H), Image.LANCZOS)

        # bottom gradient band
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dr = ImageDraw.Draw(overlay)
        for y in range(int(H * 0.55), H):
            t = (y - H * 0.55) / (H * 0.45)
            a = int(210 * t ** 1.4)
            dr.line([(0, y), (W, y)], fill=(4, 6, 16, a))
        # top scrim for the source badge
        for y in range(0, int(H * 0.16)):
            a = int(120 * (1 - y / (H * 0.16)))
            dr.line([(0, y), (W, y)], fill=(4, 6, 16, a))
        img.paste(Image.alpha_composite(img.convert("RGBA"),
                                        overlay).convert("RGB"))
        dr = ImageDraw.Draw(img)

        dr.rectangle([0, 0, 14, H], fill=T.CYAN)
        T.chip(dr, (110, 70), source.upper()[:26], T.body(28, weight=700),
               T.CYAN)

        head = headline.upper()
        f = T.fit_font(T.DISPLAY_FONT, head, W - 260, 118, min_size=46)
        lines = T.wrap_text(head, f, W - 260)[:3]
        y = H - 150 - int(len(lines) * f.size * 1.1)
        for line in lines:
            T.draw_text(dr, (120, y), line, f, fill=T.WHITE,
                        stroke_width=3, stroke_fill=(0, 0, 0), shadow=(4, 6))
            y += int(f.size * 1.1)
        return img

    # ------------------------------------------------------------------ outro
    def _outro_card(self) -> Image.Image:
        img = T.studio_bg(self.size, seed=99)
        dr = ImageDraw.Draw(img)
        _channel_bar(dr, 110, 96, scale=1.2)

        f = T.fit_font(T.DISPLAY_FONT, "STAY AHEAD", W - 220, 210, min_size=80)
        T.draw_text(dr, (110, 420), "STAY AHEAD", f, fill=T.WHITE,
                    stroke_width=4, stroke_fill=(0, 0, 0), shadow=(7, 9))
        f2 = T.fit_font(T.DISPLAY_FONT, "OF THE CURVE", W - 220, 210,
                        min_size=80)
        T.draw_text(dr, (110, 640), "OF THE CURVE", f2, fill=T.YELLOW,
                    stroke_width=4, stroke_fill=(0, 0, 0), shadow=(7, 9))

        cf = T.body(34, weight=700)
        T.chip(dr, (110, 920), "SUBSCRIBE + NOTIFICATIONS ON", cf, T.RED,
               text_color=T.WHITE)
        return img

    # ------------------------------------------------------------- public API
    def render_all(self, script: Script, stories: list[Story],
                   work_dir: Path) -> list[Path]:
        work_dir.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []

        def save(img: Image.Image, name: str) -> Path:
            p = work_dir / name
            img.save(p, "PNG")
            out.append(p)
            return p

        save(self._title_card(script, stories), "slide_000.png")

        story_total = sum(1 for s in script.sections if s.type == "story")
        story_idx = 0
        for i, sec in enumerate(script.sections[1:], start=1):
            if sec.type == "intro":
                continue
            story = (stories[sec.story_index - 1]
                     if 0 < sec.story_index <= len(stories) else None)
            if sec.type == "story":
                story_idx += 1
                img = self._story_card(sec.title, story, story_idx,
                                       max(story_total, 1), sec.on_screen)
                # if a real article image exists, prefer the image card
                img_path = getattr(sec, "image_path", None)
                if img_path and story is not None:
                    try:
                        img = self._image_card(sec.title, Path(img_path),
                                               story.source)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("image card failed (%s); using slide",
                                    exc)
                save(img, f"slide_{i:03d}.png")
            elif sec.type == "take":
                save(self._take_card(sec.title, sec.on_screen),
                     f"slide_{i:03d}.png")
            else:  # outro
                save(self._outro_card(), f"slide_{i:03d}.png")
        log.info("Rendered %d slides", len(out))
        return out

    # ------------------------------------------------------------- thumbnail
    def render_thumbnail(self, script: Script, stories: list[Story],
                         out_path: Path) -> Path:
        """THE epic thumbnail: huge condensed type, glow, arrow, badges."""
        TW, TH = 1280, 720
        img = T.gradient_bg((TW, TH), (4, 5, 14), (16, 10, 44))
        T.glow_orb(img, (int(TW * 0.18), int(TH * 0.30)), int(TH * 0.85),
                   T.CYAN, alpha=70)
        T.glow_orb(img, (int(TW * 0.86), int(TH * 0.78)), int(TH * 0.75),
                   T.MAGENTA, alpha=60)
        T.tech_grid(img, alpha=22, spacing=80)
        dr = ImageDraw.Draw(img)

        # top badges
        T.chip(dr, (36, 30), "AI NEWS", T.body(30, weight=800), T.RED,
               text_color=T.WHITE, pad=(22, 10))
        date_chip = T.body(26, weight=700)
        T.chip(dr, (250, 34), datetime.now(timezone.utc).strftime("%b %d")
               .upper(), date_chip, T.CYAN, text_color=(4, 6, 16),
               pad=(18, 10))

        # giant headline — 2 lines, white then yellow
        text = (script.thumbnail_text or "AI JUST LEVELED UP").upper()
        words = text.split()
        if len(words) >= 3:
            mid = len(words) // 2
            lines = [" ".join(words[:mid]), " ".join(words[mid:])]
        else:
            lines = [text, ""]
        sizes = []
        for line in lines:
            if not line:
                sizes.append(None)
                continue
            f = T.fit_font(T.DISPLAY_FONT, line, TW - 300, 190, min_size=70)
            sizes.append(f)
        active = [f for f in sizes if f]
        base_size = min((f.size for f in active), default=120)
        y = 150
        for line, f in zip(lines, sizes):
            if not line:
                continue
            f = T.font(T.DISPLAY_FONT, min(f.size, base_size + 18))
            color = T.WHITE if y < TH // 2 else T.YELLOW
            T.draw_text(dr, (60, y), line, f, fill=color,
                        stroke_width=max(6, f.size // 14),
                        stroke_fill=(0, 0, 0), shadow=(10, 12))
            y += int(f.size * 1.16)

        # stylized double chevrons slamming toward the headline
        # (back red chevron first, then the front yellow one on top)
        cx, cy = TW - 190, TH // 2
        for dx, color in [(86, T.RED), (0, T.YELLOW)]:
            chev = [(cx + dx, cy - 130), (cx + dx + 64, cy - 130),
                    (cx + dx + 118, cy), (cx + dx + 64, cy + 130),
                    (cx + dx, cy + 130), (cx + dx + 54, cy)]
            dr.polygon(chev, fill=color)
            dr.polygon([(x + 4, y + 4) for x, y in chev],
                       outline=(255, 255, 255))

        # story-count badge bottom-left + channel mark bottom-right
        T.chip(dr, (36, TH - 96), f"{len(stories)} BIG STORIES",
               T.body(28, weight=800), T.YELLOW, pad=(20, 10))
        mark = T.body(26, weight=800)
        T.chip(dr, (TW - 300, TH - 96), "AI PROGRESS", mark, T.WHITE,
               pad=(16, 9))

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "PNG")
        log.info("Thumbnail rendered -> %s", out_path)
        return out_path
