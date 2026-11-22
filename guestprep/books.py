"""--book: the guest's books (or papers) as PDFs, read section by section into the guide.

A book is far too long to hand to the guide in one go, so each section of
about 40k tokens is read once by the reader model. It writes a digest: what
the section argues, its key ideas, and exact quotes with PDF page numbers.
The digests are cached. The guide is then written from all the digests, and
every quote is checked against the PDF's own text.
"""
from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .llm import LLMError, call_json

SECTION_CHARS = 150_000  # ~40k tokens of book text per reading call
MIN_TEXT_CHARS = 2_000  # less than this in a whole PDF means it's a scan with no text layer

SYSTEM = """\
You are helping a podcast host prepare to interview the author of a book. \
You are given one section of the book, with [p. N] markers giving the PDF \
page each passage starts on.

Write a digest of this section for someone who has not read it:
- summary: what this section does and argues, in a paragraph or two. If it \
is front or back matter (contents, index, bibliography, acknowledgements), \
say so in one line and leave ideas empty.
- ideas: the section's main ideas or arguments. For each, explain the claim, \
the reasoning or examples given for it, and what it's arguing against. Give \
1-3 quotes per idea.
- examples: memorable examples, thought experiments, stories or analogies \
the author uses, in a line each, with their page. These make good interview \
material.
- personal: anything the author says about their own life, motives or how \
they came to the view, with its page.

Quotes must be the author's exact words, copied character for character from \
the text (fix only line-break hyphenation), each with the page from the \
nearest [p. N] marker before it. Never paraphrase inside a quote."""

_PAGE_ITEM = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "page": {"type": "integer"}},
    "required": ["text", "page"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "explanation": {"type": "string"},
                    "quotes": {"type": "array", "items": _PAGE_ITEM},
                },
                "required": ["name", "explanation", "quotes"],
                "additionalProperties": False,
            },
        },
        "examples": {"type": "array", "items": _PAGE_ITEM},
        "personal": {"type": "array", "items": _PAGE_ITEM},
    },
    "required": ["summary", "ideas", "examples", "personal"],
    "additionalProperties": False,
}


def pdf_paths(args: list[Path]) -> list[Path]:
    """Each argument is a PDF or a folder of them."""
    out: list[Path] = []
    for p in args:
        p = p.expanduser()
        if p.is_dir():
            out += sorted(q for q in p.iterdir() if q.suffix.lower() == ".pdf")
        elif p.is_file():
            out.append(p)
        else:
            print(f"  no such file: {p}", file=sys.stderr)
    return out


def load_pdf(path: Path) -> dict | None:
    """-> {title, path, url, pages: [text per page]}, or None if there's no text to read."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        pages = [(pg.extract_text() or "") for pg in reader.pages]
        meta_title = (reader.metadata.title if reader.metadata else None) or ""
    except Exception as e:  # pypdf raises many types on damaged or encrypted files
        print(f"  couldn't read {path.name}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if sum(len(p) for p in pages) < MIN_TEXT_CHARS:
        print(
            f"  {path.name} has no text layer (a scan?). Run it through OCR first, e.g. "
            f"`ocrmypdf in.pdf out.pdf`, then pass the new file.",
            file=sys.stderr,
        )
        return None
    title = meta_title.strip() if len(meta_title.strip()) > 3 else path.stem.replace("_", " ").replace("-", " ")
    return {"title": title, "path": str(path), "url": path.resolve().as_uri(), "pages": pages}


def sections(pages: list[str], max_chars: int = SECTION_CHARS) -> list[tuple[int, int, str]]:
    """Split into runs of whole pages: -> [(first_page, last_page, text with [p. N] markers)], 1-based."""
    out: list[tuple[int, int, str]] = []
    buf: list[str] = []
    start = 1
    size = 0
    for i, text in enumerate(pages, 1):
        chunk = f"[p. {i}]\n{text.strip()}\n"
        if buf and size + len(chunk) > max_chars:
            out.append((start, i - 1, "".join(buf)))
            buf, size, start = [], 0, i
        buf.append(chunk)
        size += len(chunk)
    if buf:
        out.append((start, len(pages), "".join(buf)))
    return out


def _digest(client, model: str, guest: str, book: dict, sec: tuple[int, int, str], cache_dir: Path, effort: str) -> dict | None:
    first, last, text = sec
    key = hashlib.sha1(f"{model}|{text}".encode()).hexdigest()[:16]
    cache = cache_dir / "books" / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    user = (
        f"Author: {guest}\nBook: {book['title']}\nThis section: PDF pages {first}-{last} of {len(book['pages'])}\n\n"
        f"<book_section>\n{text}\n</book_section>"
    )
    try:
        data = call_json(client, model=model, system=SYSTEM, user=user, schema=SCHEMA, effort=effort)
    except LLMError as e:
        print(f"  couldn't read pp. {first}-{last} of {book['title']}: {e}", file=sys.stderr)
        return None
    if data is None:
        return None
    data = {"first_page": first, "last_page": last, **data}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=1))
    return data


def collect(paths: list[Path], client, model: str, guest: str, cache_dir: Path, effort: str = "low") -> list[dict]:
    """-> [{title, path, url, pages, digests}] for each readable PDF."""
    books = [b for b in (load_pdf(p) for p in pdf_paths(paths)) if b]
    jobs = [(b, s) for b in books for s in sections(b["pages"])]
    for b in books:
        print(f"  {b['title']}: {len(b['pages'])} pages", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda j: _digest(client, model, guest, j[0], j[1], cache_dir, effort), jobs))
    for b in books:
        b["digests"] = []
    for (b, _), d in zip(jobs, results):
        if d:
            b["digests"].append(d)
    return [b for b in books if b["digests"]]


def full_text(book: dict) -> str:
    return " ".join(book["pages"])


def for_prompt(books: list[dict]) -> str:
    """The digests, laid out for the guide prompt."""
    parts = []
    for b in books:
        lines = [f"### {b['title']} ({len(b['pages'])} pages)", f"url: {b['url']}"]
        for d in b["digests"]:
            lines.append(f"\n#### Pages {d['first_page']}-{d['last_page']}\n{d['summary']}")
            for idea in d.get("ideas", []):
                lines.append(f"- **{idea['name']}**: {idea['explanation']}")
                lines += [f'  - "{q["text"]}" (p. {q["page"]}; url: {b["url"]}#page={q["page"]})' for q in idea.get("quotes", [])]
            lines += [f"- Example (p. {e['page']}): {e['text']}" for e in d.get("examples", [])]
            lines += [f"- About the author (p. {e['page']}): {e['text']}" for e in d.get("personal", [])]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def titles(books: list[dict]) -> str:
    """Book titles plus their idea names, for the fresh-questions prompt."""
    out = []
    for b in books:
        ideas = [i["name"] for d in b["digests"] for i in d.get("ideas", [])]
        examples = [f"{e['text']} (p. {e['page']})" for d in b["digests"] for e in d.get("examples", [])]
        out.append(f"- {b['title']}")
        if ideas:
            out.append("  ideas: " + "; ".join(ideas[:40]))
        if examples:
            out.append("  examples: " + "; ".join(examples[:20]))
    return "\n".join(out)
