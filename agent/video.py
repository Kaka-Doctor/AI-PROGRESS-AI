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


def _run(cmd: list[str], label: str = "ffmpeg") -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
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
                 clips: dict | None = None,
                 clip_max_seconds: float = 12.0) -> dict:
    """Assemble slides (+ optional real video clips) + narration → MP4.

    `clips` maps SECTION index (0-based, parallel to `slides`) to a clips.Clip
    (a downloaded legal video). For a section with a clip, the first
    `clip_max_seconds` of its narration play over the real video (trimmed,
    scaled, letterboxed); the rest of the section plays over its Ken Burns
    slide. Sections without a clip are pure Ken Burns, as before.
    """
    if len(slides) != len(wavs):
        raise ValueError(f"{len(slides)} slides vs {len(wavs)} narration tracks")
    work = work_dir or out_path.parent / "segments"
    work.mkdir(parents=True, exist_ok=True)

    clips = clips or {}
    segments: list[Path] = []
    total_frames = 0
    n = len(slides)
    for i, (png, wav) in enumerate(zip(slides, wavs)):
        dur = probe_duration(wav)
        # last slide holds a little longer for the fade-out
        pad = 1.6 if i == n - 1 else 0.0
        total = dur + pad
        seg = work / f"seg_{i:02d}.mp4"
        clip = clips.get(i)

        if clip is not None and total > 3.0:
            # --- real-video section: clip first, slide for the remainder ----
            clip_len = min(clip_max_seconds, clip.duration - 1.0, total - 1.0)
            clip_len = max(clip_len, 1.0)
            part_a = work / f"seg_{i:02d}_a.mp4"
            _render_clip_segment(clip.path, clip_len, part_a)
            rest = total - clip_len
            if rest > 1.2:
                part_b = work / f"seg_{i:02d}_b.mp4"
                frames_b = math.ceil(rest * 30)
                _render_segment(
                    png, frames_b, part_b,
                    zoom_in=(i % 2 == 0), ken_burns=ken_burns,
                    fade_in=False, fade_out=(i == n - 1))
                _concat([part_a, part_b], seg, "video")
                part_a.unlink(missing_ok=True)
                part_b.unlink(missing_ok=True)
            else:
                part_a.replace(seg)
            log.info("Segment %02d/%02d: %.1fs (%.1fs real clip + slide) [%s]",
                     i + 1, n, total, clip_len, clip.title[:40])
        else:
            frames = math.ceil(total * 30)
            _render_segment(
                png, frames, seg,
                zoom_in=(i % 2 == 0),
                ken_burns=ken_burns,
                fade_in=(i == 0),
                fade_out=(i == n - 1),
            )
            log.info("Segment %02d/%02d: %.1fs (%s)", i + 1, n,
                     frames / 30, png.name)

        segments.append(seg)
        total_frames += math.ceil(total * 30)

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
    log.info("Final video: %.1f min, %.1f MB → %s",
             final_dur / 60, size_mb, out_path)
    return {"duration": final_dur, "size_mb": round(size_mb, 1), "path": str(out_path)}
