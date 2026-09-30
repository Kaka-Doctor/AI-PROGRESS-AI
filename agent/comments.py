"""Reply to viewer comments with an energetic, friendly host tone.

Runs as its own step (workflow: reply_comments.yml, every 6 hours) or manually:

    python -m agent.comments             # reply for real
    python -m agent.comments --dry-run   # list comments + draft replies, post nothing

For every NEW top-level comment on any of the channel's videos:
  1. skip if it is our own comment, already replied to (checked live via the
     thread's replies AND the local comments_state.json), older than
     COMMENT_MAX_AGE_DAYS, or plain spam
  2. ask Gemini for a short, high-energy reply IN THE COMMENTER'S
     OWN LANGUAGE (English, Swahili, or mixed)
  3. post it as a reply via the YouTube Data API v3 (youtube.force-ssl scope)

Safety rails: at most MAX_COMMENT_REPLIES replies per run, never argues with
hostile comments (Gemini is instructed to return action=skip), never posts
without a successful draft, and stops immediately on quota errors.

State lives in comments_state.json at the repo root; the workflow commits it
back after each run so replies are never duplicated.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import ROOT, Settings
from .scriptgen import _call_gemini, _extract_json
from .youtube import YouTubeError, _access_token

log = logging.getLogger("comments")

YT_API = "https://www.googleapis.com/youtube/v3"
STATE_PATH = ROOT / "comments_state.json"
REPLIED_KEEP = 500  # how many replied comment ids to remember

PROMPT_TEMPLATE = """You are the host of "{channel}", a daily AI-news YouTube channel. One of your viewers left a comment.

VIDEO: "{title}"
COMMENT BY {author}:
"{body}"

Write the channel's public reply.
- Reply in the SAME language the commenter used (English, Swahili, or a mix — mirror their language exactly).
- Tone: energetic, friendly, and approachable — like a passionate tech creator talking to a subscriber.
- Answer questions with what is well-known and public; if you are not sure of a fact, invite them to the next episode instead of guessing.
- Length: 1-3 sentences, at most 60 words.
- Thank them for watching when it feels natural; hype the daily schedule; invite good questions.
- NEVER fabricate news, products, dates, or personal stories. No hashtags, no links, no emojis, no arguments.
- If the comment is hostile, mocking, spam, or anything you cannot answer positively — we stay silent: return action "skip".

Return ONLY JSON exactly like:
{{"action": "reply", "reply": "..."}}
or
{{"action": "skip", "reason": "short reason"}}"""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    default = {"replied": [], "replies_posted": 0, "last_run": None,
               "note": "Comment ids already replied to (cap 500)."}
    if not STATE_PATH.exists():
        return default
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("replied", [])
        data.setdefault("replies_posted", 0)
        data.setdefault("last_run", None)
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("comments_state.json unreadable (%s); starting fresh", exc)
        return default


def save_state(state: dict) -> None:
    state["replied"] = state["replied"][-REPLIED_KEEP:]
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n",
                          encoding="utf-8")


# ---------------------------------------------------------------------------
# YouTube Data API helpers
# ---------------------------------------------------------------------------

def _get_my_channel_id(token: str) -> str:
    r = requests.get(f"{YT_API}/channels", params={"part": "id", "mine": "true"},
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        raise YouTubeError("channels.list?mine=true returned no channel — "
                           "check that the YouTube account has a channel")
    return items[0]["id"]


def _list_threads(token: str, channel_id: str, max_pages: int = 2) -> list[dict]:
    """Newest-first comment threads across all of the channel's videos."""
    threads: list[dict] = []
    page_token = ""
    for _ in range(max_pages):
        params = {
            "part": "snippet,replies",
            "allThreadsRelatedToChannelId": channel_id,
            "order": "time",
            "maxResults": 50,
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(f"{YT_API}/commentThreads", params=params,
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=60)
        r.raise_for_status()
        data = r.json()
        threads.extend(data.get("items", []))
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return threads


def _video_titles(token: str, video_ids: list[str]) -> dict[str, str]:
    """Batch-fetch titles so replies can reference the actual video."""
    titles: dict[str, str] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        r = requests.get(f"{YT_API}/videos",
                         params={"part": "snippet", "id": ",".join(batch)},
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=60)
        if r.status_code != 200:
            log.warning("videos.list failed (HTTP %d) — replies will use a "
                        "generic video title", r.status_code)
            break
        for item in r.json().get("items", []):
            titles[item["id"]] = item["snippet"].get("title", "")
    return titles


def _post_reply(token: str, parent_id: str, text: str) -> dict:
    r = requests.post(
        f"{YT_API}/comments",
        params={"part": "snippet"},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"snippet": {"parentId": parent_id, "textOriginal": text}},
        timeout=60,
    )
    if r.status_code == 403 and "quotaExceeded" in r.text:
        raise QuotaExceeded(r.text[:300])
    r.raise_for_status()
    return r.json()


class QuotaExceeded(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Candidate filtering
# ---------------------------------------------------------------------------

def _thread_fields(thread: dict) -> dict | None:
    """Flatten a commentThread item into the bits we care about."""
    try:
        top = thread["snippet"]["topLevelComment"]
        snip = top["snippet"]
        return {
            "id": top.get("id", thread["id"]),
            "video_id": thread["snippet"].get("videoId", ""),
            "author": snip.get("authorDisplayName", "a viewer") or "a viewer",
            "author_channel": (snip.get("authorChannelId") or {}).get("value", ""),
            "text": html.unescape(snip.get("textDisplay", "")),
            "published_at": snip.get("publishedAt", ""),
            "likes": int(snip.get("likeCount", 0)),
            "existing_replies": [
                (c.get("snippet") or {}) for c in
                (thread.get("replies", {}) or {}).get("comments", [])
            ],
        }
    except (KeyError, TypeError):
        return None


def _parse_published(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_candidates(threads: list[dict], my_channel_id: str,
                       state: dict, settings: Settings) -> list[dict]:
    replied = set(state.get("replied", []))
    now = datetime.now(timezone.utc)
    max_age_days = settings.comment_max_age_days
    candidates: list[dict] = []

    for th in threads:
        f = _thread_fields(th)
        if not f or not f["text"].strip():
            continue
        # our own comment (e.g. pinned) — never reply to ourselves
        if f["author_channel"] and f["author_channel"] == my_channel_id:
            continue
        # already replied live (our reply is present in the thread)
        if any((r.get("authorChannelId") or {}).get("value", "")
               == my_channel_id for r in f["existing_replies"]):
            continue
        # already replied according to local state (covers deleted replies)
        if f["id"] in replied:
            continue
        # too old — don't dig up years-old threads
        pub = _parse_published(f["published_at"])
        if pub is None:
            continue
        age_days = (now - pub).total_seconds() / 86400
        if age_days > max_age_days:
            continue
        candidates.append(f)

    candidates.sort(key=lambda c: c["published_at"], reverse=True)
    return candidates[:settings.max_comment_replies]


# ---------------------------------------------------------------------------
# Reply generation (Gemini with model fallback chain + graceful skip)
# ---------------------------------------------------------------------------

def _clean_reply(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    text = text.replace("\\n", " ").replace("\\\"", '"')
    text = re.sub(r"^(reply|host|channel)\s*[:\-]\s*", "", text, flags=re.I)
    return text[:900]


def draft_reply(comment: dict, video_title: str,
                settings: Settings) -> tuple[str, str]:
    """Returns (action, reply_text). action is 'reply' or 'skip'."""
    prompt = PROMPT_TEMPLATE.format(
        channel=settings.channel_name,
        title=video_title or "our daily AI news episode",
        author=comment["author"][:40],
        body=comment["text"][:1500],
    )
    try:
        raw, model_used = _call_gemini(prompt, settings)
        data = _extract_json(raw)
        action = str(data.get("action", "reply")).lower().strip()
        if action == "skip":
            return "skip", str(data.get("reason", ""))[:120]
        reply = _clean_reply(str(data.get("reply", "")))
        if len(reply.split()) < 4:  # suspiciously short → treat as skip
            return "skip", "draft too short"
        return "reply", reply
    except Exception as exc:  # noqa: BLE001
        # No draft → stay SILENT rather than post something generic twice.
        log.warning("Gemini could not draft a reply for comment %s: %s",
                    comment["id"], exc)
        return "error", str(exc)[:120]


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(settings: Settings) -> int:
    if not settings.has_youtube_credentials:
        log.warning("YouTube credentials not set — nothing to do.")
        return 0

    token = _access_token(settings.yt_client_id,
                          settings.yt_client_secret,
                          settings.yt_refresh_token)
    my_channel = _get_my_channel_id(token)
    log.info("Channel %s — listing newest comment threads…", my_channel)

    threads = _list_threads(token, my_channel)
    log.info("Fetched %d comment thread(s) across the channel's videos.",
             len(threads))

    state = load_state()
    candidates = collect_candidates(threads, my_channel, state, settings)
    log.info("%d candidate comment(s) after filters "
             "(self/already-replied/age/spam).", len(candidates))

    if not candidates:
        state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state)
        log.info("Nothing to reply to. See you next run.")
        return 0

    titles = _video_titles(token, sorted({c["video_id"] for c in candidates
                                          if c["video_id"]}))

    posted, skipped, failed = 0, 0, 0
    for c in candidates:
        title = titles.get(c["video_id"], "")
        action, payload = draft_reply(c, title, settings)

        if action == "skip":
            log.info("SKIP %s (%s): %.60s", c["author"], payload, c["text"])
            state.setdefault("replied", []).append(c["id"])  # don't reconsider
            skipped += 1
            continue
        if action == "error":
            failed += 1
            continue  # retry next run

        author_preview = c["author"]
        comment_preview = c["text"][:80].replace("\n", " ")
        if settings.dry_run:
            log.info("DRY-RUN would reply to %s: %.60s", author_preview,
                     comment_preview)
            log.info("  draft: %s", payload)
            continue

        try:
            _post_reply(token, c["id"], payload)
        except QuotaExceeded as exc:
            log.error("YouTube quota exceeded — stopping for this run: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("Reply failed for comment %s: %s", c["id"], exc)
            failed += 1
            continue

        posted += 1
        state.setdefault("replied", []).append(c["id"])
        log.info("REPLIED to %s | %.60s", author_preview, comment_preview)
        log.info("  reply: %s", payload)

    state["replies_posted"] = int(state.get("replies_posted", 0)) + posted
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not settings.dry_run:
        save_state(state)

    log.info("Done: %d posted, %d skipped, %d failed to draft. "
             "Lifetime replies: %d.", posted, skipped, failed,
             state["replies_posted"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reply to YouTube comments")
    parser.add_argument("--dry-run", action="store_true",
                        help="draft replies but post nothing, keep state")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = Settings.from_env()
    if args.dry_run:
        settings.dry_run = True

    if settings.dry_run:
        log.info("DRY RUN — no replies will be posted.")

    try:
        return run(settings)
    except Exception as exc:  # noqa: BLE001
        log.critical("COMMENT RUN FAILED: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
