"""--papers: the guest's academic publications, from OpenAlex.

PhilPeople, PhilPapers and PhilArchive sit behind Cloudflare's bot check, so a
script can't read them. OpenAlex is a free, open index of the same journals and
books, with abstracts and links to free PDFs (often on PhilArchive itself).

1. Find the guest's OpenAlex author record. Several people usually share a name,
   so Claude picks the one that matches --about (or pass --openalex-author).
2. List their works: the most cited plus the newest, with venue, year and abstract.
   This goes to the guide as a bibliography.
3. Download the free PDFs among them, which are then read like --book PDFs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from .llm import call_json

API = "https://api.openalex.org"
TTL = 7 * 24 * 3600
UA = "Mozilla/5.0 (Macintosh) guestprep/0.1"

PICK_SYSTEM = """\
Several academic author records share the guest's name. Pick the one that is \
the guest, using what the host says about them, the institutions and the \
research topics. If none fits, return index -1."""

PICK_SCHEMA = {
    "type": "object",
    "properties": {"index": {"type": "integer"}, "why": {"type": "string"}},
    "required": ["index", "why"],
    "additionalProperties": False,
}


class PapersError(RuntimeError):
    pass


def _get(path: str, params: dict, cache_dir: Path) -> dict:
    import requests

    params = dict(params)
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    ident = hashlib.sha1(json.dumps([path, sorted((k, v) for k, v in params.items() if k != "api_key")]).encode()).hexdigest()[:16]
    cache = cache_dir / "openalex" / f"{ident}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < TTL:
        return json.loads(cache.read_text())
    try:
        r = requests.get(f"{API}{path}", params=params, timeout=30, headers={"User-Agent": UA})
    except requests.RequestException as e:
        raise PapersError(f"couldn't reach OpenAlex: {e}") from e
    if r.status_code == 429 or (r.status_code != 200 and "Rate limit" in r.text):
        raise PapersError(
            "OpenAlex's free daily limit for your network is used up. Get a free key at "
            "https://openalex.org (Settings > API key), then: export OPENALEX_API_KEY=..."
        )
    if r.status_code != 200:
        raise PapersError(f"OpenAlex returned HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def _short(author_id: str) -> str:
    return author_id.rstrip("/").split("/")[-1]


def describe(a: dict) -> str:
    inst = ", ".join(i["display_name"] for i in (a.get("last_known_institutions") or [])) or "no institution listed"
    topics = ", ".join(t["display_name"] for t in (a.get("topics") or [])[:4])
    return f"{a['display_name']} ({inst}; {a.get('works_count', 0)} works; topics: {topics})"


def find_author(client, model: str, guest: str, about: str, cache_dir: Path, author_id: str = "") -> dict | None:
    if author_id:
        return _get(f"/authors/{_short(author_id)}", {}, cache_dir)
    found = _get("/authors", {"search": guest, "per-page": 10}, cache_dir).get("results", [])
    found = [a for a in found if a.get("works_count", 0) >= 3]
    if not found:
        return None
    if len(found) == 1 and not about:
        return found[0]
    listing = "\n".join(f"{i}. {describe(a)}" for i, a in enumerate(found))
    user = f"Guest: {guest}\nWho they are: {about or '(not given)'}\n\nAuthor records:\n{listing}"
    pick = call_json(client, model=model, system=PICK_SYSTEM, user=user, schema=PICK_SCHEMA, effort="low", max_tokens=4000)
    if not pick or not 0 <= pick["index"] < len(found):
        print("  no OpenAlex author record matches; pass --openalex-author A... to pick one:", file=sys.stderr)
        for a in found[:5]:
            print(f"    {_short(a['id'])}  {describe(a)}", file=sys.stderr)
        return None
    chosen = found[pick["index"]]
    others = [a for a in found if a is not chosen][:3]
    print(f"  OpenAlex author: {describe(chosen)}", file=sys.stderr)
    if others:
        print("  (wrong person? rerun with --openalex-author and one of: "
              + "; ".join(f"{_short(a['id'])} {a['display_name']}" for a in others) + ")", file=sys.stderr)
    return chosen


def abstract(inv: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; put the words back in order."""
    if not inv:
        return ""
    pos = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in pos)


def list_works(author: dict, cache_dir: Path, limit: int = 40) -> list[dict]:
    """Most cited plus the newest, without duplicates, newest first."""
    common = {
        "filter": f"author.id:{_short(author['id'])}",
        "per-page": 50,
        "select": "id,doi,title,publication_year,type,cited_by_count,primary_location,best_oa_location,abstract_inverted_index",
    }
    cited = _get("/works", {**common, "sort": "cited_by_count:desc"}, cache_dir).get("results", [])
    newest = _get("/works", {**common, "sort": "publication_year:desc"}, cache_dir).get("results", [])
    seen: dict[str, dict] = {}
    for w in newest[:10] + cited:
        title = (w.get("title") or "").strip()
        if not title or w.get("type") in ("erratum", "paratext", "peer-review"):
            continue
        k = re.sub(r"[^a-z0-9]", "", title.lower())[:80]
        if k in seen or len(seen) >= limit:
            continue
        src = ((w.get("primary_location") or {}).get("source") or {})
        oa = w.get("best_oa_location") or {}
        seen[k] = {
            "title": title,
            "year": w.get("publication_year"),
            "type": w.get("type") or "",
            "venue": src.get("display_name") or "",
            "cited_by": w.get("cited_by_count") or 0,
            "url": w.get("doi") or w.get("id"),
            "pdf_url": oa.get("pdf_url") or "",
            "abstract": abstract(w.get("abstract_inverted_index")),
        }
    return sorted(seen.values(), key=lambda w: -(w["year"] or 0))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:70] or "paper"


def download_pdfs(works: list[dict], cache_dir: Path, limit: int) -> list[tuple[Path, str]]:
    """-> [(pdf path, title)] for up to `limit` free PDFs, most cited first."""
    import requests

    out: list[tuple[Path, str]] = []
    blocked = 0
    for w in sorted((w for w in works if w["pdf_url"]), key=lambda w: -w["cited_by"]):
        if len(out) >= limit:
            break
        path = cache_dir / "papers" / f"{_slug(w['title'])}-{w['year']}.pdf"
        if not path.exists():
            try:
                r = requests.get(w["pdf_url"], timeout=60, headers={"User-Agent": UA})
            except requests.RequestException:
                blocked += 1
                continue
            if r.status_code != 200 or not r.content.startswith(b"%PDF"):
                blocked += 1  # often a Cloudflare check page instead of the PDF
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
        out.append((path, w["title"]))
    if blocked:
        print(f"  {blocked} free PDF(s) wouldn't download (the site blocks scripts); "
              "save them from your browser and pass them with --book", file=sys.stderr)
    return out


def for_prompt(works: list[dict]) -> str:
    lines = []
    for w in works:
        head = f"- {w['title']} ({w['year'] or 'n.d.'}{', ' + w['venue'] if w['venue'] else ''}; {w['type']}; cited {w['cited_by']}x) {w['url']}"
        lines.append(head)
        if w["abstract"]:
            lines.append(f"  abstract: {w['abstract'][:1500]}")
    return "\n".join(lines)


def titles(works: list[dict]) -> str:
    return "\n".join(f"- {w['title']} ({w['year'] or 'n.d.'}{', ' + w['venue'] if w['venue'] else ''})" for w in works)
