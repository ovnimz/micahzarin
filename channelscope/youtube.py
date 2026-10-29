"""YouTube Data API: your uploads, their metadata, thumbnails, comments, captions."""
from __future__ import annotations

import ssl
import sys
import urllib.request
from pathlib import Path

from guestprep.models import Segment

from .models import ChannelVideo, parse_iso_duration


class NoChannel(RuntimeError):
    pass


def my_channel(yt) -> dict:
    resp = yt.channels().list(part="snippet,contentDetails,statistics", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        raise NoChannel(
            "The Google account you signed in with has no YouTube channel. Run channelscope again and, "
            "in the sign-in window, pick the account or Brand Account channel your videos are on."
        )
    ch = items[0]
    return {
        "id": ch["id"],
        "title": ch["snippet"]["title"],
        "description": ch["snippet"].get("description", ""),
        "published": ch["snippet"]["publishedAt"],
        "uploads": ch["contentDetails"]["relatedPlaylists"]["uploads"],
        "subscribers": int(ch["statistics"].get("subscriberCount", 0)),
        "views": int(ch["statistics"].get("viewCount", 0)),
        "video_count": int(ch["statistics"].get("videoCount", 0)),
    }


def upload_ids(yt, playlist_id: str):
    """Yields video ids, newest first."""
    token = None
    while True:
        resp = yt.playlistItems().list(
            part="contentDetails", playlistId=playlist_id, maxResults=50, pageToken=token
        ).execute()
        for it in resp.get("items", []):
            yield it["contentDetails"]["videoId"]
        token = resp.get("nextPageToken")
        if not token:
            return


def video_details(yt, ids: list[str]) -> list[ChannelVideo]:
    out: list[ChannelVideo] = []
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(
            part="snippet,contentDetails,statistics,status,liveStreamingDetails", id=",".join(ids[i:i + 50])
        ).execute()
        out += [
            parse_video(item) for item in resp.get("items", [])
            if item["snippet"].get("liveBroadcastContent", "none") == "none"  # skip upcoming / live right now
        ]
    return out


def parse_video(item: dict) -> ChannelVideo:
    sn, st = item["snippet"], item.get("statistics", {})
    return ChannelVideo(
        id=item["id"],
        title=sn["title"],
        published=sn["publishedAt"],
        duration=parse_iso_duration(item.get("contentDetails", {}).get("duration", "")),
        description=sn.get("description", ""),
        tags=sn.get("tags", []),
        thumbnail_url=best_thumbnail(sn.get("thumbnails", {})),
        live="liveStreamingDetails" in item,
        privacy=item.get("status", {}).get("privacyStatus", "public"),
        views=int(st.get("viewCount", 0)),
        likes=int(st.get("likeCount", 0)),
        comment_count=int(st.get("commentCount", 0)),
    )


def best_thumbnail(thumbs: dict) -> str:
    for size in ("maxres", "standard", "high", "medium", "default"):
        if size in thumbs:
            return thumbs[size]["url"]
    return ""


def titles_for(yt, ids: list[str]) -> dict[str, dict]:
    """Title + channel for any public videos (used for 'suggested next to')."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(part="snippet", id=",".join(ids[i:i + 50])).execute()
        for it in resp.get("items", []):
            out[it["id"]] = {"title": it["snippet"]["title"], "channel": it["snippet"]["channelTitle"]}
    return out


def download_thumbnail(video: ChannelVideo, directory: Path) -> Path | None:
    if not video.thumbnail_url:
        return None
    # Name by the URL's last path part too, so a changed thumbnail is fetched again.
    name = video.thumbnail_url.rsplit("/", 1)[-1].split("?")[0]
    path = directory / f"{video.id}-{name}"
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        try:
            # certifi's CA bundle: python.org's Python has none until "Install Certificates" is run
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(video.thumbnail_url, timeout=30, context=ctx) as r:
                path.write_bytes(r.read())
        except OSError as e:
            print(f"  couldn't download thumbnail for {video.id}: {e}", file=sys.stderr)
            return None
    return path


def comments(yt, video_id: str, limit: int) -> list[dict]:
    """Top-level comments, most relevant first (YouTube's ranking leans on likes)."""
    from googleapiclient.errors import HttpError

    out: list[dict] = []
    token = None
    while len(out) < limit:
        try:
            resp = yt.commentThreads().list(
                part="snippet", videoId=video_id, order="relevance", textFormat="plainText",
                maxResults=min(100, limit - len(out)), pageToken=token,
            ).execute()
        except HttpError as e:
            if e.resp.status in (403, 404):  # comments disabled / video gone
                return out
            raise
        for it in resp.get("items", []):
            top = it["snippet"]["topLevelComment"]["snippet"]
            out.append({
                "text": top.get("textDisplay") or top.get("textOriginal", ""),
                "likes": int(top.get("likeCount", 0)),
                "replies": int(it["snippet"].get("totalReplyCount", 0)),
            })
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def caption_segments(yt, video_id: str) -> list[Segment]:
    """Your own captions via the API (needs --captions-api scope). Costs 250 quota units."""
    from guestprep.transcripts import parse_cues

    tracks = yt.captions().list(part="snippet", videoId=video_id).execute().get("items", [])
    if not tracks:
        return []
    # Prefer a human-made English track over the automatic one.
    tracks.sort(key=lambda t: (
        not t["snippet"].get("language", "").startswith("en"),
        t["snippet"].get("trackKind") == "asr",
    ))
    raw = yt.captions().download(id=tracks[0]["id"], tfmt="vtt").execute()
    return parse_cues(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)
