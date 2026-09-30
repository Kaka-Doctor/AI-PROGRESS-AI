"""Generate the daily AI-news episode script.

Priority:
1. Google Gemini (free tier key) — preferred, writes the energetic
   news-anchor script from the REAL stories collected from the web.
2. Built-in headline-walk template — last resort when every model is
   storming. Reads the actual RSS headlines/summaries with framing, so
   it is always factually honest (never invents news).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Settings
from .news import Story, format_digest

log = logging.getLogger("scriptgen")

VALID_TYPES = {"intro", "story", "take", "outro"}

MAX_DRAFTS = 3

SCHEMA_HINT = """{
  "hook": "explosive spoken first line that stops the scroll (15-25 words)",
  "thumbnail_text": "3-6 PUNCHY words for the thumbnail (ALL CAPS energy)",
  "title": "clickable YouTube title (max 95 chars, no clickbait lies)",
  "description": "2-3 sentence episode summary for YouTube (no hashtags)",
  "sections": [
    {"type": "intro",  "title": "What Just Happened",       "narration": "...", "on_screen": ["short phrase", "short phrase"]},
    {"type": "story",  "title": "Story 1 title",            "narration": "...", "on_screen": ["...", "...", "..."]},
    {"type": "take",   "title": "Why Story 1 Matters",      "narration": "...", "on_screen": ["...", "..."]},
    {"type": "story",  "title": "Story 2 title",            "narration": "...", "on_screen": ["...", "...", "..."]},
    {"type": "take",   "title": "Why Story 2 Matters",      "narration": "...", "on_screen": ["...", "..."]},
    {"type": "outro",  "title": "Stay Ahead",               "narration": "...", "on_screen": []}
  ]
}"""


def build_prompt(stories: list[Story], settings: Settings,
                 follow_ups: set[int] | None = None) -> str:
    digest = format_digest(stories, follow_ups)
    fu_note = ""
    if follow_ups:
        fu_note = f"""
FOLLOW-UP STORIES: {len(follow_ups)} of the stories below were covered in an
earlier episode (marked [FOLLOW-UP] in the digest). For those, do NOT repeat
the earlier framing — cover the NEW developments, a different angle, or what
has changed since. Open them with update language like "since our last
report" or "here's what's new on". The channel posts twice a day, so a
recurring story must feel like progress, not a rerun."""
    return f"""You are the energetic anchor of "AI Progress Daily" — a YouTube AI-news
channel covering every major lab: OpenAI, Anthropic, Google DeepMind, Meta,
xAI, Mistral, and the China labs (DeepSeek, Alibaba Qwen, ByteDance, Moonshot,
Zhipu, MiniMax). The viewer wants to feel the speed of AI progress.

TODAY'S REAL STORIES (from the web, most important first):
{digest}
{fu_note}
Write the episode script with {settings.target_words}-{settings.target_words + 300} words of total
narration — about 6-8 energetic minutes. Structure:

1. HOOK — an explosive opener: the single most exciting thing that happened
   today, said in one breath-stopping line.
2. STORY SECTIONS — one section per story, in priority order. For EACH story:
   what was announced/released/leaked, the concrete numbers (parameters,
   benchmarks, prices, dates — use ONLY numbers present in the digest), and
   the context of who it beats or follows. Keep the energy high: short punchy
   sentences, present tense, direct address ("you", "we").
3. TAKE sections — after each story (or grouped after 2), a quick "why this
   matters" beat: what it changes for users, developers, or the race between
   labs. Opinionated but fair — one sharp insight, not a lecture.
4. OUTRO — "Stay ahead of the curve" energy + invite to subscribe for daily
   AI progress.

HARD RULES:
- FACTS ONLY from the digest. Never invent products, numbers, dates, or
  quotes. If a detail is not in the digest, describe it in general terms.
- LENGTH (critical): total narration between {settings.target_words} and
  {settings.target_words + 300} words. Rough targets: intro 80-110; each
  story 140-200; each take 80-120; outro 50-80. At most 13 sections.
- NO EXACT REPEATS: if a story is marked [FOLLOW-UP], give it a fresh angle
  and explicitly signal the update; never re-state the previous episode's
  framing word for word.
- Tone: energetic news anchor — vivid verbs, rhythm, momentum. NO profanity,
  no doom-mongering, no "skynet" cliches. Confident optimism about progress.
- "narration" is read aloud by a neural voice: plain speakable English. No
  markdown, no emojis, no parentheses, no URLs.
- Story order in the script must match the digest priority order.
- "title" of story sections: a short catchy label (3-6 words), not the full
  headline. "on_screen": 2-4 ultra-short phrases (max ~7 words), title case,
  no ending punctuation.
- "thumbnail_text": 3-6 words that sell the biggest story, ALL-CAPS energy
  (e.g. "GPT-5 JUST LEAKED?!").
- "title" (YouTube): under 95 chars, energetic but truthful, end with the
  biggest lab name, e.g. "OpenAI's New Model SHOCKS The Industry | AI News".

Return ONLY valid JSON matching exactly this shape:
{SCHEMA_HINT}"""


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

def _call_gemini_model(prompt: str, settings: Settings, model: str,
                       max_attempts: int = 5) -> str:
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    headers = {
        "x-goog-api-key": settings.gemini_api_key,
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.85,
            "maxOutputTokens": 16384,
        },
    }
    if "2.5" in model or "3" in model:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 2048}

    retryable = {429, 500, 502, 503, 504}
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
        except requests.RequestException as exc:
            log.warning("Gemini attempt %d/%d network error: %s",
                        attempt, max_attempts, exc)
        else:
            if r.status_code in retryable:
                log.warning("Gemini attempt %d/%d got HTTP %d (transient)",
                            attempt, max_attempts, r.status_code)
            elif (r.status_code == 400
                  and "thinkingConfig" in body["generationConfig"]):
                body["generationConfig"].pop("thinkingConfig", None)
                log.warning("Gemini rejected thinkingConfig; retrying without it")
                continue
            else:
                r.raise_for_status()
                data = r.json()
                cand = data["candidates"][0]
                finish = cand.get("finishReason", "")
                text = "".join(
                    p.get("text", "") for p in cand["content"]["parts"])
                if not text.strip():
                    raise ValueError(f"empty completion (finishReason={finish})")
                if finish == "MAX_TOKENS":
                    log.warning("Gemini hit MAX_TOKENS; output may be truncated.")
                return text
        if attempt < max_attempts:
            wait = min(8 * 2 ** (attempt - 1), 45)
            log.warning("Retrying %s in %ds…", model, wait)
            time.sleep(wait)
    raise RuntimeError(f"{model} failed after {max_attempts} attempts")


def _call_gemini(prompt: str, settings: Settings) -> tuple[str, str]:
    chain: list[str] = [settings.gemini_model]
    chain += [m for m in settings.gemini_fallback_models
              if m and m != settings.gemini_model]
    last_exc: Exception | None = None
    for i, model in enumerate(chain):
        attempts = 5 if i == 0 else 3
        try:
            return _call_gemini_model(prompt, settings, model,
                                      max_attempts=attempts), model
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("Gemini model %s unavailable: %s", model, exc)
            if i < len(chain) - 1:
                log.warning("Switching to fallback model: %s", chain[i + 1])
    raise RuntimeError(
        f"all Gemini models failed ({', '.join(chain)}): {last_exc}")


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start: i + 1])
    raise ValueError("unbalanced JSON")


@dataclass
class Section:
    type: str
    title: str
    narration: str
    on_screen: list[str] = field(default_factory=list)
    story_index: int = 0   # which story (1-based) this section covers


@dataclass
class Script:
    hook: str = ""
    thumbnail_text: str = ""
    title: str = ""
    description: str = ""
    sections: list[Section] = field(default_factory=list)
    source: str = "unknown"

    @property
    def word_count(self) -> int:
        return sum(len(s.narration.split()) for s in self.sections) \
            + len(self.hook.split())

    def validate(self) -> None:
        cleaned: list[Section] = []
        for sec in self.sections:
            sec.type = str(sec.type).lower().strip()
            if sec.type not in VALID_TYPES:
                sec.type = "story"
            sec.title = (sec.title or "").strip()[:80]
            sec.narration = (sec.narration or "").strip()
            sec.on_screen = [str(p).strip()[:60]
                             for p in sec.on_screen if str(p).strip()][:4]
            if sec.narration:
                cleaned.append(sec)
        if not cleaned:
            raise ValueError("script has no usable sections")
        if cleaned[0].type != "intro":
            cleaned[0].type = "intro"
        if cleaned[-1].type != "outro":
            cleaned.append(Section("outro", "Stay Ahead", "", []))
        self.sections = cleaned
        self.hook = (self.hook or "").strip()
        self.thumbnail_text = (self.thumbnail_text or "").strip()[:40]
        self.title = (self.title or "").strip()[:100]
        self.description = (self.description or "").strip()


def _parse_script(raw: str, source: str) -> Script:
    data = _extract_json(raw)
    sections = [
        Section(
            type=s.get("type", "story"),
            title=s.get("title", ""),
            narration=s.get("narration", ""),
            on_screen=s.get("on_screen", []) or [],
            story_index=int(s.get("story_index", 0) or 0),
        )
        for s in data.get("sections", [])
    ]
    script = Script(
        hook=data.get("hook", ""),
        thumbnail_text=data.get("thumbnail_text", ""),
        title=data.get("title", ""),
        description=data.get("description", ""),
        sections=sections,
        source=source,
    )
    script.validate()
    return script


# ---------------------------------------------------------------------------
# Headline-walk fallback (total AI outage — facts only)
# ---------------------------------------------------------------------------

def _template_script(stories: list[Story], min_words: int,
                     follow_ups: set[int] | None = None) -> Script:
    """Read the REAL headlines and summaries with energetic framing.

    Never invents news — every fact spoken comes from the RSS digest.
    """
    follow_ups = follow_ups or set()
    sections = [
        Section("intro", "What Just Happened",
                "Stop scrolling, because the world of AI did not sleep last "
                "night. Big moves are landing from the biggest labs on the "
                "planet, and in the next few minutes I will catch you up on "
                "everything that matters. Let's get into the top stories in "
                "artificial intelligence right now.",
                ["The latest AI stories", "Big labs, big moves",
                 "Let's go"]),
    ]
    for i, s in enumerate(stories, 1):
        src = f"{s.source}" + (" — straight from the lab" if s.official
                               else "")
        narration = (
            f"Story number {i}. {s.title}. That is the headline from {src}, "
            f"{s.published.strftime('released %B %d')}. ")
        if i in follow_ups:
            narration += ("You may remember this one from our earlier report — "
                          "here is the latest chapter in the story. ")
        if s.summary:
            narration += f"Here is what we know so far. {s.summary} "
        narration += (
            "Details are still developing, so treat the finer points as "
            "early information — but the headline itself is confirmed. ")
        sections.append(Section(
            "story", s.title[:70], narration,
            [f"Story {i}", s.source, "Confirmed headline"], story_index=i))
        take_sets = [
            ["Capabilities jump, prices drop", "Competitors must answer",
             "The race shifts again"],
            ["Changes what you can build", "Changes what you expect",
             "Follow-ups land within days"],
            ["Frontier gets closer", "Everyday tools level up",
             "Watch this space closely"],
            ["Big labs, big moves", "The pace is the story",
             "Notice first, benefit first"],
            ["The board resets today", "Momentum compounds",
             "Tomorrow looks different"],
        ]
        sections.append(Section(
            "take", f"Why It Matters — Story {i}",
            "So why should you care about this one? Because every move from "
            "a major lab shifts the whole board. Capabilities jump, prices "
            "usually drop, and every competitor has to answer within weeks. "
            "For developers it changes what you can build; for everyone else "
            "it changes what you can expect from the tools you already use. "
            "The pace right now is the real story: what counted as a "
            "breakthrough a year ago is now a routine release. Watch this "
            "space closely, because follow-up announcements usually arrive "
            "within days, and I will keep tracking it for you right here, "
            "every single day.",
            take_sets[(i - 1) % len(take_sets)],
            story_index=i))
    # Big-picture synthesis — references the REAL headlines, invents nothing.
    titles = [s.title for s in stories]
    sections.append(
        Section("take", "The Big Picture",
                "Step back and look at today's board as a whole. "
                + " ".join(titles[:3])
                + ". Put together, these headlines tell one story: the AI "
                "race is compounding. Labs are not just shipping faster "
                "than ever; they are shipping on top of each other, each "
                "release squeezing the gap between frontier capability and "
                "everyday affordability. That is why following this space "
                "daily actually matters now — the landscape you see today "
                "may look meaningfully different by the end of the week, "
                "and the people who notice first are the ones who benefit "
                "first.",
                ["The race is compounding", "Notice first, benefit first",
                 "Tomorrow looks different"]))

    sections.append(
        Section("outro", "Stay Ahead",
                "And that is your AI progress report for today. The pace is "
                "not slowing down, and neither are we. If you want to stay "
                "ahead of the curve, subscribe and turn on notifications, "
                "because tomorrow these stories will already look old. See "
                "you in the next one.",
                ["Subscribe for daily AI news", "Stay ahead of the curve"]))

    script = Script(
        hook=f"Big AI news just dropped: {stories[0].title}. Here is "
             f"everything you missed today.",
        thumbnail_text="AI NEWS JUST DROPPED",
        title=f"{stories[0].title[:60]} | AI News Daily",
        description=f"Today's top {len(stories)} AI stories, led by: "
                    f"{stories[0].title}. A fast daily digest of everything "
                    "happening across OpenAI, Anthropic, Google DeepMind, "
                    "Meta, and the China AI labs.",
        sections=sections,
        source="template",
    )
    script.validate()
    return script


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_script(stories: list[Story], settings: Settings,
                    follow_ups: set[int] | None = None) -> Script:
    if not settings.gemini_api_key:
        log.warning("No GEMINI_API_KEY — using headline-walk template.")
        return _template_script(stories, settings.min_template_words,
                                follow_ups)

    prompt = build_prompt(stories, settings, follow_ups)
    min_words = settings.min_script_words
    best: Script | None = None
    for draft in range(1, MAX_DRAFTS + 1):
        if draft > 1:
            prev = best.word_count if best else 0
            prompt = prompt + (
                f"\n\nCRITICAL LENGTH FIX: your previous draft had only "
                f"{prev} words of narration — too short. Write the COMPLETE "
                f"script with at least {min_words} words of total narration "
                "across all sections. Do not summarize, do not abbreviate, "
                "do not omit or merge sections; develop each story fully.")
        try:
            raw, model_used = _call_gemini(prompt, settings)
            script = _parse_script(raw, source=f"gemini:{model_used}")
            if script.word_count >= min_words:
                log.info("Script generated with Gemini %s (%d words, "
                         "draft %d/%d)", model_used, script.word_count,
                         draft, MAX_DRAFTS)
                return script
            log.warning("Gemini draft %d/%d TOO SHORT (%d words, need %d) "
                        "— regenerating with a stricter length demand",
                        draft, MAX_DRAFTS, script.word_count, min_words)
            if best is None or script.word_count > best.word_count:
                best = script
        except Exception as exc:  # noqa: BLE001
            log.error("Gemini draft %d/%d failed (%s)", draft,
                      MAX_DRAFTS, exc)
    if best is not None:
        log.error("All %d Gemini drafts were short (best: %d words). The "
                  "pre-upload quality gate will now decide.", MAX_DRAFTS,
                  best.word_count)
        return best
    log.error("Gemini failed completely — falling back to the honest "
              "headline-walk script (facts read straight from the feeds).")
    return _template_script(stories, settings.min_template_words, follow_ups)
