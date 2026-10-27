"""--writing: the guest's own posts (a whole Substack, or individual pages) as primary sources."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from . import web

MAX_CHARS_PER_POST = 20000  # long essays are cut here when handed to Claude
RECENT = 5  # always include the newest few, whatever their likes


def _substack_archive(base: str, cache_dir: Path, limit: int) -> list[dict] | None:
    """Post list from a Substack's archive API, or None if `base` isn't a Substack."""
    import requests

    cache = cache_dir / "writing" / f"archive-{web.page_id(base)[4:]}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    posts: list[dict] = []
    try:
        for offset in range(0, max(limit * 3, 50), 50):
            r = requests.get(f"{base}/api/v1/archive", params={"sort": "new", "offset": offset, "limit": 50}, timeout=20)
            if r.status_code != 200 or not isinstance(r.json(), list):
                return None if not posts else posts
            batch = r.json()
            posts += [
                {
                    "title": p.get("title") or "",
                    "subtitle": p.get("subtitle") or "",
                    "url": p.get("canonical_url") or "",
                    "date": (p.get("post_date") or "")[:10],
                    "likes": p.get("reaction_count") or 0,
                    "paid": p.get("audience") not in (None, "everyone"),
                }
                for p in batch
                if p.get("type", "newsletter") == "newsletter" and p.get("canonical_url")
            ]
            if len(batch) < 50:
                break
    except (requests.RequestException, ValueError):
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(posts, indent=1))
    return posts


def pick_posts(posts: list[dict], limit: int) -> list[dict]:
    """Most-liked posts plus the newest few, newest first."""
    newest = posts[:RECENT]
    liked = sorted(posts, key=lambda p: -p["likes"])
    chosen = {p["url"]: p for p in newest}
    for p in liked:
        if len(chosen) >= limit:
            break
        chosen.setdefault(p["url"], p)
    return sorted(chosen.values(), key=lambda p: p["date"], reverse=True)


def collect(urls: list[str], cache_dir: Path, limit: int = 25) -> list[dict]:
    """-> [{title, subtitle, url, date, likes, paid, text}] for each readable piece."""
    items: list[dict] = []
    for u in urls:
        parsed = urlparse(u if "://" in u else f"https://{u}")
        base = f"{parsed.scheme}://{parsed.netloc}"
        is_root = parsed.path.strip("/") in ("", "archive")
        archive = _substack_archive(base, cache_dir, limit) if is_root else None
        if archive is not None:
            picked = pick_posts(archive, limit)
            print(f"  {base}: {len(archive)} posts, reading {len(picked)}", file=sys.stderr)
            items += picked
        else:
            items.append({"title": "", "subtitle": "", "url": parsed.geturl(), "date": "", "likes": 0, "paid": False})

    def fill(item: dict) -> dict | None:
        got = web.page_text(item["url"], cache_dir)
        if not got:
            return None
        title, text = got
        return {**item, "title": item["title"] or title, "text": text}

    with ThreadPoolExecutor(max_workers=6) as pool:
        return [i for i in pool.map(fill, items) if i]


def for_prompt(items: list[dict]) -> str:
    parts = []
    for i in items:
        note = " (paid post: only the free preview is readable)" if i.get("paid") else ""
        parts.append(f"### {i['title']} ({i['date'] or 'undated'}){note}\nurl: {i['url']}\n{i['text'][:MAX_CHARS_PER_POST]}")
    return "\n\n".join(parts)


def titles(items: list[dict]) -> str:
    return "\n".join(
        f"- {i['title']} ({i['date'] or 'undated'})" + (f": {i['subtitle']}" if i.get("subtitle") else "") for i in items
    )
