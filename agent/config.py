"""Central configuration for the AI Progress AI news-video agent.

Everything is driven by environment variables with sensible defaults so the
same code runs locally (for testing) and inside GitHub Actions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "fonts"
OUTPUT_DIR = ROOT / "output"
WORK_DIR = OUTPUT_DIR / "work"
STATE_FILE = ROOT / "state.json"
BROLL_DIR = OUTPUT_DIR / "broll"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get(key: str, default: str = "") -> str:
    """Env lookup where an unset OR empty value falls back to the default."""
    value = os.environ.get(key)
    return value if value not in (None, "") else default


# Official lab blogs + aggregators that cover every major US/China lab.
# Each entry: (source label, feed URL, weight 1-10, official?)
FEEDS: list[tuple[str, str, int, bool]] = [
    ("OpenAI", "https://openai.com/news/rss.xml", 10, True),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", 10, True),
    ("Google AI", "https://blog.google/technology/ai/rss/", 9, True),
    ("Qwen (Alibaba)", "https://qwenlm.github.io/blog/index.xml", 9, True),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", 8, True),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", 7, False),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 7, False),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/", 7, False),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", 7, False),
    ("Wired AI", "https://www.wired.com/feed/tag/ai/latest/rss", 6, False),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/", 6, False),
    ("SCMP Tech", "https://www.scmp.com/rss/4/feed", 5, False),
]

# Trending signals (parsed differently from RSS).
HN_FRONT_PAGE_URL = ("https://hn.algolia.com/api/v1/search?"
                     "tags=front_page&hitsPerPage=40")
REDDIT_FEEDS = [
    ("Reddit r/artificial", "https://www.reddit.com/r/artificial/.rss"),
    ("Reddit r/MachineLearning", "https://www.reddit.com/r/MachineLearning/.rss"),
]


@dataclass
class Settings:
    # --- Content -----------------------------------------------------------
    style: str = "neon_tech"          # slide theme
    max_stories: int = 5              # stories per episode
    news_window_hours: int = 48       # only consider items newer than this
    target_words: int = 1100          # ~7 minutes of energetic narration
    voice: str = "en-US-AndrewNeural" # energetic male news voice
    tts_rate: str = "+8%"             # energetic, faster-than-devotional
    ken_burns: bool = True
    dedup_lookback_days: int = 4      # skip stories covered recently

    # --- Branding ----------------------------------------------------------
    channel_name: str = "AI Progress AI"
    channel_handle: str = "@AIPROGRESSAI"
    channel_url: str = "https://www.youtube.com/@AIPROGRESSAI"

    # --- AI script writer ----------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    gemini_fallback_models: list[str] = field(
        default_factory=lambda: ["gemini-3.8-flash", "gemini-flash-latest",
                                 "gemini-3.6-flash", "gemini-3.5-flash",
                                 "gemini-3.1-flash-lite"])
    # Storm resilience: total script-generation rounds + wait between them.
    script_retry_rounds: int = 3
    storm_wait_seconds: int = 900

    # --- YouTube -------------------------------------------------------------
    yt_client_id: str = ""
    yt_client_secret: str = ""
    yt_refresh_token: str = ""
    yt_privacy: str = "public"
    yt_category_id: str = "28"  # Science & Technology

    # --- Comment replies ------------------------------------------------------
    max_comment_replies: int = 10
    comment_max_age_days: int = 30   # news comments age fast

    # --- Quality gate (pre-upload checks) --------------------------------------
    min_script_words: int = 800      # AI-written scripts; template floor is lower
    min_template_words: int = 600    # headline-walk fallback (facts only)
    min_stories: int = 3
    min_video_minutes: float = 3.5   # news episodes can be tighter than devotionals
    max_video_minutes: float = 10.0
    force_upload: bool = False

    # --- Video ---------------------------------------------------------------
    width: int = 1920
    height: int = 1080
    fps: int = 30

    # --- Run overrides (also settable via CLI) -------------------------------
    dry_run: bool = False
    keep_state: bool = False
    state_file: str = ""

    extra_tags: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        get = _get
        return cls(
            style=get("VIDEO_STYLE", "neon_tech"),
            max_stories=int(get("MAX_STORIES", "5") or 5),
            news_window_hours=int(get("NEWS_WINDOW_HOURS", "48") or 48),
            target_words=int(get("TARGET_WORDS", "1100") or 1100),
            voice=get("VOICE", "en-US-AndrewNeural"),
            tts_rate=get("TTS_RATE", "+8%"),
            ken_burns=_bool(get("KEN_BURNS"), True),
            dedup_lookback_days=int(get("DEDUP_LOOKBACK_DAYS", "4") or 4),
            channel_name=get("CHANNEL_NAME", "AI Progress AI"),
            channel_handle=get("CHANNEL_HANDLE", "@AIPROGRESSAI"),
            channel_url=get("CHANNEL_URL",
                            "https://www.youtube.com/@AIPROGRESSAI"),
            gemini_api_key=get("GEMINI_API_KEY", ""),
            gemini_model=get("GEMINI_MODEL", "gemini-3.7-flash"),
            gemini_fallback_models=[
                m.strip()
                for m in get("GEMINI_FALLBACK_MODELS",
                             "gemini-3.8-flash,gemini-flash-latest,"
                             "gemini-3.6-flash,gemini-3.5-flash,"
                             "gemini-3.1-flash-lite").split(",")
                if m.strip()
            ],
            script_retry_rounds=int(get("SCRIPT_RETRY_ROUNDS", "3") or 3),
            storm_wait_seconds=int(get("STORM_WAIT_SECONDS", "900") or 900),
            yt_client_id=get("YT_CLIENT_ID", ""),
            yt_client_secret=get("YT_CLIENT_SECRET", ""),
            yt_refresh_token=get("YT_REFRESH_TOKEN", ""),
            yt_privacy=get("YT_PRIVACY", "public"),
            yt_category_id=get("YT_CATEGORY_ID", "28"),
            max_comment_replies=int(get("MAX_COMMENT_REPLIES", "10") or 10),
            comment_max_age_days=int(get("COMMENT_MAX_AGE_DAYS", "30") or 30),
            min_script_words=int(get("MIN_SCRIPT_WORDS", "800") or 800),
            min_template_words=int(get("MIN_TEMPLATE_WORDS", "600") or 600),
            min_stories=int(get("MIN_STORIES", "3") or 3),
            min_video_minutes=float(get("MIN_VIDEO_MINUTES", "3.5") or 3.5),
            max_video_minutes=float(get("MAX_VIDEO_MINUTES", "10.0") or 10.0),
            force_upload=_bool(get("FORCE_UPLOAD"), False),
            dry_run=_bool(get("DRY_RUN"), False),
            keep_state=_bool(get("KEEP_STATE"), False),
            state_file=get("STATE_FILE", ""),
            extra_tags=[
                t.strip()
                for t in get("EXTRA_TAGS", "").split(",")
                if t.strip()
            ],
        )

    @property
    def has_youtube_credentials(self) -> bool:
        return bool(
            self.yt_client_id and self.yt_client_secret and self.yt_refresh_token
        )
