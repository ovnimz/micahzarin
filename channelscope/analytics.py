"""YouTube Analytics API: watch time, retention curves, traffic sources, audience."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .models import ChannelVideo

VIDEO_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained,subscribersLost"
)


def query(ya, **params) -> list[dict]:
    resp = ya.reports().query(ids="channel==MINE", **params).execute()
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in resp.get("rows") or []]


def _safe(ya, label: str, **params) -> list[dict]:
    """A single report failing (e.g. a video too new for retention data) shouldn't stop the run."""
    from googleapiclient.errors import HttpError

    try:
        return query(ya, **params)
    except HttpError as e:
        reason = e.reason if hasattr(e, "reason") else str(e)
        print(f"  analytics: {label} unavailable ({reason})", file=sys.stderr)
        return []


def video_totals(ya, ids: list[str], start: str, end: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        for row in _safe(
            ya, "per-video totals",
            startDate=start, endDate=end, dimensions="video", metrics=VIDEO_METRICS,
            filters="video==" + ",".join(batch), sort="-views", maxResults=200,
        ):
            out[row["video"]] = row
    return out


def apply_totals(v: ChannelVideo, row: dict) -> None:
    v.watch_minutes = row.get("estimatedMinutesWatched")
    v.avg_view_duration = row.get("averageViewDuration")
    v.avg_view_pct = row.get("averageViewPercentage")
    v.shares = row.get("shares")
    v.subs_gained = row.get("subscribersGained")
    v.subs_lost = row.get("subscribersLost")


def video_detail(ya, video_id: str, start: str, end: str) -> dict:
    """Everything per-video that needs its own query. Cached by the caller."""
    vf = f"video=={video_id}"
    retention = _safe(
        ya, f"retention for {video_id}",
        startDate=start, endDate=end, dimensions="elapsedVideoTimeRatio",
        metrics="audienceWatchRatio,relativeRetentionPerformance", filters=f"{vf};audienceType==ORGANIC",
    ) or _safe(
        ya, f"retention for {video_id}",
        startDate=start, endDate=end, dimensions="elapsedVideoTimeRatio",
        metrics="audienceWatchRatio,relativeRetentionPerformance", filters=vf,
    )
    traffic = _safe(
        ya, f"traffic sources for {video_id}",
        startDate=start, endDate=end, dimensions="insightTrafficSourceType",
        metrics="views,estimatedMinutesWatched", filters=vf,
    )
    search = _safe(
        ya, f"search terms for {video_id}",
        startDate=start, endDate=end, dimensions="insightTrafficSourceDetail", metrics="views",
        filters=f"{vf};insightTrafficSourceType==YT_SEARCH", sort="-views", maxResults=25,
    )
    suggested = _safe(
        ya, f"suggested-video sources for {video_id}",
        startDate=start, endDate=end, dimensions="insightTrafficSourceDetail", metrics="views",
        filters=f"{vf};insightTrafficSourceType==RELATED_VIDEO", sort="-views", maxResults=15,
    )
    return {
        "retention": [
            [r["elapsedVideoTimeRatio"], r["audienceWatchRatio"], r.get("relativeRetentionPerformance")]
            for r in sorted(retention, key=lambda r: r["elapsedVideoTimeRatio"])
        ],
        "traffic": {r["insightTrafficSourceType"]: int(r["views"]) for r in traffic},
        "search_terms": [[r["insightTrafficSourceDetail"], int(r["views"])] for r in search],
        "suggested_from": [[r["insightTrafficSourceDetail"], int(r["views"])] for r in suggested],
    }


def apply_detail(v: ChannelVideo, d: dict) -> None:
    v.retention = [tuple(p) for p in d.get("retention", [])]
    v.traffic = d.get("traffic", {})
    v.search_terms = [tuple(t) for t in d.get("search_terms", [])]
    v.suggested_from = [tuple(t) for t in d.get("suggested_from", [])]


def channel_audience(ya, start: str, end: str) -> dict:
    demo = _safe(ya, "demographics", startDate=start, endDate=end, dimensions="ageGroup,gender", metrics="viewerPercentage")
    countries = _safe(
        ya, "geography", startDate=start, endDate=end, dimensions="country",
        metrics="views,estimatedMinutesWatched", sort="-views",
    )[:15]
    devices = _safe(ya, "devices", startDate=start, endDate=end, dimensions="deviceType", metrics="views,estimatedMinutesWatched")
    subscribed = _safe(
        ya, "subscriber split", startDate=start, endDate=end, dimensions="subscribedStatus",
        metrics="views,estimatedMinutesWatched,averageViewDuration",
    )
    traffic = _safe(
        ya, "channel traffic sources", startDate=start, endDate=end,
        dimensions="insightTrafficSourceType", metrics="views,estimatedMinutesWatched",
    )
    search = _safe(
        ya, "channel search terms", startDate=start, endDate=end, dimensions="insightTrafficSourceDetail",
        metrics="views", filters="insightTrafficSourceType==YT_SEARCH", sort="-views", maxResults=25,
    )
    return {
        "age_gender": demo,
        "countries": countries,
        "devices": devices,
        "subscribed": subscribed,
        "traffic": traffic,
        "search_terms": [[r["insightTrafficSourceDetail"], int(r["views"])] for r in search],
    }


def cached(path: Path, max_age_hours: float, fetch):
    """JSON cache with a time limit: analytics keep changing, but not hour to hour."""
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600:
        return json.loads(path.read_text())
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data
