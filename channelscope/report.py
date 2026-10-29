"""Write the report (.md), the full data (.json) and a per-video spreadsheet (.csv)."""
from __future__ import annotations

import csv
import html
import shutil
from dataclasses import asdict
from datetime import date
from pathlib import Path

from guestprep.models import format_timestamp

from . import retention
from .models import ChannelVideo
from .strategy import TRAFFIC_NAMES, video_label


def _pct(x, digits=0):
    return f"{100 * x:.{digits}f}%" if x is not None else "–"


def _x(x):
    return f"{x:.2f}x" if x is not None else "–"


def _md(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def _rules(title: str, rules: list[dict]) -> list[str]:
    if not rules:
        return []
    out = [f"**{title}**", ""]
    for r in rules:
        out.append(f"- **{_md(r['rule'])}** _({r['confidence']} confidence)_  ")
        out.append(f"  {_md(r['evidence'])}")
    return out + [""]


def _bullets(title: str, items: list[str]) -> list[str]:
    return ([f"**{title}**", ""] + [f"- {_md(i)}" for i in items] + [""]) if items else []


def copy_thumbnails(videos: list[ChannelVideo], directory: Path) -> dict[str, str]:
    """Copy thumbnails next to the report so the images show up. -> {video id: relative path}."""
    out = {}
    for v in videos:
        if v.thumbnail_path and Path(v.thumbnail_path).exists():
            directory.mkdir(parents=True, exist_ok=True)
            dest = directory / f"{v.id}{Path(v.thumbnail_path).suffix}"
            shutil.copyfile(v.thumbnail_path, dest)
            out[v.id] = f"{directory.name}/{dest.name}"
    return out


def _gallery(title: str, videos: list[tuple[int, ChannelVideo]], thumbs: dict[str, str], scores: dict) -> list[str]:
    if not videos:
        return []
    out = [f"**{title}**", "", "| | Video | CTR | Views |", "|---|---|---:|---:|"]
    for i, v in videos:
        s = scores.get(v.id, {})
        img = f'<img src="{thumbs[v.id]}" width="240">' if v.id in thumbs else ""
        out.append(
            f"| {img} | {video_label(i)} [{_md(v.title)}]({v.url}) | {_pct(v.ctr, 1)} ({_x(s.get('ctr_vs_neighbours'))}) "
            f"| {v.views:,} ({_x(s.get('views_vs_neighbours'))}) |"
        )
    return out + [""]


def render_markdown(
    channel: dict,
    audience: dict,
    videos: list[ChannelVideo],
    scores: dict[str, dict],
    lifts: dict[str, list[dict]],
    strategy: dict | None,
    thumbs: dict[str, str],
    notes: list[str],
    next_episode: str = "",
) -> str:
    numbered = list(enumerate(videos, 1))
    n_ctr = sum(v.ctr is not None for v in videos)
    out = [
        f"# Audience & packaging report: {channel['title']}",
        "",
        f"_{date.today().isoformat()} · {len(videos)} long-form videos · CTR for {n_ctr} · "
        f"{sum(bool(v.moments) for v in videos)} episodes read for drop-offs and comments_",
        "",
    ]
    for note in notes:
        out += [f"> {note}", ""]

    s = strategy or {}
    am = s.get("audience_model")
    if am:
        out += ["## Your audience", "", _md(am["summary"]), "", f"**Who they are.** {_md(am['who'])}", ""]
        out += _bullets("What they come for", am["what_they_come_for"])
        out += _bullets("Why they click", am["why_they_click"])
        out += _bullets("Why they stay", am["why_they_stay"])
        out += _bullets("Why they leave", am["why_they_leave"])
        out += _bullets("Words they use (searches and comments)", am["words_they_use"])
        out += _bullets("What else they watch", am["what_else_they_watch"])

    ne = s.get("next_episode")
    if next_episode and ne and (ne["titles"] or ne["thumbnails"]):
        out += [f"## Next episode: {_md(next_episode)}", ""]
        out += ["**Titles**", ""] + [f"1. **{_md(t['title'])}**: {_md(t['why'])}" for t in ne["titles"]] + [""]
        out += ["**Thumbnails**", ""] + [
            f"1. {_md(t['concept'])}" + (f' Text: "{_md(t["text_overlay"])}".' if t["text_overlay"] else "") + f" _{_md(t['why'])}_"
            for t in ne["thumbnails"]
        ] + [""]
        if ne["opening"]:
            out += [f"**First 60 seconds.** {_md(ne['opening'])}", ""]
        out += _bullets("Angles to lean into", ne["angles"])

    with_ctr = [(i, v) for i, v in numbered if scores.get(v.id, {}).get("ctr_vs_neighbours") is not None]
    by_ctr = sorted(with_ctr, key=lambda iv: -scores[iv[1].id]["ctr_vs_neighbours"])
    if s.get("titles") or s.get("thumbnails") or by_ctr:
        out += ["## Titles and thumbnails", ""]
        t = s.get("titles", {})
        out += _rules("Titles that work here", t.get("do", []))
        out += _rules("Titles to avoid", t.get("avoid", []))
        out += _bullets("Title templates", t.get("templates", []))
        th = s.get("thumbnails", {})
        if th.get("recipe"):
            out += [f"**Thumbnail recipe.** {_md(th['recipe'])}", ""]
        out += _rules("Thumbnails that work here", th.get("do", []))
        out += _rules("Thumbnails to avoid", th.get("avoid", []))
        k = min(5, len(by_ctr) // 2)
        out += _gallery("Best CTR vs. neighbouring uploads", by_ctr[:k], thumbs, scores)
        out += _gallery("Worst CTR vs. neighbouring uploads", by_ctr[::-1][:k], thumbs, scores)

    iv = s.get("interviewing")
    if iv:
        out += ["## Interviewing", ""]
        if iv["openings"]:
            out += [f"**Openings.** {_md(iv['openings'])}", ""]
        out += _rules("Do more", iv["do_more"])
        out += _rules("Do less", iv["do_less"])
        out += _bullets("Topics that hold viewers", iv["topics_that_hold"])
        out += _bullets("Topics that lose viewers", iv["topics_that_lose"])

    out += _dropoff_tables(numbered)

    if s.get("content_opportunities"):
        out += ["## Content opportunities", ""] + [f"- **{_md(o['idea'])}**: {_md(o['why'])}" for o in s["content_opportunities"]] + [""]
    if s.get("experiments"):
        out += ["## Experiments to run", "", "| Hypothesis | How | Watch |", "|---|---|---|"]
        out += [f"| {_md(e['hypothesis'])} | {_md(e['how'])} | {_md(e['metric'])} |" for e in s["experiments"]] + [""]
    out += _bullets("Caveats", s.get("caveats", []))

    out += ["---", "", "# The data", ""]
    out += _video_table(numbered, scores)
    out += _audience(audience)
    out += _lift_tables(lifts)
    return "\n".join(out) + "\n"


def _dropoff_tables(numbered: list[tuple[int, ChannelVideo]]) -> list[str]:
    drops, spikes = [], []
    for i, v in numbered:
        analysed = {round(e["start"]): e for e in v.moments.get("events", []) if "start" in e}
        for e in retention.events(v.retention, v.duration):
            info = analysed.get(round(e.start), {})
            (drops if e.kind == "drop" else spikes).append((i, v, e, info))
    out = []
    for title, rows, verb in (("Biggest drop-offs", drops, "Lost"), ("Most rewatched", spikes, "Rewatched")):
        rows = sorted(rows, key=lambda r: -r[2].size)[:20]
        if not rows:
            continue
        out += [f"## {title}", "", f"| Video | When | {verb} | What was happening |", "|---|---|---:|---|"]
        for i, v, e, info in rows:
            if info:
                what = f"**{_md(info['category'])}**: {_md(info['what_was_happening'])}"
            elif v.excerpts.get(str(round(e.start))):
                what = f"_\"…{_md(v.excerpts[str(round(e.start))][-300:])}\"_"
            else:
                what = "_no transcript_"
            out.append(f"| {video_label(i)} {_md(v.title[:60])} | [{e.label}]({v.link(e.start)}) | {100 * e.size:.1f}% | {what} |")
        out.append("")
    return out


def _video_table(numbered, scores) -> list[str]:
    out = [
        "## Every video",
        "",
        "_\"x\" compares with the 5 uploads either side. Held = share still watching at 30s._",
        "",
        "| # | Published | Title | Length | Views | Impr. | CTR | Avg viewed | Held 30s | Subs/1k | Top source |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, v in numbered:
        s = scores.get(v.id, {})
        r = retention.summary(v.retention, v.duration) if v.retention else {}
        top = next(iter(v.traffic_share().items()), None)
        impressions = f"{v.impressions:,}" if v.impressions is not None else "–"
        avg = f"{v.avg_view_pct:.0f}%" if v.avg_view_pct is not None else "–"
        subs = f"{s['subs_per_1k_views']:.1f}" if s.get("subs_per_1k_views") is not None else "–"
        source = f"{TRAFFIC_NAMES.get(top[0], top[0].lower())} {_pct(top[1])}" if top else "–"
        out.append(
            f"| {video_label(i)} | {v.published[:10]} | [{_md(v.title)}]({v.url}) | {format_timestamp(v.duration)} "
            f"| {v.views:,} ({_x(s.get('views_vs_neighbours'))}) | {impressions} "
            f"| {_pct(v.ctr, 1)} ({_x(s.get('ctr_vs_neighbours'))}) | {avg} | {_pct(r.get('at_30s'))} | {subs} | {source} |"
        )
    return out + [""]


def _audience(a: dict) -> list[str]:
    if not any(a.values()):
        return []
    out = ["## Audience", ""]
    if a.get("age_gender"):
        out += ["| Age | Gender | % of viewers |", "|---|---|---:|"]
        for r in sorted(a["age_gender"], key=lambda r: -r["viewerPercentage"]):
            if r["viewerPercentage"] >= 0.5:
                out.append(f"| {r['ageGroup'].replace('age', '')} | {r['gender'].lower()} | {r['viewerPercentage']:.1f}% |")
        out.append("")
    if a.get("countries"):
        out.append("**Top countries:** " + ", ".join(f"{r['country']} ({r['views']:,})" for r in a["countries"][:10]))
        out.append("")
    if a.get("search_terms"):
        out.append("**Top searches:** " + ", ".join(f"{_md(t)} ({n})" for t, n in a["search_terms"][:25]))
        out.append("")
    return out


def _lift_tables(lifts: dict[str, list[dict]]) -> list[str]:
    out = []
    names = {
        "ctr_vs_neighbours": "CTR vs. neighbours", "ctr": "CTR", "views_vs_neighbours": "Views vs. neighbours",
        "avg_view_pct": "Average % viewed", "held_at_30s": "Held at 30s", "subs_per_1k_views": "Net subs per 1k views",
    }
    for target, rows in lifts.items():
        rows = rows[:12]
        if not rows:
            continue
        if not out:
            out = ["## What goes with better numbers", "",
                   "_Correlation on your own videos. Small samples: treat as leads to test, not rules._", ""]
        out += [f"**{names.get(target, target)}**", "", "| Trait | Result |", "|---|---|"]
        for r in rows:
            if r["kind"] == "bool":
                res = f"{r['with']:.3g} with (n={r['n_with']}) vs {r['without']:.3g} without (n={r['n_without']})"
            elif r["kind"] == "num":
                res = f"rank correlation {r['rho']:+.2f} (n={r['n']})"
            else:
                res = ", ".join(f"{_md(c['value'])} {c['mean']:.3g} (n={c['n']})" for c in r["categories"])
            out.append(f"| {r['feature'].replace('_', ' ')} | {res} |")
        out.append("")
    return out


def write_csv(path: Path, videos: list[ChannelVideo], scores: dict[str, dict]) -> None:
    cols = [
        "label", "id", "published", "title", "duration_s", "views", "views_vs_neighbours", "impressions", "ctr",
        "ctr_vs_neighbours", "ctr_source", "avg_view_pct", "avg_view_duration_s", "held_at_30s", "held_at_50pct",
        "watch_minutes", "likes", "comments", "shares", "subs_gained", "subs_lost", "subs_per_1k_views",
        "top_traffic_source", "thumbnail_text", "thumbnail_emotion", "title_style", "url",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, v in enumerate(videos, 1):
            s = scores.get(v.id, {})
            r = retention.summary(v.retention, v.duration) if v.retention else {}
            top = next(iter(v.traffic_share()), "")
            w.writerow([
                video_label(i), v.id, v.published, v.title, v.duration, v.views, s.get("views_vs_neighbours"),
                v.impressions, v.ctr, s.get("ctr_vs_neighbours"), v.ctr_source, v.avg_view_pct, v.avg_view_duration,
                r.get("at_30s"), r.get("at_50pct"), v.watch_minutes, v.likes, v.comment_count, v.shares,
                v.subs_gained, v.subs_lost, s.get("subs_per_1k_views"), top,
                v.thumbnail_tags.get("text_overlay", ""), v.thumbnail_tags.get("dominant_emotion", ""),
                v.thumbnail_tags.get("title_style", ""), v.url,
            ])


def to_json(channel, audience, videos, scores, lifts, strategy) -> dict:
    return {
        "channel": channel,
        "audience": audience,
        "strategy": strategy,
        "lifts": lifts,
        "videos": [
            {"label": video_label(i), **asdict(v), "scores": scores.get(v.id, {})}
            for i, v in enumerate(videos, 1)
        ],
    }


HTML_STYLE = """
:root { --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e5e5ea; --accent: #0a66c2; --soft: #f5f5f7; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161618; --fg: #f2f2f7; --muted: #a1a1a6; --line: #333336; --accent: #6cb4ff; --soft: #222225; }
}
body { background: var(--bg); color: var(--fg); font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1100px; margin: 0 auto; padding: 24px 16px 80px; }
h1 { font-size: 1.9em; line-height: 1.2; } h2 { margin-top: 2.2em; padding-top: .6em; border-top: 1px solid var(--line); }
a { color: var(--accent); } em { color: var(--muted); }
blockquote { margin: 1em 0; padding: .6em 1em; background: var(--soft); border-left: 3px solid var(--accent); border-radius: 4px; }
.table { overflow-x: auto; } table { border-collapse: collapse; font-size: .9em; margin: 1em 0; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: var(--soft); position: sticky; top: 0; } img { max-width: 240px; height: auto; border-radius: 6px; }
"""


def render_html(markdown_text: str, title: str) -> str:
    """The same report as a page that opens in any browser."""
    import markdown

    body = markdown.markdown(markdown_text, extensions=["tables", "sane_lists"])
    body = body.replace("<table>", '<div class="table"><table>').replace("</table>", "</table></div>")
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}: channelscope</title><style>{HTML_STYLE}</style></head><body>{body}</body></html>\n"
    )
