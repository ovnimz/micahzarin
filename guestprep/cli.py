"""guestprep: list the questions a guest has already been asked on other podcasts."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from . import cluster, discover, extract, guide, report, suggest, transcripts, web, writing
from .llm import DEFAULT_MODEL, FAST_MODEL, LLMError, make_client
from .models import Episode

BLOCKED_HELP = """\
YouTube is refusing transcript requests from this network ("sign in to confirm
you're not a bot"). Common on cloud/datacenter IPs and VPNs. Fixes, easiest first:
  - add --cookies-from-browser chrome (or safari / firefox), using a browser
    where you're logged in to YouTube
  - turn off any VPN, or run from your own computer / home network
  - pass --proxy http://user:pass@host:port (a residential proxy)
  - download transcripts yourself and pass --transcripts DIR"""

RATE_LIMIT_HELP = """\
YouTube is rate-limiting this network (HTTP 429: too many requests recently).
It clears up on its own. Options:
  - wait 30-60 minutes and run the same command again. Transcripts already
    downloaded are saved, so it picks up where it left off
  - add --cookies-from-browser chrome (or safari / firefox) to use your
    YouTube login, which gets a much higher limit
  - use -n 8 to fetch fewer interviews per run"""

RATE_LIMIT_WAITS = (60, 180)  # seconds to back off before retrying after a 429


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
    p.add_argument("--topic", default="", help='focus the prep sheet on one topic, e.g. "being a person"')
    p.add_argument("--query", action="append", default=[], help="extra YouTube search query (repeatable)")
    p.add_argument("--url", action="append", default=[], help="YouTube URL of a known appearance (repeatable)")
    p.add_argument("--urls-file", type=Path, help="file with one YouTube URL per line")
    p.add_argument("--no-search", action="store_true", help="only use --url / --urls-file / --transcripts (and --web)")
    p.add_argument("--web", action="store_true", help="also search the whole web (podcast sites, written interviews, talks) with Claude")
    p.add_argument("--no-suggest", action="store_true", help="skip writing fresh questions and no-context prompts")
    p.add_argument("--writing", action="append", default=[], metavar="URL",
                   help="the guest's Substack (whole archive) or a post/blog URL; repeatable")
    p.add_argument("--max-posts", type=int, default=25, help="posts to read per Substack (default 25)")
    p.add_argument("--no-guide", action="store_true", help="skip the researched guide (bio, ideas, quotes, reading list)")
    p.add_argument("--transcripts", type=Path, help="directory of transcripts you already have (.txt .vtt .srt .json3)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="use YouTube login cookies from chrome / safari / firefox / edge / brave")
    p.add_argument("--cookies", help="Netscape cookies.txt for YouTube (alternative to --cookies-from-browser)")
    p.add_argument("--proxy", help="HTTP(S) proxy URL for YouTube requests")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"model for grouping, fresh questions and the guide (default {DEFAULT_MODEL})")
    p.add_argument("--reader-model", default=FAST_MODEL, help=f"model that reads each transcript and searches the web (default {FAST_MODEL})")
    p.add_argument("--fast", action="store_true", help="quickest run: every step on the reader model, at lower effort")
    p.add_argument("--no-llm", action="store_true", help="keyword heuristics instead of Claude (rough; no API key needed)")
    p.add_argument("--worn-min", type=int, default=3, help="interviews a question must appear in to count as worn out (default 3)")
    p.add_argument("-o", "--out", type=Path, help="report path (default guestprep-<guest>.md); a .json is written alongside")
    p.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "guestprep")
    p.add_argument("--list-only", action="store_true", help="just print the candidate videos and exit")
    return p.parse_args(argv)


def gather_episodes(args: argparse.Namespace, on_episode=None) -> tuple[list[Episode], list[dict]]:
    """-> (interviews with transcripts, audio-only appearances found on the web).

    on_episode(ep) is called as each transcript arrives, so analysis can start
    while the rest are still downloading.
    """
    episodes: list[Episode] = []
    audio_only: list[dict] = []

    def add(ep: Episode) -> None:
        episodes.append(ep)
        if on_episode:
            on_episode(ep)

    if args.transcripts:
        for video, segs in transcripts.local_videos(args.transcripts):
            add(Episode(video=video, segments=segs))
        print(f"Loaded {len(episodes)} local transcript(s) from {args.transcripts}", file=sys.stderr)

    urls = list(args.url)
    if args.urls_file:
        urls += [l.strip() for l in args.urls_file.read_text().splitlines() if l.strip() and not l.startswith("#")]
    candidates = discover.videos_from_urls(urls, args.guest)

    web_search: Future | None = None
    if args.web:
        print(f"Searching the web for {args.guest} in the background...", file=sys.stderr)
        bg = ThreadPoolExecutor(max_workers=1)
        web_search = bg.submit(web.discover, make_client(), args.reader_model, args.guest, args.about, args.cache_dir, args.topic)
        bg.shutdown(wait=False)

    if candidates:
        discover.fill_metadata(candidates, args.proxy, args.cookies)

    if not args.no_search:
        print(f"Searching YouTube for {args.guest}...", file=sys.stderr)
        found = discover.search_youtube(
            args.guest, args.per_query, args.min_minutes, args.proxy, args.cookies,
            args.query
            + ([f'"{args.guest}" {args.about}'] if args.about else [])
            + ([f'"{args.guest}" {args.topic}'] if args.topic else []),
        )
        known = {v.id for v in candidates}
        candidates += [v for v in found if v.id not in known]
        print(f"  {len(found)} long-form candidates ({sum(v.confirmed for v in found)} name the guest)", file=sys.stderr)

    if web_search is not None:
        found = web_search.result()
        yt_urls, pages, audio_only = web.split_appearances(found)
        print(f"  {len(found)} appearances: {len(yt_urls)} YouTube, {len(pages)} pages, {len(audio_only)} audio-only", file=sys.stderr)
        known = {v.id for v in candidates}
        extra = [v for v in discover.videos_from_urls(yt_urls, args.guest) if v.id not in known]
        if extra:
            discover.fill_metadata(extra, args.proxy, args.cookies)
        candidates = extra + candidates  # links Claude vetted go first
        for a in pages:
            if args.list_only:
                print(f"🌐 {a['url']}  [{a.get('outlet', '')}] {a.get('title', '')}")
                continue
            got = web.fetch_page(a, args.cache_dir)
            if got is None:
                print(f"  no readable transcript: {a['url']}", file=sys.stderr)
                continue
            video, segs = got
            print(f"  ✓ {video.title} [{video.channel}]", file=sys.stderr)
            add(Episode(video=video, segments=segs))
        if args.list_only:
            for a in audio_only:
                print(f"🔈 {a['url']}  [{a.get('outlet', '')}] {a.get('title', '')}")

    if args.list_only:
        for v in candidates:
            mark = "✓" if v.confirmed else "?"
            print(f"{mark} {v.link()}  [{v.channel}] {v.title}")
        sys.exit(0)

    for v in candidates:
        if len(episodes) >= args.max_interviews:
            break
        was_cached = transcripts.cache_path(v, args.cache_dir).exists()
        try:
            segs = _fetch_with_backoff(v, args)
        except transcripts.TranscriptRateLimited as e:
            print(f"\n  rate-limited: {e}\n\n{RATE_LIMIT_HELP}\n", file=sys.stderr)
            break
        except transcripts.TranscriptBlocked as e:
            print(f"\n  blocked: {e}\n\n{BLOCKED_HELP}\n", file=sys.stderr)
            break
        if not was_cached:
            time.sleep(1)  # be gentle; YouTube rate-limits bursts
        if not segs:
            continue
        if not v.confirmed:
            text = " ".join(s.text for s in segs)
            if not discover.mentions_guest(text, args.guest):
                print(f"  skip (guest not mentioned): {v.title}", file=sys.stderr)
                continue
            v.confirmed = True
        print(f"  ✓ {v.title} [{v.channel}]", file=sys.stderr)
        add(Episode(video=v, segments=segs))

    return episodes, audio_only


def _fetch_with_backoff(v, args) -> list:
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            return transcripts.fetch_youtube(v, args.cache_dir, args.proxy, args.cookies)
        except transcripts.TranscriptRateLimited:
            if wait is None:
                raise
            print(f"  YouTube says too many requests; waiting {wait}s before retrying...", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def run(args: argparse.Namespace) -> int:
    suggestions = None
    guide_data = None
    if args.no_llm:
        episodes, audio_only = gather_episodes(args)
        if not episodes:
            print("No usable interviews found yet.", file=sys.stderr)
            return 1
        for ep in episodes:
            extract.extract_heuristic(ep)
        clusters = cluster.cluster_heuristic(episodes)
    else:
        client = make_client()
        model = args.reader_model if args.fast else args.model
        effort = "medium" if args.fast else "high"

        def work(ep: Episode) -> Episode | None:
            try:
                ok = extract.extract_with_claude(
                    client, args.reader_model, args.guest, ep, args.cache_dir, args.about,
                    effort="low" if args.fast else "medium",
                )
            except LLMError as e:
                print(f"  extraction failed for {ep.video.title}: {e}", file=sys.stderr)
                return None
            if not ok:
                print(f"  dropped (not an interview of {args.guest}): {ep.video.title}", file=sys.stderr)
                return None
            print(f"  {len(ep.questions):3d} questions · {ep.video.title}", file=sys.stderr)
            return ep

        with ThreadPoolExecutor(max_workers=8) as pool:
            pending: list[Future] = []
            posts_job = None
            if args.writing:
                print("Reading their writing in the background...", file=sys.stderr)
                posts_job = pool.submit(writing.collect, args.writing, args.cache_dir, args.max_posts)
            episodes, audio_only = gather_episodes(args, on_episode=lambda ep: pending.append(pool.submit(work, ep)))
            print(f"Reading {len(episodes)} interview(s)...", file=sys.stderr)
            episodes = [ep for ep in (f.result() for f in pending) if ep is not None]
            posts = posts_job.result() if posts_job else []
            if posts:
                print(f"  {len(posts)} pieces of their writing", file=sys.stderr)
            if not episodes and not posts:
                print("No usable interviews (or writing) found.", file=sys.stderr)
                return 1

            guide_job = None
            if not args.no_guide:
                print("Researching the guide in the background (background, ideas, quotes, reading list)...", file=sys.stderr)
                guide_job = pool.submit(
                    guide.build_guide, client, model, args.guest, args.about, args.topic, episodes, args.cache_dir, effort, posts
                )
            print("Grouping repeat questions across interviews...", file=sys.stderr)
            clusters = cluster.cluster_with_claude(client, model, args.guest, episodes, args.topic, effort="medium")
            if not args.no_suggest:
                print("Writing fresh questions...", file=sys.stderr)
                suggestions = suggest.suggest(
                    client, model, args.guest, args.about, episodes, clusters, args.topic, effort, posts
                )
            if guide_job is not None:
                print("Finishing the guide...", file=sys.stderr)
                guide_data = guide_job.result()
            if guide_data:
                quotes = [q for i in guide_data.get("ideas", []) for q in i.get("quotes", [])]
                bad = sum(not q.get("verified") for q in quotes)
                print(f"  {len(quotes)} quotes, {len(quotes) - bad} checked against their sources", file=sys.stderr)

    out = args.out or Path(f"guestprep-{_slug(args.guest)}.md")
    out.write_text(
        report.render_markdown(args.guest, episodes, clusters, args.worn_min, suggestions, audio_only, args.topic, guide_data),
        encoding="utf-8",
    )
    out.with_suffix(".json").write_text(
        json.dumps(report.to_json(args.guest, episodes, clusters, suggestions, audio_only, guide_data), indent=1),
        encoding="utf-8",
    )
    worn = sum(c.times_asked >= args.worn_min and c.on_topic for c in clusters)
    print(f"\nWrote {out} ({worn} worn-out questions across {len(episodes)} interviews)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cookies_from_browser:
        args.cookies = f"browser:{args.cookies_from_browser.lower()}"
    if args.web and args.no_llm:
        print("--web uses Claude to search, so it can't be combined with --no-llm.", file=sys.stderr)
        return 2
    needs_claude = args.web or not (args.no_llm or args.list_only)
    if needs_claude and not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
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
