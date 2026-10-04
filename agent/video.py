"""Assemble slides + narration into the final 1080p MP4 with FFmpeg.

Pipeline
--------
1. Each slide becomes a video segment: a slow zoom (Ken Burns) rendered at
   4K then sampled down to 1080p for smooth motion.
2. Segments are concatenated losslessly (identical encoder parameters).
3. Narration WAVs are concatenated and muxed in with fades.
"""
from __future__ import annotations

import json
import logging
import math
import subprocess
from pathlib import Path

log = logging.getLogger("video")

ZMAX = 1.08            # max zoom for Ken Burns
ENCODE = [             # identical for every segment → lossless concat
    "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
    "-crf", "20", "-profile:v", "high", "-level", "4.2",
    "-pix_fmt", "yuv420p", "-r", "30",
]
# Real-footage segments: same codec/profile/level/fps/pix_fmt (concat-safe)
# but no stillimage tune and a slightly higher CRF — sources are already
# compressed web video, and this keeps episode sizes sane.
FOOTAGE_ENCODE = [
    "-c:v", "libx264", "-preset", "veryfast",
    "-crf", "22", "-profile:v", "high", "-level", "4.2",
    "-pix_fmt", "yuv420p", "-r", "30",
]


def _run(cmd: list[str], label: str = "ffmpeg") -> str:
    # stdin=DEVNULL: ffmpeg must NEVER enter its interactive console
    # ("Enter command:" prompt) — on some hosts an inherited stdin pipe
    # makes it block mid-encode forever.
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{label} failed ({proc.returncode}):\n{' '.join(cmd[:12])}…\n"
            f"{proc.stderr[-1200:]}"
        )
    return proc.stdout


def probe_duration(path: Path) -> float:
    out = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ], label="ffprobe").strip()
    return float(out)


def _zoom_expr(frames: int, zoom_in: bool) -> str:
    rate = (ZMAX - 1.0) / max(frames, 1)
    if zoom_in:
        return f"min(1+{rate:.10f}*on,{ZMAX})"
    return f"max({ZMAX}-{rate:.10f}*on,1)"


def _render_segment(png: Path, frames: int, out: Path, *, zoom_in: bool,
                    ken_burns: bool, fade_in: bool, fade_out: bool) -> None:
    dur = frames / 30.0
    chain = []
    if ken_burns:
        z = _zoom_expr(frames, zoom_in)
        chain.append("scale=3840:2160:flags=lanczos")
        chain.append(
            f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s=1920x1080:fps=30"
        )
    else:
        chain.append("scale=1920:1080:flags=lanczos")
    chain.append("format=yuv420p")
    if fade_in:
        chain.append("fade=t=in:st=0:d=0.8")
    if fade_out:
        chain.append(f"fade=t=out:st={max(dur - 1.5, 0):.3f}:d=1.5")

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", "30", "-i", str(png),
        "-filter_complex", "[0:v]" + ",".join(chain) + "[v]",
        "-map", "[v]", "-frames:v", str(frames),
        *ENCODE, "-an", str(out),
    ]
    _run(cmd, label=f"segment {png.name}")


BG_COLOR = "0x070B18"  # episode background for letterboxed clips


def _render_clip_segment(src: Path, seconds: float, out: Path) -> None:
    """Trim + scale + letterbox a real video clip into a 1080p30 segment.

    Skips the first moments of the source (intros), takes `seconds` worth,
    scales it to fit 1920x1080 (padding with the episode background), and
    adds short fades so it blends with the surrounding slides. Encoded with
    the SAME parameters as slide segments → lossless concat downstream.
    """
    try:
        total = probe_duration(src)
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"clip unreadable: {src}")
    seconds = max(1.0, min(seconds, total - 0.5))
    start = max(0.0, min(2.0, total - seconds - 0.5))
    fade_out_st = max(seconds - 0.6, 0)
    vf = (
        "scale=1920:1080:force_original_aspect_ratio=decrease:"
        "flags=lanczos,"
        f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={BG_COLOR},"
        "fps=30,format=yuv420p,"
        "fade=t=in:st=0:d=0.5,"
        f"fade=t=out:st={fade_out_st:.3f}:d=0.6"
    )
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}",
        "-i", str(src),
        "-filter_complex", f"[0:v]{vf}[v]",
        "-map", "[v]", *ENCODE, "-an", str(out),
    ]
    _run(cmd, label=f"clip {src.name}")


def _render_footage_seg(src: Path, start: float, seconds: float,
                        out: Path) -> None:
    """Trim (explicit offset) + scale + letterbox real footage → 1080p30
    segment, encoded with concat-safe parameters (see FOOTAGE_ENCODE)."""
    seconds = max(1.0, seconds)
    fade_out_st = max(seconds - 0.5, 0)
    vf = (
        "scale=1920:1080:force_original_aspect_ratio=decrease:"
        "flags=lanczos,"
        f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={BG_COLOR},"
        "fps=30,format=yuv420p,"  # noqa: E501
        "fade=t=in:st=0:d=0.4,"
        f"fade=t=out:st={fade_out_st:.3f}:d=0.5"
    )
    cmd = [
        "ffmpeg", "-y", "-ss", f"{max(start, 0.0):.2f}",
        "-t", f"{seconds:.2f}", "-i", str(src),
        "-filter_complex", f"[0:v]{vf}[v]",
        "-map", "[v]", *FOOTAGE_ENCODE, "-an", str(out),
    ]
    _run(cmd, label=f"footage {src.name}")


def _concat(files: list[Path], out: Path, kind: str) -> None:
    listfile = out.with_suffix(".concat.txt")
    listfile.write_text(
        "".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8"
    )
    if kind == "video":
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i",
               str(listfile), "-c", "copy", str(out)]
    else:  # wav
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i",
               str(listfile), "-c:a", "pcm_s16le", str(out)]
    _run(cmd, label=f"concat {kind}")
    listfile.unlink(missing_ok=True)


def render_video(slides: list[Path], wavs: list[Path], out_path: Path,
                 ken_burns: bool = True, work_dir: Path | None = None,
                 sources: list | None = None,
                 section_story: dict[int, int] | None = None,
                 footage_segment_seconds: float = 14.0,
                 slide_head_seconds: float = 2.5) -> dict:
    """Assemble the FOOTAGE-FIRST episode: real video + narration → MP4.

    Channel brief: ~90% real moving video, a few short moving slides.
    Per section: a brief Ken-Burns headline slide (the "few moving
    slides"), then real footage segments chopped from the pool until the
    narration is covered. Story sections prefer their story's topical
    footage; generic AI/tech b-roll fills the rest. When the pool runs
    dry the remainder falls back to slides — the QA ratio gate decides
    whether the episode still earns its upload.

    `sources` is a list of footage.FootageSource; `section_story` maps
    section index → 1-based story index (story AND take sections).
    Each source's `segments` is rewritten to the segments ACTUALLY used,
    so downstream attribution matches the rendered video exactly.
    """
    if len(slides) != len(wavs):
        raise ValueError(f"{len(slides)} slides vs {len(wavs)} narration tracks")
    work = work_dir or out_path.parent / "segments"
    work.mkdir(parents=True, exist_ok=True)

    section_story = section_story or {}
    sources = list(sources or [])
    n = len(slides)

    # --- flatten the pool (stable shuffle → variety, not source-order) ----
    import random
    story_pool: dict[int, list[tuple[int, int, float]]] = {}
    generic_pool: list[tuple[int, int, float]] = []
    for si, src in enumerate(sources):
        for gi, (start, secs) in enumerate(src.segments):
            item = (si, gi, start, secs)
            if src.story and src.story in section_story.values():
                story_pool.setdefault(src.story, []).append(item)
            else:
                generic_pool.append(item)
    random.shuffle(generic_pool)
    n_generic = len(generic_pool)
    # cycling generic b-roll is allowed when there is a real rotation
    cycle_ok = n_generic >= 4

    narration_total = sum(probe_duration(w) for w in wavs)
    head_sections = sorted({0, n - 1, *section_story.keys()})
    head_budget = 0.06 * narration_total        # ~94% real footage target
    per_head = max(0.8, min(slide_head_seconds,
                            head_budget / max(len(head_sections), 1)))

    used: dict[tuple[int, int], float] = {}      # (source_idx, seg_idx) -> s
    generic_cursor = 0

    def _next_generic() -> tuple[int, int, float] | None:
        """Fresh generic segment first (shuffled order); cycle the rotation
        when dry. Rendered failures are blacklisted via used=0.0."""
        nonlocal generic_cursor
        for g in generic_pool:
            if (g[0], g[1]) not in used:
                return g
        if cycle_ok:
            g = generic_pool[generic_cursor % n_generic]
            generic_cursor += 1
            return g
        return None

    segments: list[Path] = []
    footage_seconds = 0.0
    slide_seconds = 0.0

    for i, (png, wav) in enumerate(zip(slides, wavs)):
        dur = probe_duration(wav)
        pad = 1.6 if i == n - 1 else 0.0
        total = dur + pad
        story_idx = section_story.get(i)
        parts: list[Path] = []

        # ---- brief headline slide (the "few moving slides") ---------------
        head = min(per_head, max(0.08 * total, 1.2)) if i in head_sections \
            else 0.0
        head = min(head, max(total - 1.0, 0.0))
        if head >= 0.8:
            part_h = work / f"seg_{i:02d}_h.mp4"
            _render_segment(
                png, math.ceil(head * 30), part_h,
                zoom_in=(i % 2 == 0), ken_burns=ken_burns,
                fade_in=(i == 0), fade_out=False)
            parts.append(part_h)
            slide_seconds += head

        # ---- real footage body --------------------------------------------
        body = total - head
        covered = 0.0
        queue: list[tuple[int, int, float]] = []
        if story_idx:
            queue = [q for q in story_pool.get(story_idx, [])
                     if (q[0], q[1]) not in used]
        k = 0
        while body - covered > 1.0:
            take = body - covered
            item = None
            if k < len(queue):
                item = queue[k]
                k += 1
            else:
                item = _next_generic()
            if item is None:
                break
            si, gi, start, secs = item
            clip_len = min(footage_segment_seconds, secs, take)
            if clip_len < 1.0:
                break          # body is (almost) covered — let the slide finish
            part_f = work / f"seg_{i:02d}_f{len(parts):02d}.mp4"
            src = sources[si]
            try:
                _render_footage_seg(src.path, start, clip_len, part_f)
            except Exception as exc:  # noqa: BLE001
                log.info("footage segment failed (%s): %s",
                         src.title[:40], exc)
                part_f.unlink(missing_ok=True)
                used[(si, gi)] = 0.0        # blacklist this segment
                continue
            parts.append(part_f)
            covered += clip_len
            footage_seconds += clip_len
            used[(si, gi)] = used.get((si, gi), 0.0) + clip_len

        # ---- slide remainder when the pool ran dry -------------------------
        # (ALWAYS render the remainder: a gap here would desync the
        # narration — the section video must be exactly head + covered + rest)
        rest = body - covered
        if rest > 0.2:
            part_s = work / f"seg_{i:02d}_s.mp4"
            _render_segment(
                png, math.ceil(rest * 30), part_s,
                zoom_in=(i % 2 == 1), ken_burns=ken_burns,
                fade_in=False, fade_out=(i == n - 1))
            parts.append(part_s)
            slide_seconds += rest

        seg = work / f"seg_{i:02d}.mp4"
        if len(parts) == 1:
            parts[0].replace(seg)
        else:
            _concat(parts, seg, "video")
            for p in parts:
                p.unlink(missing_ok=True)
        segments.append(seg)
        slide_part = (head + rest) if rest > 0.2 else head
        log.info("Segment %02d/%02d: %.1fs = %.1fs slide + %.1fs real "
                 "footage%s", i + 1, n, total, slide_part, covered,
                 f" [story {story_idx}]" if story_idx else "")

    # rewrite each source's segments to what was ACTUALLY used (attribution)
    for si, src in enumerate(sources):
        rebuilt: list[tuple[float, float]] = []
        for gi, (start, secs) in enumerate(src.segments):
            u = used.get((si, gi))
            if u and u >= 1.0:
                rebuilt.append((start, u))
        src.segments = rebuilt

    silent_video = work / "video_silent.mp4"
    _concat(segments, silent_video, "video")

    track = work / "track.wav"
    _concat(wavs, track, "audio")
    audio_dur = probe_duration(track)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fade_start = max(audio_dur - 1.6, 0)
    _run([
        "ffmpeg", "-y", "-i", str(silent_video), "-i", str(track),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-af", (f"apad=pad_dur=1.5,"
                f"afade=t=in:st=0:d=0.25,"
                f"afade=t=out:st={fade_start:.3f}:d=1.55"),
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-movflags", "+faststart",
        str(out_path),
    ], label="final mux")

    final_dur = probe_duration(out_path)
    size_mb = out_path.stat().st_size / 1e6
    ratio = footage_seconds / final_dur if final_dur else 0.0
    log.info("Final video: %.1f min, %.1f MB → %s", final_dur / 60,
             size_mb, out_path)
    log.info("REAL FOOTAGE: %.1f min / %.1f min (%.0f%%) from %d sources",
             footage_seconds / 60, final_dur / 60, 100 * ratio,
             sum(1 for s in sources if s.segments))
    return {"duration": final_dur, "size_mb": round(size_mb, 1),
            "path": str(out_path), "footage_seconds": round(footage_seconds, 1),
            "slide_seconds": round(slide_seconds, 1),
            "footage_ratio": round(ratio, 3),
            "footage_sources": sum(1 for s in sources if s.segments)}
