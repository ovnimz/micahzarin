"""Per episode: what was happening when viewers left or rewatched, and what commenters said."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from guestprep.llm import call_json
from guestprep.models import Segment, format_timestamp
from guestprep.transcripts import to_prompt_text

from . import features, retention
from .models import ChannelVideo

PROMPT_VERSION = "v1"

CATEGORIES = [
    "sponsor/ad read", "slow or long intro", "host monologue / long question", "guest rambling / tangent",
    "repeated or familiar material", "jargon / too technical", "topic change", "low energy / lull",
    "audio or video problem", "promise paid off / natural exit", "end segment / wrap-up",
    "story", "strong claim / controversy", "emotional moment", "humour", "practical advice",
    "surprising fact or insight", "conflict / pushback", "other",
]

SYSTEM = """\
You are a YouTube strategist reviewing one episode of a creator's own \
interview show to learn what keeps their audience watching and what makes \
them leave. You get the episode's retention events from YouTube Analytics, \
the timestamps viewers cite in comments, the top comments, and the \
transcript (YouTube captions: no speaker labels, so infer who is talking; the \
channel's host asks the questions).

Retention events:
- A drop (d1, d2, ...) is a stretch where noticeably more viewers left than \
usual for this video. Look at what was said in the minute or so before and \
during it. Name what was happening and the most likely reason people left. \
Be concrete ("host's 90-second setup before asking about X", "third \
sponsor read"), not generic. If nothing in the transcript explains it, say so.
- A spike (s1, s2, ...) is a stretch people rewatched or skipped to. Say \
what made it worth coming back to.

Opening: say what happens in the first 60 seconds (is there a hook? does it \
promise what the title and thumbnail promised? how fast does it get to the \
point?) and how well it held viewers.

Comments: summarise what viewers liked, disliked, asked for, questions they \
wished the host had asked, and what they said about the host or the \
interviewing. Paraphrase; keep each item short; skip spam and generic praise.

Interview technique: specific things the host did, good or bad, with \
timestamps: question types, follow-ups or missed follow-ups, interruptions, \
talking too much, pacing, energy, how the conversation was steered. Tie them \
to retention events or comments when you can.

Only report what the data supports. Use [m:ss] / [h:mm:ss] timestamps from \
the transcript."""

SCHEMA = {
    "type": "object",
    "properties": {
        "opening": {
            "type": "object",
            "properties": {
                "what_happens": {"type": "string"},
                "assessment": {"type": "string"},
            },
            "required": ["what_happens", "assessment"],
            "additionalProperties": False,
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "what_was_happening": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "likely_reason": {"type": "string"},
                },
                "required": ["id", "what_was_happening", "category", "likely_reason"],
                "additionalProperties": False,
            },
        },
        "cited_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"timestamp": {"type": "string"}, "what": {"type": "string"}},
                "required": ["timestamp", "what"],
                "additionalProperties": False,
            },
        },
        "comments": {
            "type": "object",
            "properties": {
                k: {"type": "array", "items": {"type": "string"}}
                for k in ("liked", "disliked", "requests", "wished_asked", "about_host")
            },
            "required": ["liked", "disliked", "requests", "wished_asked", "about_host"],
            "additionalProperties": False,
        },
        "interview_technique": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string"},
                    "observation": {"type": "string"},
                    "effect": {"type": "string", "enum": ["helped", "hurt", "unclear"]},
                },
                "required": ["timestamp", "observation", "effect"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["opening", "events", "cited_moments", "comments", "interview_technique"],
    "additionalProperties": False,
}


def event_ids(events: list[retention.Event]) -> dict[str, retention.Event]:
    drops = [e for e in events if e.kind == "drop"]
    spikes = [e for e in events if e.kind == "spike"]
    return {**{f"d{i}": e for i, e in enumerate(drops, 1)}, **{f"s{i}": e for i, e in enumerate(spikes, 1)}}


def build_prompt(video: ChannelVideo, segments: list[Segment], events: dict[str, retention.Event], max_comments: int = 80) -> str:
    r = retention.summary(video.retention, video.duration) if video.retention else {}
    pct = lambda x: f"{100 * x:.0f}%" if x is not None else "?"
    lines = [
        f"Episode title: {video.title}",
        f"Published: {video.published[:10]} · length {format_timestamp(video.duration)} · {video.views:,} views",
        f"Average view: {video.avg_view_pct or 0:.0f}% of the episode · still watching at 30s: {pct(r.get('at_30s'))}, "
        f"60s: {pct(r.get('at_60s'))}, halfway: {pct(r.get('at_50pct'))}",
        "",
        "Retention events:",
    ]
    for eid, e in events.items():
        what = "lost" if e.kind == "drop" else "gained (rewatches)"
        lines.append(f"- {eid} {e.kind} {e.label}: {what} {100 * e.size:.1f}% of the audience")
    if not events:
        lines.append("- (no unusual drops or spikes)")

    cited = Counter()
    for c in video.comments:
        for s in features.comment_timestamps(c["text"], video.duration or None):
            cited[int(s // 30) * 30] += 1 + c["likes"] // 10
    if cited:
        lines += ["", "Moments viewers cite in comments (weighted by likes):"]
        lines += [f"- {format_timestamp(t)} ({n})" for t, n in cited.most_common(10)]

    top = sorted(video.comments, key=lambda c: -c["likes"])[:max_comments]
    lines += ["", f"Top comments ({len(top)} of {video.comment_count:,}):"]
    lines += [f"- [{c['likes']} likes] {' '.join(c['text'].split())[:600]}" for c in top] or ["- (none)"]
    if segments:
        lines += ["", "<transcript>", to_prompt_text(segments), "</transcript>"]
    else:
        lines += ["", "(No transcript available: work from the numbers and comments only.)"]
    return "\n".join(lines)


def analyse(client, model: str, video: ChannelVideo, segments: list[Segment], cache_dir: Path, effort: str = "medium") -> dict:
    evs = event_ids(retention.events(video.retention, video.duration))
    # Cache key includes the event times and comment count, so new data re-runs it.
    sig = "-".join(f"{int(e.start)}" for e in evs.values()) + f"-{len(video.comments)}-{bool(segments)}"
    cache = cache_dir / "moments" / f"{PROMPT_VERSION}-{model}-{video.id}-{hashlib.sha1(sig.encode()).hexdigest()[:8]}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
    else:
        data = call_json(
            client, model=model, system=SYSTEM, user=build_prompt(video, segments, evs), schema=SCHEMA, effort=effort
        )
        if data is None:
            return {}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    # Attach the measured numbers to Claude's reading of each event.
    for item in data.get("events", []):
        e = evs.get(item["id"])
        if e:
            item.update(kind=e.kind, start=e.start, end=e.end, size=e.size, context=retention.event_context(segments, e))
    return data
