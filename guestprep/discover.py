"""Find the guest's prior podcast appearances on YouTube via yt-dlp search."""
from __future__ import annotations

import re
import sys

from .models import Video

SEARCH_TEMPLATES = [
    '"{name}" podcast',
    '"{name}" interview',
    '"{name}" full episode',
    "{name} conversation",
]

_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")


def video_id_from_url(url: str) -> str | None:
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    m = _VIDEO_ID.search(url)
    return m.group(1) if m else None


def name_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", name.lower()) if len(t) > 1]


def mentions_guest(text: str, name: str) -> bool:
    """Full name, or the last name on its own (hosts say "Housel" a lot)."""
    text = text.lower()
    tokens = name_tokens(name)
    if not tokens:
        return False
    if " ".join(tokens) in text:
        return True
    last = tokens[-1]
    return len(last) >= 4 and re.search(rf"\b{re.escape(last)}\b", text) is not None


def _entry_to_video(entry: dict, name: str) -> Video | None:
    vid = entry.get("id")
    if not vid or len(vid) != 11:
        return None  # channels/playlists that show up in search results
    v = Video(
        id=vid,
        title=entry.get("title") or "",
        channel=entry.get("channel") or entry.get("uploader") or "",
        duration=int(entry["duration"]) if entry.get("duration") else None,
        url=entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
        description=entry.get("description") or "",
    )
    v.confirmed = mentions_guest(f"{v.title}\n{v.description}\n{v.channel}", name)
    return v


def _is_duplicate(v: Video, kept: list[Video]) -> bool:
    """Same episode re-uploaded (full ep + "full episode" re-post, etc.)."""
    for k in kept:
        if k.id == v.id:
            return True
        if (
            v.duration and k.duration
            and k.channel == v.channel
            and abs(k.duration - v.duration) <= 90
        ):
            return True
    return False


def search_youtube(
    name: str,
    per_query: int = 15,
    min_minutes: float = 20,
    proxy: str | None = None,
    cookies: str | None = None,
    extra_queries: list[str] | None = None,
) -> list[Video]:
    """Run several searches and return de-duplicated, long-form candidates.

    Confirmed hits (guest named in title/description/channel) come first;
    the rest are kept as unconfirmed and verified later against the transcript.
    """
    import yt_dlp

    opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    if proxy:
        opts["proxy"] = proxy
    if cookies:
        opts["cookiefile"] = cookies

    queries = [t.format(name=name) for t in SEARCH_TEMPLATES] + list(extra_queries or [])
    found: list[Video] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        for q in queries:
            try:
                info = ydl.extract_info(f"ytsearch{per_query}:{q}", download=False)
            except Exception as e:  # yt-dlp raises a zoo of error types
                print(f"  search failed for {q!r}: {e}", file=sys.stderr)
                continue
            for entry in (info or {}).get("entries") or []:
                v = _entry_to_video(entry, name)
                if v is None:
                    continue
                if v.duration is not None and v.duration < min_minutes * 60:
                    continue  # clips, shorts, trailers
                if not _is_duplicate(v, found):
                    found.append(v)

    # The guest's own channel is mostly solo episodes, not interviews: try those last.
    own = lambda v: " ".join(name_tokens(name)) in " ".join(name_tokens(v.channel))
    found.sort(key=lambda v: (own(v), not v.confirmed, -(v.duration or 0)))
    return found


def videos_from_urls(urls: list[str], name: str) -> list[Video]:
    """User-supplied links are trusted as real appearances."""
    out: list[Video] = []
    for u in urls:
        vid = video_id_from_url(u)
        if not vid:
            print(f"  skipping unrecognised URL: {u}", file=sys.stderr)
            continue
        if any(v.id == vid for v in out):
            continue
        out.append(Video(id=vid, title=vid, url=f"https://www.youtube.com/watch?v={vid}", confirmed=True))
    return out


def fill_metadata(videos: list[Video], proxy: str | None = None, cookies: str | None = None) -> None:
    """Best-effort title/channel lookup for bare URLs; failures are harmless."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True}
    if proxy:
        opts["proxy"] = proxy
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        for v in videos:
            if v.title != v.id:
                continue
            try:
                info = ydl.extract_info(v.url, download=False) or {}
            except Exception:
                continue
            v.title = info.get("title") or v.title
            v.channel = info.get("channel") or info.get("uploader") or ""
            if info.get("duration"):
                v.duration = int(info["duration"])
