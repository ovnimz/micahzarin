"""--web: find appearances beyond YouTube search with Claude's web search tool."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

from .discover import video_id_from_url
from .llm import FALLBACK_BETA
from .models import Segment, Video

DISCOVERY_TTL = 7 * 24 * 3600  # re-search after a week
MIN_PAGE_CHARS = 1500  # shorter than this isn't a transcript or a real Q&A

SYSTEM = """\
You are helping a podcast host research a guest before interviewing them. \
Use web search to find as many of the guest's previous interviews and \
conversations as you can: podcast episodes, video interviews, written Q&A \
interviews, panel discussions, and recorded talks with audience Q&A. Look \
beyond YouTube: show websites, Apple Podcasts / Spotify episode pages, \
university and conference pages, magazines and Substacks.

Prefer pages where the conversation itself is readable: a published \
transcript or a written Q&A. For audio-only episodes, report the episode page.

Only include appearances by this specific person, not namesakes. Skip pages \
that merely mention them, articles they wrote, and book listings.

When you have finished searching, call report_appearances once with \
everything you found."""

REPORT_TOOL = {
    "name": "report_appearances",
    "description": "Report the guest's interviews and conversations found on the web. Call once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "appearances": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "outlet": {"type": "string", "description": "show, publication or host"},
                        "kind": {
                            "type": "string",
                            "enum": ["youtube", "transcript_page", "written_interview", "audio_only", "other"],
                        },
                    },
                    "required": ["url", "title", "outlet", "kind"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["appearances"],
        "additionalProperties": False,
    },
}

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 15}


def discover(client, model: str, guest: str, about: str, cache_dir: Path, topic: str = "") -> list[dict]:
    key = hashlib.sha1(f"{guest}|{about}|{topic}".lower().encode()).hexdigest()[:12]
    cache = cache_dir / "web" / f"discover-{key}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < DISCOVERY_TTL:
        return json.loads(cache.read_text())

    who = f"{guest} ({about})" if about else guest
    ask = f"Guest: {who}" + (f"\nPrioritise conversations that touch on: {topic}" if topic else "")
    messages: list = [{"role": "user", "content": ask}]
    found: list[dict] | None = None
    text_seen: list[str] = []
    for _ in range(6):  # each pause_turn resumes the server-side search loop
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            messages=messages,
            tools=[WEB_SEARCH_TOOL, REPORT_TOOL],
            output_config={"effort": "medium"},
            betas=[FALLBACK_BETA],
            extra_body={"fallbacks": "default"},
        )
        if resp.stop_reason == "refusal":
            print("  web search request was declined; skipping --web", file=sys.stderr)
            return []
        for block in resp.content:
            if block.type == "tool_use" and block.name == "report_appearances":
                found = list(block.input.get("appearances", []))
            elif block.type == "text":
                text_seen.append(block.text)
        if found is not None or resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})

    if found is None:
        # Model wrapped up in prose instead of calling the tool: salvage the links.
        urls = dict.fromkeys(re.findall(r"https?://[^\s)\]>\"']+", "\n".join(text_seen)))
        found = [{"url": u, "title": u, "outlet": "", "kind": "other"} for u in urls]

    found = [a for a in found if a.get("url", "").startswith("http")]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(found, indent=1))
    return found


def split_appearances(appearances: list[dict]) -> tuple[list[str], list[dict], list[dict]]:
    """-> (youtube urls, readable pages, audio-only)."""
    yt, pages, audio = [], [], []
    for a in appearances:
        if video_id_from_url(a["url"]) and ("youtu" in a["url"]):
            yt.append(a["url"])
        elif a.get("kind") == "audio_only":
            audio.append(a)
        else:
            pages.append(a)
    return yt, pages, audio


# ---------------------------------------------------------------- fetching pages

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "button"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "section", "article", "dt", "dd"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _TextExtractor()
    p.feed(html)
    lines = [" ".join(l.split()) for l in "".join(p.parts).splitlines()]
    text = "\n".join(l for l in lines if l)
    return " ".join(p.title.split()), text


def page_id(url: str) -> str:
    return "web:" + hashlib.sha1(url.encode()).hexdigest()[:12]


def fetch_page(appearance: dict, cache_dir: Path) -> tuple[Video, list[Segment]] | None:
    import requests

    url = appearance["url"]
    vid = page_id(url)
    cache = cache_dir / "web" / f"{vid[4:]}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        title, text = data["title"], data["text"]
    else:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (Macintosh) guestprep/0.1"})
            r.raise_for_status()
        except Exception as e:
            print(f"  couldn't fetch {url}: {e}", file=sys.stderr)
            return None
        if "html" not in r.headers.get("content-type", "html"):
            return None
        title, text = html_to_text(r.text)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"title": title, "text": text}))

    if len(text) < MIN_PAGE_CHARS:
        return None
    video = Video(
        id=vid,
        title=appearance.get("title") or title or url,
        channel=appearance.get("outlet") or re.sub(r"^www\.", "", url.split("/")[2]),
        url=url,
        confirmed=True,
        page_url=url,
    )
    return video, [Segment(start=None, text=para) for para in text.split("\n")]

