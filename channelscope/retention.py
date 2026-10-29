"""Find where viewers leave (and rewatch) on a retention curve, and what was said there.

YouTube gives 100 points per video: the share of viewers still watching at each
1% of the runtime. Every video loses viewers steadily, so a point only counts as
a drop-off when it loses noticeably more than the video's own typical step.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from guestprep.models import Segment, format_timestamp


@dataclass
class Event:
    kind: str  # "drop" or "spike" (rewatching / skipping back)
    start: float  # seconds: the step happens between start and end
    end: float
    size: float  # share of the audience lost (drop) or gained (spike), 0-1
    excess: float  # how much bigger than this video's typical step
    relative: float | None = None  # YouTube's retention vs. similar-length videos, 0-1

    @property
    def label(self) -> str:
        return f"{format_timestamp(self.start)}-{format_timestamp(self.end)}"


def watch_ratio_at(curve, duration: float, seconds: float) -> float | None:
    """Linear interpolation of audienceWatchRatio at a given second."""
    if not curve or not duration:
        return None
    x = seconds / duration
    prev = (0.0, 1.0)
    for r, w, *_ in curve:
        if r >= x:
            (r0, w0), (r1, w1) = prev, (r, w)
            return w0 + (w1 - w0) * ((x - r0) / (r1 - r0)) if r1 > r0 else w1
        prev = (r, w)
    return curve[-1][1]


def summary(curve, duration: float) -> dict:
    """Headline retention numbers for one video."""
    rel = [p[2] for p in curve if len(p) > 2 and p[2] is not None]
    return {
        "at_30s": watch_ratio_at(curve, duration, 30),
        "at_60s": watch_ratio_at(curve, duration, 60),
        "at_25pct": watch_ratio_at(curve, duration, 0.25 * duration),
        "at_50pct": watch_ratio_at(curve, duration, 0.5 * duration),
        "at_90pct": watch_ratio_at(curve, duration, 0.9 * duration),
        "relative_avg": sum(rel) / len(rel) if rel else None,
    }


def events(
    curve,
    duration: float,
    max_drops: int = 5,
    max_spikes: int = 3,
    skip_start: float = 60.0,
    skip_end_ratio: float = 0.97,
) -> list[Event]:
    """The steepest unusual drops and the biggest rewatch spikes.

    The opening minute and the last 3% (end screen) are skipped: everyone loses
    viewers there, and the opening gets its own analysis.
    """
    pts = [(p[0], p[1], p[2] if len(p) > 2 else None) for p in curve]
    if len(pts) < 10 or not duration:
        return []
    raw = [(r0 * duration, r1 * duration, w0 - w1, rel, r1) for (r0, w0, _), (r1, w1, rel) in zip(pts, pts[1:])]
    in_body = lambda s: s[0] >= skip_start and s[4] <= skip_end_ratio
    if not any(in_body(s) for s in raw):
        return []
    typical = median(max(s[2], 0.0) for s in raw if in_body(s))
    # After a rewatch bump the curve falls back to its trend; that fall isn't people leaving.
    steps = [raw[0]] + [
        (a, b, d - (typical - prev[2]) if prev[2] < 0 < d else d, rel, r)
        for prev, (a, b, d, rel, r) in zip(raw, raw[1:])
    ]
    body = [s for s in steps if in_body(s)]
    floor = max(typical * 1.5, 0.004)  # ignore noise: under 0.4% of the audience isn't a moment

    drops = [Event("drop", a, b, d, d - typical, rel) for a, b, d, rel, _ in body if d > floor]
    spikes = [Event("spike", a, b, -d, -d, rel) for a, b, d, rel, _ in body if -d >= 0.004]
    return _spread(drops, max_drops, duration) + _spread(spikes, max_spikes, duration)


def _spread(evs: list[Event], k: int, duration: float) -> list[Event]:
    """Biggest first, skipping ones within 2% of the runtime of an already-picked event."""
    picked: list[Event] = []
    gap = max(0.02 * duration, 30)
    for e in sorted(evs, key=lambda e: -e.excess):
        if all(abs(e.start - p.start) > gap for p in picked):
            picked.append(e)
        if len(picked) == k:
            break
    return sorted(picked, key=lambda e: e.start)


def transcript_window(segments: list[Segment], start: float, end: float) -> str:
    """Words spoken from `start` to `end` seconds."""
    words = [s.text for s in segments if s.start is not None and start <= s.start <= end]
    return " ".join(" ".join(words).split())


def event_context(segments: list[Segment], e: Event, lead: float = 45.0, tail: float = 15.0) -> str:
    """What was said in the run-up to (and during) the step."""
    return transcript_window(segments, max(e.start - lead, 0), e.end + tail)
