# AI Progress AI — Daily AI-News Video Agent

A fully autonomous YouTube channel agent. Every day it:

1. **Searches the web** for what just happened in AI — official blog feeds
   (OpenAI, Google DeepMind, Google AI, Alibaba Qwen, Hugging Face) plus the
   tech press that covers every major lab (TechCrunch, The Verge, Ars
   Technica, MIT Tech Review, Wired, VentureBeat, SCMP), Hacker News front
   page, and Reddit — no API keys needed.
2. **Ranks the day's stories** (source weight, recency, keyword salience,
   social traction) and picks the top 3-5, skipping anything already covered
   in the last few days.
3. **Writes an energetic news-anchor script** with Gemini (facts only — it
   is forbidden to invent products, numbers, or dates).
4. **Fetches real article imagery** (og:image from the actual announcement
   pages) for full-bleed b-roll cards with Ken Burns motion.
5. **Renders a 1080p episode** — neon-tech studio slides, giant Anton
   headlines, animated zoom — plus an **epic high-contrast thumbnail**.
6. **Narrates it with an energetic male voice** (edge-tts
   `en-US-AndrewNeural`, +8% pace) — free, no API key.
7. **Uploads to YouTube** with title, timestamps, tags, thumbnail, and then
   **replies to viewer comments every 6 hours** in the viewer's own language.

Channel: https://www.youtube.com/@AIPROGRESSAI

## One episode per day — guaranteed

The workflow runs at three times a day (09:15, 17:15, 18:45 UTC). A
once-per-day guard in `agent/main.py` allows exactly one upload per EAT
calendar day, so extra slots simply exit clean after the day's episode is
live — GitHub cron delays and double-fires can never double-post.

## Pre-upload quality gate

An episode must EARN its upload (`agent/qa.py`): ≥800 narration words
(≥600 for the honest headline-walk fallback), ≥3 stories, 3.5-10 minute
duration, sane title/description/thumbnail. On failure: no upload, no state
advance, exit 1 — the next slot retries. A missing day is better than a bad
episode.

If Gemini is completely down (503 storm), the agent waits out the storm and
retries script generation up to 3 rounds ~15 minutes apart, across a
6-model fallback chain. The last resort is a **headline-walk episode** that
reads the real RSS headlines and summaries — it never invents news.

## Custom thumbnails need a verified channel (one-time, 1 minute)

YouTube only allows custom thumbnails on **verified channels**. Until the
channel is verified, thumbnail uploads return 403 and YouTube falls back to
an auto-picked frame (the episode itself is unaffected). Verify once:

1. Open **https://www.youtube.com/verify** signed in as the channel's Google
   account.
2. Enter the received phone code.

From then on every new episode gets its epic thumbnail automatically. To
re-apply it to an episode uploaded before verifying, run the
**Set Thumbnail** workflow (`.github/workflows/set_thumbnail.yml`) — with a
blank `video_id` it targets the latest episode from `state.json`.

## Setup (one-time)

Four repository secrets (Settings → Secrets and variables → Actions):

| Secret | Where it comes from |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio → Get API key |
| `YT_CLIENT_ID` | Google Cloud Console → OAuth client (Web application) |
| `YT_CLIENT_SECRET` | same OAuth client |
| `YT_REFRESH_TOKEN` | one-time consent, see below |

### Getting YT_REFRESH_TOKEN (once)

1. In Google Cloud Console, create an OAuth **Web application** client with
   authorized redirect URI `http://localhost:8765` (you can reuse an existing
   client that already has this URI).
2. Make sure the OAuth consent screen is **Published** (Production), and the
   YouTube Data API v3 is enabled.
3. On any machine with Python 3:

   ```bash
   pip install requests
   python scripts/get_refresh_token.py --client-id YOUR_ID --client-secret YOUR_SECRET
   ```

   Open the printed URL, **choose the Google account / channel that owns
   AI PROGRESS AI** (if the channel is a Brand Account, select the brand
   account in Google's account picker), consent, and paste the redirected
   `http://localhost:8765/?code=...` URL back into the terminal.
4. Paste the resulting refresh token into the `YT_REFRESH_TOKEN` secret.

Scopes needed: `youtube.upload` + `youtube.force-ssl`.

## Manual runs

GitHub → Actions → **Daily AI News Video** → Run workflow:

- `no_upload` — dry run (renders, QA-checks, does not upload)
- `force` — bypass the once-per-day guard

Comments: **Reply to Comments** → Run workflow (`dry_run` to preview).

## Tuning (repo Variables, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `MAX_STORIES` | `5` | stories per episode |
| `VOICE` | `en-US-AndrewNeural` | try `en-US-BrianNeural` for deeper energy |
| `TTS_RATE` | `+8%` | narration pace |
| `MIN_STORIES` | `3` | QA floor |
| `MIN_SCRIPT_WORDS` | `800` | QA floor for AI scripts |
| `SCRIPT_RETRY_ROUNDS` | `3` | storm-resilience rounds |
| `STORM_WAIT_SECONDS` | `900` | wait between rounds |

## Repository layout

```
agent/
  news.py       # RSS + HN + Reddit aggregation, ranking, dedup
  scriptgen.py  # Gemini script (6-model fallback chain) + honest template
  broll.py      # og:image fetching from the real articles
  theme.py      # neon-tech design system (Anton + Inter, cyan/magenta)
  slides.py     # studio slides + the epic thumbnail
  tts.py        # edge-tts narration (energetic male voice)
  video.py      # FFmpeg Ken Burns render, 1080p30 H.264 + AAC
  youtube.py    # resumable upload, thumbnail, description builder
  comments.py   # multilingual comment replies (Gemini)
  qa.py         # pre-upload quality gate
  state.py      # once-per-day guard + story dedup memory
  main.py       # orchestration
.github/workflows/daily_news.yml    # 3 slots/day + manual dispatch
.github/workflows/reply_comments.yml# every 6h
.github/workflows/probe.yml         # manual Gemini health check
```

## Costs

Everything runs on GitHub Actions free minutes (public repo: unlimited) with
free-tier services: RSS (no key), Gemini API free tier, edge-tts (free),
FFmpeg (runner). No paid API is required.

## Notes on video clips

The agent uses the labs' own announcement images (og:image) as b-roll.
Downloading and re-cutting copyrighted YouTube/social clips is legally risky
for an automated channel and frequently blocked by bot detection in CI, so
v1 ships with motion-graphics b-roll instead; the pipeline is structured so
a licensed-clip module can be added later in `agent/broll.py`.
