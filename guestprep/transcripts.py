"""Fetch (and cache) transcripts: YouTube captions, or local files."""
from __future__ import annotations

import json
import re
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import Segment, Video, parse_timestamp

LANGS = ["en", "en-US", "en-GB", "en-orig"]


class TranscriptBlocked(RuntimeError):
    """YouTube refused us (bot check / IP block). Retrying other videos won't help."""


class TranscriptRateLimited(TranscriptBlocked):
    """HTTP 429: too many requests recently. Goes away if we wait."""


def ytdlp_opts(proxy: str | None, cookies: str | None, **extra) -> dict:
    """Common yt-dlp options. `cookies` is a cookies.txt path or "browser:<name>"."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, **extra}
    if proxy:
        opts["proxy"] = proxy
    if cookies and cookies.startswith("browser:"):
        opts["cookiesfrombrowser"] = (cookies.split(":", 1)[1],)
    elif cookies:
        opts["cookiefile"] = cookies
    return opts


# ---------------------------------------------------------------- YouTube

def _via_transcript_api(video_id: str, proxy: str | None) -> list[Segment]:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig

    cfg = GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None
    fetched = YouTubeTranscriptApi(proxy_config=cfg).fetch(video_id, languages=LANGS)
    return [Segment(start=s.start, text=s.text) for s in fetched]


class _SilentLogger:
    """yt-dlp prints errors itself even with quiet=True; we report them our own way."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _via_ytdlp(video_id: str, proxy: str | None, cookies: str | None) -> list[Segment]:
    import yt_dlp

    with tempfile.TemporaryDirectory() as tmp:
        opts = ytdlp_opts(
            proxy,
            cookies,
            writesubtitles=True,
            writeautomaticsub=True,
            # Each language is a separate request; asking for many trips YouTube's rate limit.
            subtitleslangs=["en", "en-US", "en-GB"],
            subtitlesformat="json3",
            sleep_interval_subtitles=1,
            outtmpl=str(Path(tmp) / "%(id)s.%(ext)s"),
            logger=_SilentLogger(),
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        files = sorted(Path(tmp).glob(f"{video_id}*.json3"))
        if not files:
            return []
        # Prefer plain "en" (usually human captions) over regional/auto variants.
        files.sort(key=lambda p: (p.suffixes[-2:-1] != [".en"], p.name))
        return parse_json3(files[0].read_text(encoding="utf-8"))


def parse_json3(raw: str) -> list[Segment]:
    data = json.loads(raw)
    out: list[Segment] = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
        if text:
            out.append(Segment(start=ev.get("tStartMs", 0) / 1000, text=text.replace("\n", " ")))
    return out


def _looks_blocked(err: Exception) -> bool:
    msg = f"{type(err).__name__} {err}".lower()
    return any(s in msg for s in ("ipblocked", "requestblocked", "not a bot", "429", "too many requests"))


def cache_path(video: Video, cache_dir: Path) -> Path:
    return cache_dir / "transcripts" / f"{video.id}.json"


def fetch_youtube(
    video: Video,
    cache_dir: Path,
    proxy: str | None = None,
    cookies: str | None = None,
) -> list[Segment]:
    cache = cache_path(video, cache_dir)
    if cache.exists():
        return [Segment(**s) for s in json.loads(cache.read_text())]

    segments: list[Segment] = []
    errors: list[Exception] = []
    for fetch in (lambda: _via_transcript_api(video.id, proxy), lambda: _via_ytdlp(video.id, proxy, cookies)):
        try:
            segments = fetch()
        except Exception as e:
            errors.append(e)
            continue
        if segments:
            break

    if not segments:
        if errors and all(_looks_blocked(e) for e in errors):
            msgs = " ".join(str(e).lower() for e in errors)
            first_line = str(errors[-1]).splitlines()[0]
            if "not a bot" not in msgs and ("429" in msgs or "too many requests" in msgs):
                raise TranscriptRateLimited(first_line)
            raise TranscriptBlocked(first_line)
        if errors:
            print(f"  no transcript for {video.id}: {str(errors[-1]).splitlines()[0]}", file=sys.stderr)
        return []

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([asdict(s) for s in segments]))
    return segments


# ---------------------------------------------------------------- local files

_CUE = re.compile(r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->")
_TAG = re.compile(r"<[^>]+>")


def parse_cues(raw: str) -> list[Segment]:
    """.vtt / .srt. Collapses the rolling duplicate lines auto-captions produce."""
    out: list[Segment] = []
    start: float | None = None
    last_line = ""
    for line in raw.splitlines():
        line = line.strip()
        if _CUE.match(line):
            start = parse_timestamp(line.split("-->")[0].strip().replace(",", "."))
            continue
        if not line or line.isdigit() or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")):
            continue
        text = _TAG.sub("", line).strip()
        if text and text != last_line:
            out.append(Segment(start=start, text=text))
            last_line = text
    return out


_INLINE_TS = re.compile(r"^\s*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]?\s*[-–:]?\s*")


def parse_plaintext(raw: str) -> list[Segment]:
    """Plain text, optionally with [mm:ss] / hh:mm:ss line prefixes (podcast-site transcripts)."""
    out: list[Segment] = []
    for para in re.split(r"\n\s*\n|\n(?=\s*[\[(]?\d{1,2}:\d{2})", raw):
        para = para.strip()
        if not para:
            continue
        m = _INLINE_TS.match(para)
        start = parse_timestamp(m.group(1)) if m else None
        text = para[m.end():] if m else para
        out.append(Segment(start=start, text=" ".join(text.split())))
    return out


def load_local(path: Path) -> list[Segment]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix in (".vtt", ".srt"):
        return parse_cues(raw)
    if suffix == ".json3":
        return parse_json3(raw)
    return parse_plaintext(raw)


def local_videos(directory: Path) -> list[tuple[Video, list[Segment]]]:
    out = []
    for p in sorted(directory.iterdir()):
        if p.suffix.lower() not in (".txt", ".md", ".vtt", ".srt", ".json3"):
            continue
        segs = load_local(p)
        if segs:
            v = Video(id=f"local:{p.stem}", title=p.stem, channel="(local file)", confirmed=True, local_path=str(p))
            out.append((v, segs))
    return out


# ---------------------------------------------------------------- formatting

def to_prompt_text(segments: list[Segment], window: float = 20.0) -> str:
    """Merge caption fragments into ~20s lines prefixed with [m:ss] so Claude can cite times."""
    from .models import format_timestamp

    lines: list[str] = []
    buf: list[str] = []
    buf_start: float | None = None
    for s in segments:
        if buf and (s.start is None or buf_start is None or s.start - buf_start >= window):
            lines.append(_line(buf_start, buf, format_timestamp))
            buf = []
        if not buf:
            buf_start = s.start
        buf.append(s.text)
    if buf:
        lines.append(_line(buf_start, buf, format_timestamp))
    return "\n".join(lines)


def _line(start, buf, fmt) -> str:
    text = " ".join(" ".join(buf).split())
    return f"[{fmt(start)}] {text}" if start is not None else text
