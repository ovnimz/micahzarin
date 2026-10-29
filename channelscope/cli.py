"""channelscope: model your YouTube audience from your own channel's analytics.

Pulls CTR, impressions, watch time, retention curves, traffic sources, search
terms, comments, thumbnails and transcripts for your recent videos, finds the
moments people leave or rewatch, and has Claude turn it into an audience model
and a title / thumbnail / interviewing playbook.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from guestprep import transcripts
from guestprep.cli import BLOCKED_HELP, NO_KEY_HELP, RATE_LIMIT_HELP
from guestprep.llm import DEFAULT_MODEL, FAST_MODEL, LLMError, make_client
from guestprep.models import Segment, Video

from . import analytics, auth, features, moments, reach, report, retention, stats, strategy, vision, youtube
from .models import ChannelVideo

API_NAMES = {
    "youtube.googleapis.com": "YouTube Data API v3",
    "youtubeanalytics.googleapis.com": "YouTube Analytics API",
    "youtubereporting.googleapis.com": "YouTube Reporting API",
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "channel"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="channelscope", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", "--max-videos", type=int, default=50, help="analyse your most recent N long-form videos (default 50; 0 = all)")
    p.add_argument("--since", help="only videos published on or after this date (YYYY-MM-DD)")
    p.add_argument("--shorts", action="store_true", help="include Shorts (left out by default: they're packaged and watched differently)")
    p.add_argument("--studio-csv", action="append", default=[], type=Path, metavar="CSV",
                   help="YouTube Studio export with Impressions and CTR (Analytics > Advanced mode > Export); repeatable")
    p.add_argument("--no-reach", action="store_true", help="don't use the YouTube Reporting API for impressions/CTR")
    p.add_argument("--comments", type=int, default=100, help="top comments to read per video (default 100; 0 = none)")
    p.add_argument("--no-transcripts", action="store_true", help="skip transcripts (drop-offs are then explained from comments only)")
    p.add_argument("--captions-api", action="store_true",
                   help="fetch your captions through the YouTube API when the normal route is blocked (asks for an extra permission; 250 quota units per video)")
    p.add_argument("--next", default="", metavar="TEXT", help='your next episode, e.g. "Jane Doe, ex-FBI negotiator, on getting people to talk"; adds titles and thumbnail ideas for it')
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"model for the audience model and playbook (default {DEFAULT_MODEL})")
    p.add_argument("--reader-model", default=FAST_MODEL, help=f"model that reads thumbnails, transcripts and comments (default {FAST_MODEL})")
    p.add_argument("--fast", action="store_true", help="quickest run: every step on the reader model, at lower effort")
    p.add_argument("--no-llm", action="store_true", help="numbers only: no Claude, no API key needed")
    p.add_argument("-o", "--out", type=Path, help="report path (default channelscope-<channel>.md); .json, .csv and a thumbnails folder go alongside")
    p.add_argument("--client-secret", type=Path, default=auth.CONFIG_DIR / "client_secret.json", help="Google OAuth client file (see README)")
    p.add_argument("--account", default="default", help="name for this sign-in, if you have several channels (default: default)")
    p.add_argument("--logout", action="store_true", help="forget the saved Google sign-in and exit")
    p.add_argument("--refresh", action="store_true", help="re-download analytics and comments instead of using today's cache")
    p.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "channelscope")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="for transcripts: use YouTube cookies from chrome / safari / firefox / edge / brave")
    p.add_argument("--cookies", help="for transcripts: Netscape cookies.txt")
    p.add_argument("--proxy", help="for transcripts: HTTP(S) proxy URL")
    return p.parse_args(argv)


def collect_videos(yt, channel: dict, args) -> tuple[list[ChannelVideo], int]:
    """Your newest uploads that fit the filters. -> (videos, Shorts left out)."""
    picked: list[ChannelVideo] = []
    skipped_shorts = 0
    ids = youtube.upload_ids(yt, channel["uploads"])
    done = False
    while not done:
        chunk = [i for _, i in zip(range(50), ids)]
        if not chunk:
            break
        for v in sorted(youtube.video_details(yt, chunk), key=lambda v: v.published, reverse=True):
            if args.since and v.published[:10] < args.since:
                done = True  # the uploads playlist is newest first
                break
            if v.privacy == "private":
                continue
            if v.is_short and not args.shorts:
                skipped_shorts += 1
                continue
            picked.append(v)
            if args.max_videos and len(picked) >= args.max_videos:
                done = True
                break
    return picked, skipped_shorts


def get_transcript(v: ChannelVideo, yt, args, state: dict) -> list[Segment]:
    """Public captions first (free); your own captions through the API if that's blocked."""
    gv = Video(id=v.id, title=v.title)
    cache_dir = args.cache_dir / "transcripts-cache"
    cache = transcripts.cache_path(gv, cache_dir)
    if not state.get("blocked"):
        was_cached = cache.exists()
        try:
            segs = transcripts.fetch_youtube(gv, cache_dir, args.proxy, args.cookies)
            if not was_cached:
                time.sleep(1)  # YouTube rate-limits bursts
            if segs:
                return segs
        except transcripts.TranscriptBlocked as e:
            state["blocked"] = True
            if not args.captions_api:
                help_text = RATE_LIMIT_HELP if isinstance(e, transcripts.TranscriptRateLimited) else BLOCKED_HELP
                print(f"\n  transcripts blocked: {e}\n\n{help_text}\n  - or add --captions-api to fetch your own captions through the YouTube API\n", file=sys.stderr)
    if args.captions_api:
        from googleapiclient.errors import HttpError

        try:
            segs = youtube.caption_segments(yt, v.id)
        except HttpError as e:
            print(f"  captions API failed for {v.id}: {e.reason if hasattr(e, 'reason') else e}", file=sys.stderr)
            return []
        if segs:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps([{"start": s.start, "text": s.text} for s in segs]))
        return segs
    return []


def gather(args) -> dict:
    """Everything from Google. Returns the pieces the analysis needs."""
    scopes = auth.READ_SCOPES + ([auth.CAPTIONS_SCOPE] if args.captions_api else [])
    token = auth.CONFIG_DIR / f"token-{_slug(args.account)}.json"
    creds = auth.credentials(args.client_secret, token, scopes)
    yt, ya, yr = auth.services(creds)

    channel = youtube.my_channel(yt)
    print(f"Channel: {channel['title']} ({channel['subscribers']:,} subscribers)", file=sys.stderr)
    cache = args.cache_dir / channel["id"]
    max_age = 0 if args.refresh else 20  # hours

    videos, skipped_shorts = collect_videos(yt, channel, args)
    if not videos:
        return {"channel": channel, "videos": []}
    print(f"  {len(videos)} long-form videos" + (f" ({skipped_shorts} Shorts left out)" if skipped_shorts else ""), file=sys.stderr)

    start, end = channel["published"][:10], date.today().isoformat()
    print("Fetching analytics (watch time, retention, traffic, search terms)...", file=sys.stderr)
    totals = analytics.video_totals(ya, [v.id for v in videos], start, end)
    for i, v in enumerate(videos, 1):
        if v.id in totals:
            analytics.apply_totals(v, totals[v.id])
        detail = analytics.cached(cache / "analytics" / f"{v.id}.json", max_age, lambda: analytics.video_detail(ya, v.id, start, end))
        analytics.apply_detail(v, detail)
        print(f"\r  {i}/{len(videos)}", end="", file=sys.stderr)
    print(file=sys.stderr)
    first = min(v.published[:10] for v in videos)
    audience = analytics.cached(cache / "analytics" / f"audience-{first}.json", max_age, lambda: analytics.channel_audience(ya, first, end))

    notes: list[str] = []
    studio: dict[str, dict] = {}
    csvs = list(args.studio_csv)
    saved = auth.CONFIG_DIR / "studio.csv"  # left there by setup-channelscope.sh
    if not csvs and saved.exists():
        csvs = [saved]
        age = (time.time() - saved.stat().st_mtime) / 86400
        if age > 30:
            notes.append(f"CTR comes from a Studio export {age:.0f} days old; rerun setup-channelscope.sh to refresh it.")
    for path in csvs:
        studio.update(reach.read_studio_csv(path))
    api_reach: dict[str, dict] = {}
    if not args.no_reach:
        from googleapiclient.errors import HttpError

        try:
            job_id, created = reach.ensure_job(yr)
            files = reach.download_reports(yr, creds, job_id, cache / "reach")
            api_reach = reach.aggregate_reports(files)
            msg = reach.report_status(created, len(files))
        except HttpError as e:
            msg = f"Couldn't use the YouTube Reporting API for CTR ({e.reason if hasattr(e, 'reason') else e}). Is it enabled in your Google Cloud project?"
        if msg and not studio:
            notes.append(msg)
            reach.print_status(msg)
    reach.apply(videos, studio, api_reach)
    print(f"  CTR for {sum(v.ctr is not None for v in videos)}/{len(videos)} videos", file=sys.stderr)

    related_ids = list(dict.fromkeys(vid for v in videos for vid, _ in v.suggested_from[:6]))
    related = youtube.titles_for(yt, related_ids[:250]) if related_ids else {}

    print("Downloading thumbnails and comments...", file=sys.stderr)
    for v in videos:
        path = youtube.download_thumbnail(v, cache / "thumbnails")
        if path:
            v.thumbnail_path = str(path)
            try:
                v.thumbnail_pixels = features.thumbnail_pixels(path)
            except OSError:
                pass
        if args.comments:
            v.comments = analytics.cached(cache / "comments" / f"{v.id}.json", max_age, lambda: youtube.comments(yt, v.id, args.comments))

    segments: dict[str, list[Segment]] = {}
    if not args.no_transcripts:
        print("Getting transcripts...", file=sys.stderr)
        state: dict = {}
        for v in videos:
            segs = get_transcript(v, yt, args, state)
            if segs:
                segments[v.id] = segs
                v.has_transcript = True
                for e in retention.events(v.retention, v.duration):
                    v.excerpts[str(round(e.start))] = retention.event_context(segs, e)
            if state.get("blocked") and not args.captions_api:
                break
        print(f"  {len(segments)}/{len(videos)} transcripts", file=sys.stderr)

    return {
        "channel": channel, "videos": videos, "audience": audience, "related": related,
        "segments": segments, "notes": notes, "excluded": skipped_shorts,
    }


def analyse_with_claude(args, g: dict, scores: dict, lifts: dict) -> dict | None:
    client = make_client()
    videos, channel = g["videos"], g["channel"]
    about = f"{channel['title']}. {' '.join(channel.get('description', '').split())[:300]}"
    read_effort = "low" if args.fast else "medium"

    def tag(v: ChannelVideo):
        try:
            v.thumbnail_tags = vision.tag_thumbnail(client, args.reader_model, v, about, args.cache_dir / channel["id"], "low")
        except LLMError as e:
            print(f"  thumbnail tagging failed for {v.title}: {e}", file=sys.stderr)

    def read(v: ChannelVideo):
        segs = g["segments"].get(v.id, [])
        if not segs and not v.comments:
            return
        try:
            v.moments = moments.analyse(client, args.reader_model, v, segs, args.cache_dir / channel["id"], read_effort)
        except LLMError as e:
            print(f"  episode analysis failed for {v.title}: {e}", file=sys.stderr)
            return
        print(f"  read · {v.title}", file=sys.stderr)

    print("Claude is looking at thumbnails and reading episodes + comments...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(tag, v) for v in videos if v.thumbnail_path] + [pool.submit(read, v) for v in videos]
        for job in jobs:
            job.result()

    # Thumbnail tags feed the statistics, so recompute them now.
    lifts.clear()
    lifts.update(strategy.compute_lifts(videos, scores))
    print("Building the audience model and playbook...", file=sys.stderr)
    dossier = strategy.build_dossier(channel, g["audience"], videos, scores, lifts, g["related"], g["excluded"])
    model = args.reader_model if args.fast else args.model
    return strategy.build_strategy(client, model, dossier, args.next, "medium" if args.fast else "high")


def run(args) -> int:
    g = gather(args)
    videos, channel = g["videos"], g["channel"]
    if not videos:
        print("No videos matched (check --since / --shorts).", file=sys.stderr)
        return 1

    scores = stats.score_all(videos)
    lifts = strategy.compute_lifts(videos, scores)
    plan = None if args.no_llm else analyse_with_claude(args, g, scores, lifts)

    out = args.out or Path(f"channelscope-{_slug(channel['title'])}.md")
    thumbs = report.copy_thumbnails(videos, out.parent / f"{out.stem}-thumbnails")
    md = report.render_markdown(channel, g["audience"], videos, scores, lifts, plan, thumbs, g["notes"], args.next)
    out.write_text(md, encoding="utf-8")
    out.with_suffix(".html").write_text(report.render_html(md, channel["title"]), encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps(report.to_json(channel, g["audience"], videos, scores, lifts, plan), indent=1, default=str),
        encoding="utf-8",
    )
    report.write_csv(out.with_suffix(".csv"), videos, scores)
    # The same summary Claude saw, for asking your own follow-up questions in a chat.
    dossier = strategy.build_dossier(channel, g["audience"], videos, scores, lifts, g["related"], g["excluded"])
    out.with_name(f"{out.stem}-data.md").write_text(dossier, encoding="utf-8")
    print(f"\nWrote {out.with_suffix('.html')} (+ .md, .json, .csv, -data.md)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cookies_from_browser:
        args.cookies = f"browser:{args.cookies_from_browser.lower()}"
    if args.logout:
        token = auth.CONFIG_DIR / f"token-{_slug(args.account)}.json"
        token.unlink(missing_ok=True)
        print(f"Signed out ({token} removed).", file=sys.stderr)
        return 0
    if not args.no_llm and not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(NO_KEY_HELP, file=sys.stderr)
        return 2
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except auth.SetupError as e:
        print(e, file=sys.stderr)
        return 2
    except Exception as e:
        from googleapiclient.errors import HttpError

        if isinstance(e, HttpError) and e.resp.status == 403 and ("accessNotConfigured" in str(e) or "has not been used" in str(e)):
            api = next((name for host, name in API_NAMES.items() if host in str(e)), "the YouTube APIs")
            print(f"Enable {api} in your Google Cloud project (APIs & Services > Library), then run again.\n{e}", file=sys.stderr)
            return 2
        if isinstance(e, HttpError) and e.resp.status == 403 and "quotaExceeded" in str(e):
            print("Daily YouTube API quota used up. It resets at midnight Pacific time; cached data is kept.", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
