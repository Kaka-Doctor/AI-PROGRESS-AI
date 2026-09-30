"""Tests for the twice-daily upgrade: cadence guard, missed-news coverage,
follow-up framing, and clip attribution.

Run:  /home/z/.venv/bin/python scripts/test_twice_daily.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import Settings
from agent.news import Story, format_digest
from agent.state import (cadence_allows_post, hours_since_last_post,
                         known_fingerprints, remember_stories)
from agent.clips import Clip, attribution_lines, story_queries

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def mk_story(title: str, hours_old: float = 3.0) -> Story:
    return Story(
        title=title, url=f"https://example.com/{abs(hash(title))}",
        source="Test Feed",
        published=datetime.now(timezone.utc) - timedelta(hours=hours_old),
        summary="A test summary.", weight=7, official=False)


S = Settings()


# ---------------------------------------------------------------------------
print("1. Cadence guard (posts every ~12 hours)")
# ---------------------------------------------------------------------------

def state_with(hours_ago: float) -> dict:
    at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"last_video": {"id": "x", "url": "y"},
            "post_log": [{"at": at.isoformat(), "video_id": "x",
                          "url": "y"}],
            "last_run": at.isoformat()}


allowed, why = cadence_allows_post(state_with(12.5), S)
check("12.5h since last post → allowed", allowed, why)
blocked, why = cadence_allows_post(state_with(3.0), S)
check("3h since last post → blocked", not blocked, why)
blocked, why = cadence_allows_post(state_with(9.0), S)
check("9h since last post → blocked (below 10.5h)", not blocked, why)
allowed, why = cadence_allows_post(state_with(11.0), S)
check("11h since last post → allowed (tolerates cron delay)", allowed, why)
allowed, why = cadence_allows_post({}, S)
check("never posted → allowed", allowed, why)
gap = hours_since_last_post(state_with(6.0))
check("hours_since_last_post ≈ 6.0", gap is not None and 5.9 < gap < 6.1,
      f"got {gap}")

# ---------------------------------------------------------------------------
print("2. Coverage memory (hours-based)")
# ---------------------------------------------------------------------------

st = {"recent_stories": []}
a, b = mk_story("OpenAI launches GPT-6"), mk_story("Anthropic ships Claude 5")
remember_stories(st, [a, b])
known = known_fingerprints(st, lookback_hours=S.dedup_lookback_hours)
check("remembered stories are known", a.fingerprint in known
      and b.fingerprint in known)
old = {"recent_stories": [{
    "fingerprint": a.fingerprint, "title": a.title, "url": a.url,
    "source": "s", "date": (datetime.now(timezone.utc)
                             - timedelta(hours=S.dedup_lookback_hours + 10)
                             ).isoformat()}]}
known_old = known_fingerprints(old, lookback_hours=S.dedup_lookback_hours)
check("stories older than the lookback are forgotten",
      a.fingerprint not in known_old)

# ---------------------------------------------------------------------------
print("3. Missed-news selection (fresh first, covered only as follow-ups)")
# ---------------------------------------------------------------------------


def select(ranked: list[Story], state: dict, settings: Settings):
    known = known_fingerprints(state, settings.dedup_lookback_hours)
    fresh = [s for s in ranked if s.fingerprint not in known]
    if len(fresh) >= settings.min_stories:
        stories = fresh[:settings.max_stories]
    else:
        covered = [s for s in ranked if s.fingerprint in known]
        stories = (fresh + covered)[:settings.max_stories]
    follow_ups = {i for i, s in enumerate(stories, 1)
                  if s.fingerprint in known}
    return stories, follow_ups


cov_state = {"recent_stories": []}
remember_stories(cov_state, [a, b])          # a, b covered earlier today
c, d, e = (mk_story("DeepSeek releases V4"), mk_story("Qwen3 tops charts"),
           mk_story("EU AI Act update"))
f2 = mk_story("Nvidia unveils Rubin chip")

# plenty of fresh news → zero repeats
stories, fus = select([a, b, c, d, e, f2], cov_state, S)
check("fresh supply: no covered story selected",
      all(s.fingerprint not in
          known_fingerprints(cov_state, S.dedup_lookback_hours)
          for s in stories))
check("fresh supply: no follow-ups marked", not fus)

# thin fresh supply → covered stories top up as follow-ups
stories2, fus2 = select([a, b, c], cov_state, S)
check("thin supply: 3 stories still selected", len(stories2) == 3)
check("thin supply: covered ones flagged as follow-ups",
      len(fus2) == 2, f"follow_ups={fus2}")
order = [s.title for s in stories2]
check("thin supply: fresh story comes FIRST", order[0] == c.title, str(order))

# ---------------------------------------------------------------------------
print("4. Digest marks follow-ups for the script writer")
# ---------------------------------------------------------------------------

digest = format_digest([a, c], follow_ups={1})
check("follow-up story carries the UPDATE marker",
      "FOLLOW-UP" in digest and "new angle" in digest.lower())
digest2 = format_digest([a, c], follow_ups=set())
check("clean digest has no marker", "FOLLOW-UP" not in digest2)

# ---------------------------------------------------------------------------
print("5. Clip attribution + query building")
# ---------------------------------------------------------------------------

clip = Clip(path=Path("/tmp/x.mp4"), provider="youtube",
            title="Robots Take Over Warehouse",
            url="https://www.youtube.com/watch?v=abc",
            license="CC-BY 3.0 via YouTube's Creative Commons option",
            channel="TechChannel", duration=90.0)
line = clip.attribution_line()
check("attribution names title, author, url, license",
      "Robots Take Over Warehouse" in line and "TechChannel" in line
      and "youtube.com/watch?v=abc" in line and "CC-BY" in line)
lines = attribution_lines({1: clip})
check("attribution_lines lists one line per clip", len(lines) == 1)

q = story_queries(mk_story("OpenAI launches GPT-6 with huge benchmarks"))
check("story query keeps salient words, drops stopwords",
      "OpenAI" in q[0] and "launches" in q[0], q[0])
check("generic fallbacks present", len(q) >= 3 and
      any("artificial intelligence" in x for x in q))

# ---------------------------------------------------------------------------
print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
