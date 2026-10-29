"""Put it all together: a model of the audience and a packaging/interview playbook."""
from __future__ import annotations

from collections import defaultdict

from guestprep.llm import call_json
from guestprep.models import format_timestamp

from . import retention, stats
from .models import ChannelVideo

SYSTEM = """\
You are a YouTube strategist working for the creator of the channel below. \
You have their private analytics for recent long-form videos: impressions \
and click-through rate, watch time and retention, where viewers come from, \
what they searched, which videos they were watching before, what is in each \
thumbnail and title, where in each episode viewers left or rewatched and \
why, and what commenters said. You also have simple statistics comparing \
title and thumbnail traits with performance on this channel.

The creator wants to know:
1. Who their audience is, what they come for, why they click, why they \
stay and why they leave: a model they can use to make decisions.
2. The best strategy for titles and thumbnails on this channel.
3. What the retention and comments say about their interviewing: what to \
do more of and less of.
4. What to try next.

Rules:
- Ground every claim in the data. Cite videos by their V-number (e.g. V3, \
V17) and quote the numbers (CTR, "x neighbours", % held at 30s, drop sizes). \
Prefer patterns that show up across several videos over one-offs.
- CTR depends on traffic source: search and subscriber traffic click more \
than browse or suggested traffic. Take a video's traffic mix into account \
before crediting or blaming its packaging. "x neighbours" scores compare a \
video with the ones published around it, which controls for channel growth.
- Be honest about sample size. With few videos, say which conclusions are \
strong and which are hunches worth testing. Confidence is "high" only when \
several videos agree and the effect is large.
- Be specific and usable: name the exact title patterns, words, faces, \
expressions, text overlays and interview moves, not generic YouTube advice. \
Anything that is true of every channel ("use high contrast", "hook viewers \
early") is only worth saying if this channel's data shows it.
- Experiments should be things the creator can actually run, such as \
YouTube's built-in Test & Compare for thumbnails and titles, with the metric \
to watch."""

_rule = {
    "type": "object",
    "properties": {
        "rule": {"type": "string"},
        "evidence": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["rule", "evidence", "confidence"],
    "additionalProperties": False,
}
_list = {"type": "array", "items": {"type": "string"}}
_rules = {"type": "array", "items": _rule}

SCHEMA = {
    "type": "object",
    "properties": {
        "audience_model": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "who": {"type": "string"},
                "what_they_come_for": _list,
                "why_they_click": _list,
                "why_they_stay": _list,
                "why_they_leave": _list,
                "words_they_use": _list,
                "what_else_they_watch": _list,
            },
            "required": ["summary", "who", "what_they_come_for", "why_they_click", "why_they_stay",
                         "why_they_leave", "words_they_use", "what_else_they_watch"],
            "additionalProperties": False,
        },
        "titles": {
            "type": "object",
            "properties": {"do": _rules, "avoid": _rules, "templates": _list},
            "required": ["do", "avoid", "templates"],
            "additionalProperties": False,
        },
        "thumbnails": {
            "type": "object",
            "properties": {"do": _rules, "avoid": _rules, "recipe": {"type": "string"}},
            "required": ["do", "avoid", "recipe"],
            "additionalProperties": False,
        },
        "interviewing": {
            "type": "object",
            "properties": {
                "do_more": _rules,
                "do_less": _rules,
                "openings": {"type": "string"},
                "topics_that_hold": _list,
                "topics_that_lose": _list,
            },
            "required": ["do_more", "do_less", "openings", "topics_that_hold", "topics_that_lose"],
            "additionalProperties": False,
        },
        "content_opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"idea": {"type": "string"}, "why": {"type": "string"}},
                "required": ["idea", "why"],
                "additionalProperties": False,
            },
        },
        "experiments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "how": {"type": "string"},
                    "metric": {"type": "string"},
                },
                "required": ["hypothesis", "how", "metric"],
                "additionalProperties": False,
            },
        },
        "next_episode": {
            "type": "object",
            "properties": {
                "titles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}, "why": {"type": "string"}},
                        "required": ["title", "why"],
                        "additionalProperties": False,
                    },
                },
                "thumbnails": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "concept": {"type": "string"},
                            "text_overlay": {"type": "string"},
                            "why": {"type": "string"},
                        },
                        "required": ["concept", "text_overlay", "why"],
                        "additionalProperties": False,
                    },
                },
                "opening": {"type": "string"},
                "angles": _list,
            },
            "required": ["titles", "thumbnails", "opening", "angles"],
            "additionalProperties": False,
        },
        "caveats": _list,
    },
    "required": ["audience_model", "titles", "thumbnails", "interviewing", "content_opportunities",
                 "experiments", "next_episode", "caveats"],
    "additionalProperties": False,
}

TRAFFIC_NAMES = {
    "RELATED_VIDEO": "suggested", "SUBSCRIBER": "browse/subscriptions", "YT_SEARCH": "search",
    "EXT_URL": "external", "NO_LINK_OTHER": "direct/unknown", "PLAYLIST": "playlists",
    "NOTIFICATION": "notifications", "YT_CHANNEL": "channel page", "YT_OTHER_PAGE": "other YouTube",
    "END_SCREEN": "end screens", "SHORTS": "shorts feed", "HASHTAGS": "hashtags", "ANNOTATION": "cards",
    "CAMPAIGN_CARD": "campaign cards", "VIDEO_REMIXES": "remixes", "SOUND_PAGE": "sound page",
    "ADVERTISING": "ads", "YT_PLAYLIST_PAGE": "playlist page", "PRODUCT_PAGE": "product page",
}


def _pct(x, digits=0):
    return f"{100 * x:.{digits}f}%" if x is not None else "?"


def _x(x):
    return f"{x:.2f}x" if x is not None else "?"


def video_label(i: int) -> str:
    return f"V{i}"


def build_dossier(
    channel: dict,
    audience: dict,
    videos: list[ChannelVideo],
    scores: dict[str, dict],
    lifts: dict[str, list[dict]],
    related_titles: dict[str, dict],
    excluded: int = 0,
) -> str:
    out = [
        f"# Channel: {channel['title']}",
        f"{channel['subscribers']:,} subscribers · {channel['views']:,} lifetime views · {channel['video_count']} videos",
        f"About: {' '.join(channel.get('description', '').split())[:800]}",
        "",
        "# Audience (whole channel, analysis period)",
    ]
    if audience.get("age_gender"):
        out.append("Age/gender (% of viewers): " + ", ".join(
            f"{r['ageGroup'].replace('age', '')} {r['gender'].lower()} {r['viewerPercentage']:.1f}"
            for r in sorted(audience["age_gender"], key=lambda r: -r["viewerPercentage"]) if r["viewerPercentage"] >= 1
        ))
    if audience.get("countries"):
        total = sum(r["views"] for r in audience["countries"]) or 1
        out.append("Top countries (share of top-15 views): " + ", ".join(
            f"{r['country']} {_pct(r['views'] / total)}" for r in audience["countries"][:10]))
    if audience.get("devices"):
        total = sum(r["estimatedMinutesWatched"] for r in audience["devices"]) or 1
        out.append("Watch time by device: " + ", ".join(
            f"{r['deviceType'].lower()} {_pct(r['estimatedMinutesWatched'] / total)}" for r in audience["devices"]))
    for r in audience.get("subscribed", []):
        out.append(f"{r['subscribedStatus'].lower()}: {r['views']:,} views, avg view {format_timestamp(r['averageViewDuration'])}")
    if audience.get("traffic"):
        total = sum(r["views"] for r in audience["traffic"]) or 1
        out.append("Channel traffic sources: " + ", ".join(
            f"{TRAFFIC_NAMES.get(r['insightTrafficSourceType'], r['insightTrafficSourceType'].lower())} {_pct(r['views'] / total)}"
            for r in sorted(audience["traffic"], key=lambda r: -r["views"])[:8]))
    if audience.get("search_terms"):
        out.append("Top YouTube searches that found the channel: " + "; ".join(f"{t} ({n})" for t, n in audience["search_terms"]))

    out += ["", f"# Videos ({len(videos)} long-form, newest first" + (f"; {excluded} Shorts left out" if excluded else "") + ")"]
    drop_cats: dict[str, list[float]] = defaultdict(list)
    for i, v in enumerate(videos, 1):
        s = scores.get(v.id, {})
        r = retention.summary(v.retention, v.duration) if v.retention else {}
        out += ["", f"## {video_label(i)} \"{v.title}\"", f"{v.published[:10]} · {format_timestamp(v.duration)}" + (" · live stream" if v.live else "")]
        out.append(
            f"views {v.views:,} ({_x(s.get('views_vs_neighbours'))} neighbours) · "
            + (f"impressions {v.impressions:,} · CTR {_pct(v.ctr, 1)} ({_x(s.get('ctr_vs_neighbours'))} neighbours) · " if v.ctr is not None else "CTR unknown · ")
            + f"avg viewed {v.avg_view_pct or 0:.0f}% ({format_timestamp(v.avg_view_duration)}) · "
            + f"held at 30s {_pct(r.get('at_30s'))}, 50% mark {_pct(r.get('at_50pct'))} · "
            + (f"YouTube relative retention {r['relative_avg']:.2f} · " if r.get("relative_avg") is not None else "")
            + f"net subs/1k views {s.get('subs_per_1k_views') or 0:.1f} · likes {v.likes:,} · comments {v.comment_count:,}"
        )
        if v.traffic:
            out.append("traffic: " + ", ".join(f"{TRAFFIC_NAMES.get(k, k.lower())} {_pct(x)}" for k, x in list(v.traffic_share().items())[:5]))
        if v.search_terms:
            out.append("searched: " + "; ".join(f"{t} ({n})" for t, n in v.search_terms[:10]))
        rel = [related_titles[vid] for vid, _ in v.suggested_from[:6] if vid in related_titles]
        if rel:
            out.append("suggested next to: " + "; ".join(f"\"{r['title']}\" [{r['channel']}]" for r in rel))
        t = v.thumbnail_tags
        if t:
            out.append(
                f"thumbnail: {t.get('summary', '')} Text: \"{t.get('text_overlay', '')}\" ({t.get('text_vs_title')}). "
                f"Emotion: {t.get('dominant_emotion')}. Faces {t.get('faces')}, eye contact {t.get('eye_contact')}. "
                f"Colours: {t.get('colours')}. Readability {t.get('readability')}/5, clutter {t.get('clutter')}/5. "
                f"Curiosity: {t.get('curiosity')}"
            )
            out.append(f"title: style {t.get('title_style')}; promise: {t.get('title_promise')}")
        if v.thumbnail_pixels:
            out.append("thumbnail pixels: " + ", ".join(f"{k} {val}" for k, val in v.thumbnail_pixels.items()))
        m = v.moments
        if m:
            out.append(f"opening: {m['opening']['what_happens']} -> {m['opening']['assessment']}")
            for e in m.get("events", []):
                if "start" not in e:
                    continue
                verb = "lost" if e["kind"] == "drop" else "rewatched"
                out.append(f"{e['kind']} {format_timestamp(e['start'])}: {verb} {100 * e['size']:.1f}% · {e['category']} · {e['what_was_happening']} ({e['likely_reason']})")
                if e["kind"] == "drop":
                    drop_cats[e["category"]].append(e["size"])
            for c in m.get("cited_moments", []):
                out.append(f"commenters cite {c['timestamp']}: {c['what']}")
            cm = m.get("comments", {})
            for k in ("liked", "disliked", "requests", "wished_asked", "about_host"):
                if cm.get(k):
                    out.append(f"comments {k.replace('_', ' ')}: " + "; ".join(cm[k][:6]))
            for it in m.get("interview_technique", []):
                out.append(f"technique [{it['timestamp']}] ({it['effect']}): {it['observation']}")

    if drop_cats:
        out += ["", "# Drop-offs by cause (all videos)"]
        for cat, sizes in sorted(drop_cats.items(), key=lambda kv: -sum(kv[1])):
            out.append(f"- {cat}: {len(sizes)} drops, avg {100 * sum(sizes) / len(sizes):.1f}% of the audience each")

    out += ["", "# Trait vs. performance on this channel", "(bool: average with vs without; num: Spearman rho; cat: average per category)"]
    for target, rows in lifts.items():
        if not rows:
            continue
        out.append(f"## {target}")
        for r in rows[:15]:
            if r["kind"] == "bool":
                out.append(f"- {r['feature']}: {r['with']:.3g} with (n={r['n_with']}) vs {r['without']:.3g} without (n={r['n_without']})")
            elif r["kind"] == "num":
                out.append(f"- {r['feature']}: rho {r['rho']:+.2f} (n={r['n']})")
            else:
                out.append(f"- {r['feature']}: " + ", ".join(f"{c['value']} {c['mean']:.3g} (n={c['n']})" for c in r["categories"]))
    return "\n".join(out)


def build_strategy(client, model: str, dossier: str, next_episode: str = "", effort: str = "high") -> dict | None:
    ask = (
        f"\n\n# Next episode\nThe creator's next episode: {next_episode}\n"
        "Write next_episode for it: 5 titles and 3 thumbnail concepts that fit this audience, "
        "an opening for the first 60 seconds, and angles to lean into."
        if next_episode
        else "\n\nNo next episode was given: leave next_episode's lists empty and its opening \"\"."
    )
    return call_json(client, model=model, system=SYSTEM, user=dossier + ask, schema=SCHEMA, effort=effort, max_tokens=64000)


def compute_lifts(videos: list[ChannelVideo], scores: dict[str, dict]) -> dict[str, list[dict]]:
    rows = stats.feature_rows(videos)
    have_ctr = any(s.get("ctr") is not None for s in scores.values())
    targets = (["ctr_vs_neighbours", "ctr"] if have_ctr else []) + [
        "views_vs_neighbours", "avg_view_pct", "held_at_30s", "subs_per_1k_views",
    ]
    return {t: stats.lift(rows, scores, t) for t in targets}
