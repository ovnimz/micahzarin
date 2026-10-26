"""The briefing half of the prep sheet: who the guest is, their ideas, what to read."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

from . import web
from .discover import video_id_from_url
from .models import Episode, format_timestamp

GUIDE_TTL = 7 * 24 * 3600

SYSTEM = """\
You are a researcher writing a briefing document for a podcast host who is \
about to interview a guest. The host wants to walk in knowing the guest's \
story and ideas as well as a close reader of their work would.

Research the guest with web search and web fetch: their background (where \
they're from, education, career turns), what they're known for, their books, \
essays, papers, talks and recent work, the ideas they keep returning to, and \
the criticisms or debates around them. Read primary sources (their own \
writing and talks) where you can, not just profiles.

You are also given quotes from the guest's previous interviews, each with \
its source link.

Write:
- summary: 2-3 sentences on who they are and why they matter.
- background: a detailed biography in several paragraphs (separate \
paragraphs with a blank line). Concrete facts, dates and turning points.
- timeline: the key dated events in order.
- ideas: their 5-10 most important ideas, each explained in depth (what they \
claim, why they believe it, how it has evolved, where it's contested) in a \
paragraph or two, with 1-3 supporting quotes.
- reading_list: what the host should read, watch or listen to, IN THE ORDER \
they should take it: start with the most accessible overview, then the \
central works, then deeper or newer material. For each item give the \
background it needs or assumes, and why it matters for the interview.
- criticisms: the main criticisms of or disagreements with their work, fairly \
stated, with sources.

Quotes must be the guest's exact words, copied character for character from \
a page you fetched or from the interview quotes provided. Never paraphrase \
inside quotation marks, never stitch separate sentences together, and give \
the exact URL you took each quote from. If you can't find an exact quote for \
an idea, give none rather than an approximate one. Every link must be one \
you actually saw.

When you're done, call submit_guide once with everything."""

_QUOTE = {
    "type": "object",
    "properties": {
        "quote": {"type": "string"},
        "source_title": {"type": "string"},
        "source_url": {"type": "string"},
    },
    "required": ["quote", "source_title", "source_url"],
    "additionalProperties": False,
}

SUBMIT_TOOL = {
    "name": "submit_guide",
    "description": "Submit the finished briefing on the guest. Call once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "background": {"type": "string"},
            "timeline": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"when": {"type": "string"}, "what": {"type": "string"}},
                    "required": ["when", "what"],
                    "additionalProperties": False,
                },
            },
            "ideas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "explanation": {"type": "string"},
                        "quotes": {"type": "array", "items": _QUOTE},
                    },
                    "required": ["name", "explanation", "quotes"],
                    "additionalProperties": False,
                },
            },
            "reading_list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "kind": {"type": "string", "description": "book, essay, paper, talk, podcast, video..."},
                        "background_needed": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["title", "url", "kind", "background_needed", "why"],
                    "additionalProperties": False,
                },
            },
            "criticisms": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"point": {"type": "string"}, "source_url": {"type": "string"}},
                    "required": ["point", "source_url"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "background", "timeline", "ideas", "reading_list", "criticisms"],
        "additionalProperties": False,
    },
}

TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 12},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 12},
    SUBMIT_TOOL,
]


def build_guide(
    client, model: str, guest: str, about: str, topic: str, episodes: list[Episode], cache_dir: Path,
    effort: str = "high",
) -> dict | None:
    ep_key = ",".join(sorted(ep.video.id for ep in episodes))
    key = hashlib.sha1(f"{guest}|{about}|{topic}|{ep_key}|{model}".lower().encode()).hexdigest()[:12]
    cache = cache_dir / "guide" / f"{key}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < GUIDE_TTL:
        guide = json.loads(cache.read_text())
    else:
        lines = []
        for ep in episodes:
            for q in ep.quotes:
                ts = format_timestamp(q.get("seconds"))
                lines.append(
                    f'- "{q["quote"]}" ({q["about"]}) [source: {ep.video.title}, {ep.video.channel}'
                    f'{", " + ts if ts else ""}; url: {ep.video.link(q.get("seconds"))}]'
                )
        user = (
            f"Guest: {guest}\n"
            + (f"Who they are: {about}\n" if about else "")
            + (f"The interview will focus on: {topic}. Weight the ideas and reading list toward it.\n" if topic else "")
            + "\nQuotes from their previous interviews:\n"
            + ("\n".join(lines) or "(none)")
        )
        try:
            guide, _ = web.run_until_tool(client, model, SYSTEM, user, TOOLS, "submit_guide", effort=effort, max_tokens=32000, rounds=12)
        except web.Refused:
            print("  guide request was declined; skipping the guide", file=sys.stderr)
            return None
        if guide is None:
            print("  Claude didn't finish the guide; skipping it", file=sys.stderr)
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(guide, indent=1))
    verify_quotes(guide, episodes, cache_dir)
    return guide


# ---------------------------------------------------------------- quote checking

def _norm(s: str) -> str:
    s = s.lower().replace("’", "").replace("'", "")
    return " ".join(re.findall(r"[a-z0-9]+", s))


def quote_in(quote: str, text_norm: str) -> bool:
    """Exact match after normalising case/punctuation; '...' and [brackets] may skip words."""
    parts = [_norm(p) for p in re.split(r"\.\.\.|…|\[[^\]]*\]", quote)]
    parts = [p for p in parts if len(p.split()) >= 3]
    return bool(parts) and all(p in text_norm for p in parts)


def verify_quotes(guide: dict, episodes: list[Episode], cache_dir: Path) -> None:
    """Mark each quote verified=True/False by finding it in its source (or any transcript we read)."""
    transcripts = {ep.video.id: _norm(" ".join(s.text for s in ep.segments)) for ep in episodes}
    by_url = {}
    for ep in episodes:
        for u in (ep.video.url, ep.video.page_url):
            if u:
                by_url[u.split("#")[0]] = ep.video.id
    pages: dict[str, str | None] = {}

    def source_text(url: str) -> str | None:
        vid = by_url.get(url.split("#")[0]) or (video_id_from_url(url) if "youtu" in url else None)
        if vid in transcripts:
            return transcripts[vid]
        if url not in pages:
            got = web.page_text(url, cache_dir, quiet=True) if url.startswith("http") else None
            pages[url] = _norm(got[1]) if got else None
        return pages[url]

    for idea in guide.get("ideas", []):
        for q in idea.get("quotes", []):
            src = source_text(q.get("source_url", ""))
            q["verified"] = bool(src and quote_in(q["quote"], src)) or any(
                quote_in(q["quote"], t) for t in transcripts.values()
            )


# ---------------------------------------------------------------- rendering

def _esc(s: str) -> str:
    return s.replace("\n", " ").strip()


def render(guide: dict) -> list[str]:
    out = ["## Who they are", ""]
    if guide.get("summary"):
        out += [f"**{_esc(guide['summary'])}**", ""]
    for para in re.split(r"\n\s*\n", guide.get("background", "")):
        if para.strip():
            out += [_esc(para), ""]

    if guide.get("timeline"):
        out += ["### Timeline", ""]
        out += [f"- **{_esc(t['when'])}**: {_esc(t['what'])}" for t in guide["timeline"]]
        out.append("")

    if guide.get("ideas"):
        out += ["## Their ideas", ""]
        for i, idea in enumerate(guide["ideas"], 1):
            out += [f"### {i}. {_esc(idea['name'])}", ""]
            for para in re.split(r"\n\s*\n", idea.get("explanation", "")):
                if para.strip():
                    out += [_esc(para), ""]
            for q in idea.get("quotes", []):
                flag = "" if q.get("verified") else " · _⚠ couldn't find this exact wording in the source; check before quoting_"
                out += [f"> \"{_esc(q['quote'])}\"  ", f"> [{_esc(q['source_title'])}]({q['source_url']}){flag}", ""]

    if guide.get("reading_list"):
        out += ["## What to read and listen to, in this order", ""]
        for i, r in enumerate(guide["reading_list"], 1):
            out.append(f"{i}. **[{_esc(r['title'])}]({r['url']})** _({_esc(r['kind'])})_  ")
            out.append(f"   {_esc(r['why'])}  ")
            if r.get("background_needed"):
                out.append(f"   _Background: {_esc(r['background_needed'])}_")
        out.append("")

    if guide.get("criticisms"):
        out += ["## Criticisms and pushback", ""]
        out += [f"- {_esc(c['point'])} ([source]({c['source_url']}))" for c in guide["criticisms"]]
        out.append("")
    return out
