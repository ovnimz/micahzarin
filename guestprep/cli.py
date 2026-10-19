"""guestprep: list the questions a guest has already been asked on other podcasts."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cluster, discover, extract, report, transcripts
from .llm import DEFAULT_MODEL, LLMError, make_client
from .models import Episode

BLOCKED_HELP = """\
YouTube is refusing transcript requests from this network ("sign in to confirm
you're not a bot" / IP block). This almost always means you're on a cloud or
datacenter IP. Fixes, easiest first:
  - run guestprep from your own computer / home network
  - pass --cookies cookies.txt (export from a logged-in browser; see
    https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)
  - pass --proxy http://user:pass@host:port (a residential proxy)
  - download transcripts yourself and pass --transcripts DIR"""


NO_KEY_HELP = """\
ANTHROPIC_API_KEY isn't set in this Terminal window.
  - If you added it to ~/.zshrc, open a new Terminal window (or run: source ~/.zshrc),
    then cd back here and run: source .venv/bin/activate
  - Or set it just for this window: export ANTHROPIC_API_KEY=sk-ant-...
  - Or run without Claude (rough results): add --no-llm"""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "guest"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="guestprep", description=__doc__)
    p.add_argument("guest", help='Guest\'s full name, e.g. "Morgan Housel"')
    p.add_argument("-n", "--max-interviews", type=int, default=15, help="stop after this many usable interviews (default 15)")
    p.add_argument("--per-query", type=int, default=15, help="YouTube results per search query (default 15)")
    p.add_argument("--min-minutes", type=float, default=20, help="ignore videos shorter than this; filters out clips (default 20)")
    p.add_argument("--about", default="", help='who the guest is, to rule out namesakes, e.g. "Australian VET and tertiary education policy expert"')
    p.add_argument("--query", action="append", default=[], help="extra YouTube search query (repeatable)")
    p.add_argument("--url", action="append", default=[], help="YouTube URL of a known appearance (repeatable)")
    p.add_argument("--urls-file", type=Path, help="file with one YouTube URL per line")
    p.add_argument("--no-search", action="store_true", help="only use --url / --urls-file / --transcripts")
    p.add_argument("--transcripts", type=Path, help="directory of transcripts you already have (.txt .vtt .srt .json3)")
    p.add_argument("--cookies", help="Netscape cookies.txt for YouTube (helps when YouTube blocks requests)")
    p.add_argument("--proxy", help="HTTP(S) proxy URL for YouTube requests")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default {DEFAULT_MODEL})")
    p.add_argument("--no-llm", action="store_true", help="keyword heuristics instead of Claude (rough; no API key needed)")
    p.add_argument("--worn-min", type=int, default=3, help="interviews a question must appear in to count as worn out (default 3)")
    p.add_argument("-o", "--out", type=Path, help="report path (default guestprep-<guest>.md); a .json is written alongside")
    p.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "guestprep")
    p.add_argument("--list-only", action="store_true", help="just print the candidate videos and exit")
    return p.parse_args(argv)


def gather_episodes(args: argparse.Namespace) -> list[Episode]:
    episodes: list[Episode] = []

    if args.transcripts:
        for video, segs in transcripts.local_videos(args.transcripts):
            episodes.append(Episode(video=video, segments=segs))
        print(f"Loaded {len(episodes)} local transcript(s) from {args.transcripts}", file=sys.stderr)

    urls = list(args.url)
    if args.urls_file:
        urls += [l.strip() for l in args.urls_file.read_text().splitlines() if l.strip() and not l.startswith("#")]
    candidates = discover.videos_from_urls(urls, args.guest)
    if candidates:
        discover.fill_metadata(candidates, args.proxy, args.cookies)

    if not args.no_search:
        print(f"Searching YouTube for {args.guest}...", file=sys.stderr)
        found = discover.search_youtube(
            args.guest, args.per_query, args.min_minutes, args.proxy, args.cookies,
            args.query + ([f'"{args.guest}" {args.about}'] if args.about else []),
        )
        known = {v.id for v in candidates}
        candidates += [v for v in found if v.id not in known]
        print(f"  {len(found)} long-form candidates ({sum(v.confirmed for v in found)} name the guest)", file=sys.stderr)

    if args.list_only:
        for v in candidates:
            mark = "✓" if v.confirmed else "?"
            print(f"{mark} {v.link()}  [{v.channel}] {v.title}")
        sys.exit(0)

    for v in candidates:
        if len(episodes) >= args.max_interviews:
            break
        try:
            segs = transcripts.fetch_youtube(v, args.cache_dir, args.proxy, args.cookies)
        except transcripts.TranscriptBlocked as e:
            print(f"\n  blocked: {e}\n\n{BLOCKED_HELP}\n", file=sys.stderr)
            break
        if not segs:
            continue
        if not v.confirmed:
            text = " ".join(s.text for s in segs)
            if not discover.mentions_guest(text, args.guest):
                print(f"  skip (guest not mentioned): {v.title}", file=sys.stderr)
                continue
            v.confirmed = True
        print(f"  ✓ {v.title} [{v.channel}]", file=sys.stderr)
        episodes.append(Episode(video=v, segments=segs))
        time.sleep(0.5)  # be gentle; YouTube rate-limits bursts

    return episodes


def run(args: argparse.Namespace) -> int:
    episodes = gather_episodes(args)
    if not episodes:
        print("No usable interviews found.", file=sys.stderr)
        return 1

    print(f"Extracting questions from {len(episodes)} interview(s)...", file=sys.stderr)
    if args.no_llm:
        for ep in episodes:
            extract.extract_heuristic(ep)
        clusters = cluster.cluster_heuristic(episodes)
    else:
        client = make_client()

        def work(ep: Episode) -> Episode | None:
            try:
                ok = extract.extract_with_claude(client, args.model, args.guest, ep, args.cache_dir, args.about)
            except LLMError as e:
                print(f"  extraction failed for {ep.video.title}: {e}", file=sys.stderr)
                return None
            if not ok:
                print(f"  dropped (not an interview of {args.guest}): {ep.video.title}", file=sys.stderr)
                return None
            print(f"  {len(ep.questions):3d} questions · {ep.video.title}", file=sys.stderr)
            return ep

        with ThreadPoolExecutor(max_workers=4) as pool:
            episodes = [ep for ep in pool.map(work, episodes) if ep is not None]
        if not episodes:
            print("No interviews survived extraction.", file=sys.stderr)
            return 1
        print("Grouping repeat questions across interviews...", file=sys.stderr)
        clusters = cluster.cluster_with_claude(client, args.model, args.guest, episodes)

    out = args.out or Path(f"guestprep-{_slug(args.guest)}.md")
    out.write_text(report.render_markdown(args.guest, episodes, clusters, args.worn_min), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(report.to_json(args.guest, episodes, clusters), indent=1), encoding="utf-8")
    worn = sum(c.times_asked >= args.worn_min for c in clusters)
    print(f"\nWrote {out} ({worn} worn-out questions across {len(episodes)} interviews)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.no_llm and not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(NO_KEY_HELP, file=sys.stderr)
        return 2
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        import anthropic

        if isinstance(e, anthropic.AuthenticationError) or "Could not resolve authentication" in str(e):
            print("Claude API authentication failed: set ANTHROPIC_API_KEY (or use --no-llm).", file=sys.stderr)
            return 2
        if isinstance(e, anthropic.APIConnectionError):
            print(f"Couldn't reach the Claude API: {e}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
