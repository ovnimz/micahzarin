from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

SHORT_MAX_SECONDS = 180  # YouTube Shorts can be up to 3 minutes


@dataclass
class ChannelVideo:
    id: str
    title: str
    published: str  # ISO 8601, as the Data API returns it
    duration: int = 0  # seconds
    description: str = ""
    tags: list[str] = field(default_factory=list)
    thumbnail_url: str = ""
    live: bool = False
    privacy: str = "public"

    # Public counters (Data API)
    views: int = 0
    likes: int = 0
    comment_count: int = 0

    # YouTube Analytics, lifetime
    watch_minutes: float | None = None
    avg_view_duration: float | None = None  # seconds
    avg_view_pct: float | None = None  # 0-100
    shares: int | None = None
    subs_gained: int | None = None
    subs_lost: int | None = None

    # Reach: from a Studio export or the Reporting API
    impressions: int | None = None
    ctr: float | None = None  # 0-1
    ctr_source: str = ""

    # Where views came from: {"YT_SEARCH": 1200, "RELATED_VIDEO": 3400, ...}
    traffic: dict[str, int] = field(default_factory=dict)
    search_terms: list[tuple[str, int]] = field(default_factory=list)
    # Videos (any channel) this one was suggested next to, with views from there
    suggested_from: list[tuple[str, int]] = field(default_factory=list)
    # [(elapsed ratio 0-1, audienceWatchRatio, relativeRetentionPerformance)]
    retention: list[tuple[float, float, float | None]] = field(default_factory=list)

    # Filled in later
    thumbnail_path: str = ""
    thumbnail_pixels: dict = field(default_factory=dict)
    thumbnail_tags: dict = field(default_factory=dict)  # from Claude
    comments: list[dict] = field(default_factory=list)  # {"text", "likes", "replies"}
    has_transcript: bool = False
    # What was said around each retention event: {str(round(event start seconds)): text}
    excerpts: dict[str, str] = field(default_factory=dict)
    moments: dict = field(default_factory=dict)  # from Claude

    @property
    def is_short(self) -> bool:
        return 0 < self.duration <= SHORT_MAX_SECONDS

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    def link(self, seconds: float | None = None) -> str:
        if seconds is not None and seconds > 0:
            return f"{self.url}&t={int(seconds)}s"
        return self.url

    @property
    def published_dt(self) -> datetime:
        return datetime.fromisoformat(self.published.replace("Z", "+00:00"))

    def age_days(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max((now - self.published_dt).total_seconds() / 86400, 0.0)

    @property
    def subs_per_1k_views(self) -> float | None:
        if self.subs_gained is None or not self.views:
            return None
        return 1000 * (self.subs_gained - (self.subs_lost or 0)) / self.views

    @property
    def clicks(self) -> float | None:
        if self.impressions is None or self.ctr is None:
            return None
        return self.impressions * self.ctr

    def traffic_share(self) -> dict[str, float]:
        total = sum(self.traffic.values())
        return {k: v / total for k, v in sorted(self.traffic.items(), key=lambda kv: -kv[1])} if total else {}


def parse_iso_duration(s: str) -> int:
    """'PT1H2M3S' -> 3723. Days appear on very long streams ('P1DT2H')."""
    import re

    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s or "")
    if not m:
        return 0
    d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + sec
