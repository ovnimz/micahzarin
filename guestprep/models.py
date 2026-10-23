from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Video:
    id: str
    title: str
    channel: str = ""
    duration: int | None = None  # seconds
    url: str = ""
    description: str = ""
    # True once we're confident the guest is actually in this video
    # (name in title/description/channel, or found in the transcript).
    confirmed: bool = False
    local_path: str = ""  # set for transcripts supplied from disk
    page_url: str = ""  # set for web pages found with --web

    def link(self, seconds: float | None = None) -> str:
        if self.local_path:
            return self.local_path
        if self.page_url:
            return self.page_url
        base = f"https://www.youtube.com/watch?v={self.id}"
        if seconds is not None and seconds > 0:
            return f"{base}&t={int(seconds)}s"
        return base


@dataclass
class Segment:
    start: float | None  # seconds; None when the source has no timestamps
    text: str


@dataclass
class Question:
    episode_id: str
    question: str  # as asked, lightly cleaned up
    gist: str  # show-neutral phrasing used for matching across interviews
    topic: str = ""
    seconds: float | None = None


@dataclass
class Episode:
    video: Video
    segments: list[Segment] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    unexplored_threads: list[str] = field(default_factory=list)


@dataclass
class Cluster:
    label: str
    canonical_question: str
    members: list[Question]
    on_topic: bool = True  # only meaningful with --topic

    @property
    def episode_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for q in self.members:
            seen.setdefault(q.episode_id, None)
        return list(seen)

    @property
    def times_asked(self) -> int:
        """Distinct interviews that asked this, not raw repeats within one show."""
        return len(self.episode_ids)


def parse_timestamp(ts: str | None) -> float | None:
    """'1:02:03' / '12:34' / '75' -> seconds."""
    if not ts:
        return None
    ts = ts.strip().strip("[]")
    try:
        parts = [float(p) for p in ts.split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
