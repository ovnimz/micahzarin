"""Render the prep sheet."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from .models import Cluster, Episode, format_timestamp
from . import guide as guide_mod
from .suggest import Suggestions


def _ep_ref(ep_num: dict[str, int], episodes: dict[str, Episode], cluster: Cluster) -> str:
    refs = []
    for eid in cluster.episode_ids:
        ep = episodes[eid]
        q = next(m for m in cluster.members if m.episode_id == eid)
        ts = format_timestamp(q.seconds)
        label = f"#{ep_num[eid]}" + (f" {ts}" if ts else "")
        refs.append(f"[{label}]({ep.video.link(q.seconds)})" if not ep.video.local_path else label)
    return ", ".join(refs)


def _describe(c: Cluster) -> str:
    if c.label.strip().lower() == c.canonical_question.strip().lower():
        return _md_escape(c.canonical_question)
    return f"**{_md_escape(c.label)}**: {_md_escape(c.canonical_question)}"


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_markdown(
    guest: str,
    episodes: list[Episode],
    clusters: list[Cluster],
    worn_min: int = 3,
    suggestions: Suggestions | None = None,
    audio_only: list[dict] | None = None,
    topic: str = "",
    guide: dict | None = None,
) -> str:
    ep_num = {ep.video.id: i for i, ep in enumerate(episodes, 1)}
    by_id = {ep.video.id: ep for ep in episodes}
    n = len(episodes)
    off_topic = [c for c in clusters if not c.on_topic] if topic else []
    shown = [c for c in clusters if c.on_topic] if topic else clusters
    worn = [c for c in shown if c.times_asked >= worn_min]
    repeat = [c for c in shown if 2 <= c.times_asked < worn_min]
    once = [c for c in shown if c.times_asked < 2]

    out = [f"# Guest prep: {guest}" + (f" on {topic}" if topic else ""), ""]
    out.append(
        f"_{date.today().isoformat()} · {n} prior interview{'s' if n != 1 else ''} scanned · "
        f"{sum(len(e.questions) for e in episodes)} questions found · "
        f"{len(clusters)} distinct questions"
        + (f" ({len(shown)} on {topic})" if topic else "")
        + "_"
    )
    out.append("")

    if guide:
        out += guide_mod.render(guide)
        out += ["---", "", "# Questions", ""]

    out.append(f"## Worn out: asked in {worn_min}+ interviews")
    out.append("")
    if worn:
        out.append("Skip these, or ask them from an angle the guest hasn't heard.")
        out.append("")
        out.append("| Asked | Question | Where |")
        out.append("|---:|---|---|")
        for c in worn:
            out.append(f"| {c.times_asked}/{n} | {_describe(c)} | {_ep_ref(ep_num, by_id, c)} |")
    else:
        out.append("_Nothing reached this threshold._")
    out.append("")

    if suggestions and suggestions.fresh_questions:
        out.append("## Fresh questions")
        out.append("")
        out.append("Not asked in any interview scanned.")
        out.append("")
        for i, s in enumerate(suggestions.fresh_questions, 1):
            out.append(f"{i}. **{s['question']}**  ")
            why = s["why"] + (f" _Builds on: {s['builds_on']}._" if s.get("builds_on") else "")
            out.append(f"   {why}")
        out.append("")

    if suggestions and suggestions.no_context_prompts:
        out.append("## No-context prompts")
        out.append("")
        out.append("Stand-alone prompts the audience can follow without knowing the guest's work.")
        out.append("")
        for i, s in enumerate(suggestions.no_context_prompts, 1):
            out.append(f"{i}. **{s['prompt']}** _({s['technique']})_  ")
            out.append(f"   {s['why']}")
        out.append("")

    if repeat:
        out.append("## Asked more than once")
        out.append("")
        out.append("| Asked | Question | Where |")
        out.append("|---:|---|---|")
        for c in repeat:
            out.append(f"| {c.times_asked}/{n} | {_describe(c)} | {_ep_ref(ep_num, by_id, c)} |")
        out.append("")

    threads = [(ep, t) for ep in episodes for t in ep.unexplored_threads]
    if threads:
        out.append("## Threads nobody pulled on")
        out.append("")
        out.append("Things the guest brought up that the host let pass. These are good places to find fresh material.")
        out.append("")
        for ep, t in threads:
            out.append(f"- {t} _(#{ep_num[ep.video.id]})_")
        out.append("")

    if once:
        out.append("<details><summary>Asked only once ({})</summary>".format(len(once)))
        out.append("")
        for c in once:
            out.append(f"- {c.canonical_question} ({_ep_ref(ep_num, by_id, c)})")
        out.append("")
        out.append("</details>")
        out.append("")

    if off_topic:
        out.append(f"<details><summary>Asked, but not about {_md_escape(topic)} ({len(off_topic)})</summary>")
        out.append("")
        for c in off_topic:
            out.append(f"- {c.canonical_question} · {c.times_asked}/{n} ({_ep_ref(ep_num, by_id, c)})")
        out.append("")
        out.append("</details>")
        out.append("")

    out.append("## Interviews scanned")
    out.append("")
    for ep in episodes:
        i = ep_num[ep.video.id]
        dur = f" · {format_timestamp(ep.video.duration)}" if ep.video.duration else ""
        title = _md_escape(ep.video.title)
        link = f"[{title}]({ep.video.link()})" if not ep.video.local_path else f"{title} (`{ep.video.local_path}`)"
        out.append(f"{i}. {link} · {_md_escape(ep.video.channel)}{dur} · {len(ep.questions)} questions")
    out.append("")

    if audio_only:
        out.append("## Also found (audio only, not analysed)")
        out.append("")
        for a in audio_only:
            outlet = f" · {_md_escape(a['outlet'])}" if a.get("outlet") else ""
            out.append(f"- [{_md_escape(a.get('title') or a['url'])}]({a['url']}){outlet}")
        out.append("")
    return "\n".join(out)


def to_json(
    guest: str,
    episodes: list[Episode],
    clusters: list[Cluster],
    suggestions: Suggestions | None = None,
    audio_only: list[dict] | None = None,
    guide: dict | None = None,
) -> dict:
    return {
        "guest": guest,
        "guide": guide,
        "suggestions": asdict(suggestions) if suggestions else None,
        "audio_only": audio_only or [],
        "episodes": [
            {
                "video": asdict(ep.video),
                "questions": [asdict(q) for q in ep.questions],
                "unexplored_threads": ep.unexplored_threads,
                "quotes": ep.quotes,
            }
            for ep in episodes
        ],
        "clusters": [
            {
                "label": c.label,
                "canonical_question": c.canonical_question,
                "times_asked": c.times_asked,
                "on_topic": c.on_topic,
                "members": [asdict(q) for q in c.members],
            }
            for c in clusters
        ],
    }
