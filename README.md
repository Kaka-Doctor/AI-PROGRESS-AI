# AI Progress AI — Daily AI-News Video Agent

A fully autonomous YouTube channel agent. **Twice a day** (every ~12 hours)
it:

1. **Searches the world for what just happened in AI** — 22 RSS feeds
   fetched in parallel: official lab/research blogs (OpenAI, Google
   DeepMind, Google AI, Google Research, Alibaba Qwen, Hugging Face, NVIDIA,
   Berkeley BAIR) + dedicated AI media (The Decoder, MIT News AI, TechCrunch,
   The Verge, Ars Technica, MIT Tech Review, ZDNet, Wired, VentureBeat) +
   world coverage keyword-filtered for AI (BBC Tech UK, Guardian Tech UK,
   SCMP China, Times of India, Al Jazeera) — plus Hacker News front page and
   Reddit. No API keys needed.
2. **Ranks the stories** (source weight, recency, keyword salience, social
   traction) and picks the top 3-5.
3. **Covers the news the previous episode missed**: stories covered in the
   last 60 hours are excluded, so each episode is new material; if a big
   story is still the only thing worth covering, it returns as an explicit
   **FOLLOW-UP** — the script must take a new angle ("since our last
   report…"), never an exact repeat.
4. **Writes an energetic news-anchor script** with Gemini (facts only — it
   is forbidden to invent products, numbers, or dates).
5. **Sizes a real-footage pool to the narration** — the episode is
   ~90% REAL video, slides are only brief animated headlines. Per story
   the agent hunts topical footage first: the story's own keywords, then
   the lab's own videos ("OpenAI", "Anthropic Claude", "Google
   DeepMind", "NVIDIA AI"… — first-party uploads under a
   Creative-Commons license are preferred and marked `[official
   channel]` in the attribution). Generic AI/tech b-roll (training
   data, LLMs, data centers, robots, chips, self-driving…) fills the
   rest.
6. **Sources, all license-clean and attributed in the description**:
   YouTube Creative-Commons (tried FIRST per the channel brief — free
   real videos about AI, computers and training data; official
   lab/big-tech channels rank first; after 2 failed yt-dlp downloads
   it fast-fails off for the run), Wikimedia Commons (CC0/CC-BY/
   CC-BY-SA/PD), and the Internet Archive (PD/CC). Several segments
   are chopped from each source at varied offsets — episodes are cut
   from many real videos, never one long one. Story-specific sources
   must actually match the story's keywords or vendor, and every
   title passes an AI/tech topic gate, so the footage is always
   related to what the narration is talking about.
7. **Renders a 1080p episode** — each section is a ~2s animated
   headline slide + a body of real chopped footage (concat-safe mixed
   encode), plus an **epic high-contrast thumbnail** from a real frame.
8. **Narrates it with an energetic male voice** (edge-tts
   `en-US-AndrewNeural`, +8% pace) — free, no API key.
9. **Uploads to YouTube** with title, timestamps, tags, thumbnail, and then
   **replies to viewer comments every 6 hours** in the viewer's own language.

Channel: https://www.youtube.com/@AIPROGRESSAI

## Two episodes per day — morning + evening

The workflow runs at **06:15 UTC (09:15 EAT)** and **18:15 UTC (21:15 EAT)**,
plus a 19:45 UTC backup slot in case the evening run fails. A cadence guard
in `agent/main.py` requires at least 10.5 hours since the last upload, so
GitHub cron delays and double-fires can never stack posts — and the morning
episode naturally covers whatever the evening one missed.

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
- `force` — bypass the 12-hour cadence guard

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
| `ENABLE_CLIPS` | `1` | real-video b-roll on/off |
| `ENABLE_YT_CLIPS` | `1` | YouTube CC source on/off |
| `MAX_VIDEO_CLIPS` | `3` | clips per episode |
| `CLIP_MAX_SECONDS` | `12` | length of each clip used |
| `DEDUP_LOOKBACK_HOURS` | `60` | how long a story stays "covered" |
| `MIN_HOURS_BETWEEN_POSTS` | `10.5` | cadence guard |

## About the video clips (licensing)

- **YouTube clips**: only videos published under YouTube's **Creative
  Commons (CC-BY) license** are selected (via `search.list` with
  `license=creativeCommons`). CC-BY allows reuse with attribution, which the
  agent adds to every episode description. Downloads use `yt-dlp`.
- **Wikimedia Commons**: CC0 / CC-BY / CC-BY-SA / public-domain videos only,
  with the exact license recorded per clip.
- **Internet Archive**: public-domain / Creative-Commons movies.
- Every clip is trimmed to ~12 seconds, scaled, and letterboxed; the exact
  source, author, link, and license appear in the episode description.
- If every source fails (offline, blocked, nothing on-topic), the episode
  gracefully falls back to slides + Ken Burns — clips are a bonus, never a
  blocker.
- Set `ENABLE_YT_CLIPS=0` to rely only on Commons/Archive if you prefer to
  avoid downloading from YouTube entirely.

## Repository layout

```
agent/
  news.py       # 22 feeds (parallel) + HN + Reddit aggregation, ranking, dedup
  scriptgen.py  # Gemini script (6-model chain) + honest template; follow-ups
  broll.py      # og:image fetching from the real articles
  clips.py      # LEGAL video clips: YouTube CC + Wikimedia + Archive
  theme.py      # neon-tech design system (Anton + Inter, cyan/magenta)
  slides.py     # studio slides + the epic thumbnail
  tts.py        # edge-tts narration (energetic male voice)
  video.py      # FFmpeg render: Ken Burns slides + real clip segments
  youtube.py    # resumable upload, thumbnail, description + attributions
  comments.py   # multilingual comment replies (Gemini)
  qa.py         # pre-upload quality gate
  state.py      # 12-hour cadence guard + story dedup memory
  main.py       # orchestration
.github/workflows/daily_news.yml    # 2 slots/day + backup + manual dispatch
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
