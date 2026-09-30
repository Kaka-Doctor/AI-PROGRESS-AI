"""Orchestrate one episode run: news → script → clips → slides → voice →
video → upload.

The channel posts TWICE A DAY (every ~12 hours). Run manually:
    python -m agent.main                     # next episode (dry state safe)
    python -m agent.main --no-upload         # render only (dry run)
    python -m agent.main --force             # bypass the 12-hour guard
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import broll, clips as clips_mod, qa, state as state_mod
from .config import OUTPUT_DIR, ROOT, Settings, WORK_DIR
from .news import collect_stories
from .scriptgen import generate_script
from .slides import SlideRenderer
from .tts import synth_sections
from .video import probe_duration, render_video

log = logging.getLogger("main")


def _banner(title: str) -> None:
    log.info("=" * 62)
    log.info(title)
    log.info("=" * 62)


def build_tags(stories: list, settings: Settings) -> list[str]:
    tags = [
        "AI news", "artificial intelligence", "AI", "machine learning",
        "daily AI news", "OpenAI", "Anthropic", "Google DeepMind",
        "DeepSeek", "AI progress", settings.channel_name,
    ]
    for s in stories[:3]:
        for word in s.title.split():
            w = word.strip(".,:;!?\"'()[]")
            if 4 <= len(w) <= 24 and w.isalnum():
                if w.lower() not in {"with", "from", "this", "that", "just",
                                     "into", "over", "will", "what"}:
                    tags.append(w)
    out, used = [], 0
    for t in tags:
        if used + len(t) + 1 > 480:
            break
        out.append(t)
        used += len(t) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Progress AI agent")
    parser.add_argument("--no-upload", action="store_true",
                        help="render the video but do not upload")
    parser.add_argument("--keep-state", action="store_true",
                        help="do not advance state.json")
    parser.add_argument("--force", action="store_true",
                        help="bypass the 12-hour cadence guard")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = Settings.from_env()
    if args.no_upload:
        settings.dry_run = True
    if args.keep_state:
        settings.keep_state = True
    if args.force:
        settings.force_upload = True

    started = time.time()
    st = state_mod.load(settings=settings)
    _banner(f"AI PROGRESS — {settings.channel_name} (2x daily)")

    # 1b. TWICE-A-DAY GUARD -----------------------------------------------
    if not settings.dry_run and not settings.force_upload:
        allowed, why = state_mod.cadence_allows_post(st, settings)
        if not allowed:
            lv = st.get("last_video") or {}
            log.info("CADENCE GUARD: %s — skipping this run. The channel "
                     "posts every ~12 hours; the next scheduled slot picks "
                     "up whatever news this slot would have covered. Use "
                     "--force / FORCE_UPLOAD to override.", why)
            return 0
        log.info("CADENCE GUARD: %s", why)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Gather the news ---------------------------------------------------
    # Primary window (36h) first; widen to 72h when the fresh supply is thin
    # so news missed by earlier episodes still gets covered.
    ranked = collect_stories(settings)
    if len(ranked) < settings.min_stories + 2:
        log.warning("Only %d stories in the %dh window — widening to %dh "
                    "so missed news still gets covered.", len(ranked),
                    settings.news_window_hours,
                    settings.news_window_wide_hours)
        ranked = collect_stories(
            settings, window_hours=settings.news_window_wide_hours)
    if len(ranked) < settings.min_stories:
        log.error("Only %d fresh stories found (need %d) — refusing to "
                  "build a thin episode. Try again next slot.",
                  len(ranked), settings.min_stories)
        qa.write_report(out_dir / "qa_report.json", [
            qa.Check("news_supply", False,
                     f"{len(ranked)} fresh stories (minimum {settings.min_stories})")],
            stage="news")
        return 1

    # 1b. Prefer stories NEVER covered before (missed-news coverage),
    # exclude anything covered in the last DEDUP_LOOKBACK_HOURS; only top
    # up with already-covered stories (as explicit FOLLOW-UPs) when the
    # fresh supply can't fill the episode.
    known = state_mod.known_fingerprints(st, settings.dedup_lookback_hours)
    fresh = [s for s in ranked if s.fingerprint not in known]
    if len(fresh) >= settings.min_stories:
        stories = fresh[:settings.max_stories]
        log.info("Story mix: %d brand-new stories (nothing repeated).",
                 len(stories))
    else:
        covered = [s for s in ranked if s.fingerprint in known]
        stories = (fresh + covered)[:settings.max_stories]
        log.warning("Story mix: %d new + %d FOLLOW-UPs (already covered "
                    "within %dh — the script will frame them as updates, "
                    "not repeats).", len(fresh),
                    max(0, len(stories) - len(fresh)),
                    settings.dedup_lookback_hours)
    follow_ups = {i for i, s in enumerate(stories, 1)
                  if s.fingerprint in known}

    for i, s in enumerate(stories, 1):
        log.info("STORY %d (score %.0f): %s — %s", i, s.score, s.title, s.source)

    # 2. B-roll images (best effort) ----------------------------------------
    images = broll.fetch_all(stories, settings, out_dir / "broll")

    # 2b. Legal video clips (YouTube CC / Wikimedia / Internet Archive) -----
    story_clips = clips_mod.find_clips(stories, settings, out_dir / "clips")

    # 3. Write the script (+ STORM RESILIENCE) ------------------------------
    rounds = max(1, settings.script_retry_rounds)
    script_checks = []
    for rnd in range(1, rounds + 1):
        script = generate_script(stories, settings, follow_ups)
        log.info("Script ready — round %d/%d (%d words, %d sections, "
                 "source=%s)", rnd, rounds, script.word_count,
                 len(script.sections), script.source)
        script_checks = qa.check_script(script, stories, settings)
        if all(c.passed for c in script_checks):
            break
        if not settings.gemini_api_key and script.source == "template":
            # No AI backend at all → the template is deterministic; retrying
            # would produce the identical script. Fail fast instead of
            # sleeping pointlessly.
            log.error("SCRIPT QA failed on a deterministic template with no "
                      "AI backend configured — aborting (no retry).")
            break
        fails = "; ".join(f"{c.name} ({c.detail})" for c in script_checks
                          if not c.passed)
        if rnd < rounds:
            wait_min = settings.storm_wait_seconds // 60
            log.error("SCRIPT QA failed on round %d/%d: %s — the AI backend "
                      "is likely storming. Waiting %d minutes for it to "
                      "recover, then retrying.", rnd, rounds, fails, wait_min)
            time.sleep(settings.storm_wait_seconds)
        else:
            log.error("SCRIPT QA failed on every round: %s", fails)

    # 3b. SCRIPT QUALITY GATE ------------------------------------------------
    qa.log_checks("SCRIPT QA", script_checks)
    if not all(c.passed for c in script_checks):
        qa.write_report(out_dir / "qa_report.json", script_checks,
                        stage="script", script_words=script.word_count,
                        script_source=script.source,
                        stories=[s.title for s in stories])
        for c in script_checks:
            if not c.passed:
                log.error("SCRIPT QA FAIL — %s: %s", c.name, c.detail)
        log.error("The script does not meet the channel guidelines — "
                  "refusing to render or upload. The next scheduled slot "
                  "will retry.")
        return 1

    # 4. Slides --------------------------------------------------------------
    work = WORK_DIR
    if work.exists():
        shutil.rmtree(work)
    renderer = SlideRenderer(settings)
    # attach downloaded b-roll images to their story sections
    for sec in script.sections:
        if sec.type == "story" and sec.story_index in images:
            sec.image_path = str(images[sec.story_index])  # type: ignore[attr-defined]
    slides = renderer.render_all(script, stories, work / "slides")

    # map story clips → SECTION indexes (the story section of that story)
    section_clips: dict[int, clips_mod.Clip] = {}
    for si, sec in enumerate(script.sections):
        if sec.type == "story" and sec.story_index in story_clips:
            section_clips[si] = story_clips[sec.story_index]

    # 5. Narration -------------------------------------------------------------
    narrations = []
    for sec in script.sections:
        text = sec.narration
        if sec.type == "intro" and script.hook:
            text = f"{script.hook} {text}".strip()
        narrations.append(text)
    wavs = synth_sections(narrations, settings.voice, settings.tts_rate,
                          work / "audio")

    # 6. Video ------------------------------------------------------------------
    date_slug = datetime.now(timezone.utc).strftime("%Y_%m_%d_%H%M")
    video_path = out_dir / f"ai_news_{date_slug}.mp4"
    stats = render_video(slides, wavs, video_path,
                         ken_burns=settings.ken_burns,
                         work_dir=work / "segments",
                         clips=section_clips,
                         clip_max_seconds=settings.clip_max_seconds)

    # 7. Thumbnail ----------------------------------------------------------------
    from PIL import Image
    thumb_png = renderer.render_thumbnail(script, stories,
                                          work / "slides" / "thumbnail.png")
    thumb_jpg = out_dir / f"ai_news_{date_slug}_thumb.jpg"
    Image.open(thumb_png).convert("RGB").save(thumb_jpg, "JPEG", quality=92)

    # 8. Description with story timestamps --------------------------------------
    section_chapters: list[tuple[str, float]] = []
    t_cursor = 0.0
    for sec, wav in zip(script.sections, wavs):
        label = sec.title or sec.type.title()
        if sec.type == "story" and sec.story_index <= len(stories):
            label = f"Story {sec.story_index}: {label}"
        section_chapters.append((label[:60], t_cursor))
        t_cursor += probe_duration(wav)
    from .youtube import build_description
    title = (script.title or f"{stories[0].title[:60]} | AI News "
             f"— {datetime.now(timezone.utc).strftime('%b %d %H:%M')} UTC").strip()
    clip_lines = clips_mod.attribution_lines(story_clips)
    description = build_description(script.description, stories,
                                    section_chapters, settings,
                                    clip_lines=clip_lines)
    tags = build_tags(stories, settings)

    # 8b. QUALITY GATE — the video must earn its upload -------------------------
    video_checks = qa.check_video(video_path, stats, settings)
    packaging_checks = qa.check_packaging(title, description, thumb_jpg)
    qa.log_checks("VIDEO QA", video_checks + packaging_checks)
    qa_passed = qa.write_report(
        out_dir / "qa_report.json",
        script_checks + video_checks + packaging_checks,
        script_words=script.word_count, script_source=script.source,
        duration_seconds=round(stats["duration"], 1), title=title,
        stories=[{"title": s.title, "source": s.source, "url": s.url}
                 for s in stories])
    if not qa_passed:
        for c in video_checks + packaging_checks:
            if not c.passed:
                log.error("VIDEO QA FAIL — %s: %s", c.name, c.detail)
        log.error("QUALITY GATE FAILED — this episode does not meet the "
                  "channel guidelines, so it will NOT be uploaded. The "
                  "next scheduled slot will retry.")
        return 1
    log.info("QUALITY GATE PASSED (%d checks green) — clear to upload.",
             len(script_checks) + len(video_checks) + len(packaging_checks))

    # 9. Upload ---------------------------------------------------------------------
    video_id, video_url = "", ""
    if settings.dry_run:
        log.info("DRY RUN — skipping upload (video saved at %s)", video_path)
    elif not settings.has_youtube_credentials:
        log.warning("YouTube credentials not set (YT_CLIENT_ID / "
                    "YT_CLIENT_SECRET / YT_REFRESH_TOKEN) — skipping upload. "
                    "The finished video is at %s", video_path)
    else:
        from .youtube import set_thumbnail, upload_video
        result = upload_video(video_path, title, description, tags, settings)
        video_id, video_url = result["video_id"], result["url"]
        set_thumbnail(video_id, thumb_jpg, settings)
        log.info("LIVE: %s", video_url)

    # 10. Persist metadata + advance state --------------------------------------------
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": title,
        "description": description[:500],
        "tags": tags,
        "stories": [{"title": s.title, "source": s.source, "url": s.url,
                     "score": round(s.score, 1)} for s in stories],
        "follow_ups": sorted(follow_ups),
        "clips": [{"story": i, "provider": c.provider, "title": c.title,
                   "url": c.url, "license": c.license}
                  for i, c in story_clips.items()],
        "video_id": video_id,
        "video_url": video_url,
        "video_file": str(video_path),
        "thumbnail_file": str(thumb_jpg),
        "duration_seconds": round(stats["duration"], 1),
        "script_source": script.source,
        "script_words": script.word_count,
        "qa_passed": qa_passed,
        "uploaded": bool(video_id),
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (out_dir / "last_script.json").write_text(
        json.dumps({
            "hook": script.hook, "thumbnail_text": script.thumbnail_text,
            "title": script.title, "description": script.description,
            "sections": [vars(s) for s in script.sections],
        }, indent=2) + "\n", encoding="utf-8")

    state_path = Path(settings.state_file) if settings.state_file \
        else ROOT / "state.json"
    if video_id and not settings.keep_state:
        state_mod.advance(state_path, video_url, video_id, stories, settings)
        if follow_ups:
            log.info("Next episodes will avoid these %d stories for %dh "
                     "unless nothing fresher exists.", len(follow_ups),
                     settings.dedup_lookback_hours)
    elif settings.keep_state:
        log.info("State not advanced (--keep-state).")

    log.info("Done in %.1f min. %d stories covered (%d follow-ups), "
             "%d real video clips used.",
             (time.time() - started) / 60, len(stories), len(follow_ups),
             len(story_clips))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("main").critical("RUN FAILED: %s", exc, exc_info=True)
        sys.exit(1)
