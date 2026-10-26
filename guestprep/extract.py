"""Pull the questions put to the guest out of one interview transcript."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from .llm import call_json
from .models import Episode, Question, Segment, parse_timestamp
from .transcripts import to_prompt_text

PROMPT_VERSION = "v3"  # bump to invalidate cached extractions

SYSTEM = """\
You are helping a podcast host prepare to interview a guest. You will read a \
transcript of a PREVIOUS interview the guest gave on someone else's show and \
list every question the guest was asked, so the host can avoid re-asking the \
same worn-out questions.

The transcript is usually YouTube auto-captions (no speaker labels, little or \
no punctuation, misheard words or names), or text from a web page (a \
published transcript or a written Q&A, possibly with leftover site \
navigation). Infer who is speaking from context.

What counts as a question:
- Anything the host, co-host, or audience puts to the guest to answer, \
including prompts phrased as statements ("Walk me through...", "Tell me about \
the time...", "I want to talk about your book").
- Follow-ups count as separate questions when they ask for something new.

Do not include: the guest's own questions or rhetorical questions, small talk \
("how are you", "thanks for coming on"), sponsor reads, or calls to subscribe.

For each question give:
- timestamp: the [m:ss] marker of the line where it is asked, or "" if the \
transcript has none.
- question: what was asked, cleaned up into a readable sentence. Keep the \
host's framing and specifics.
- gist: a short, show-neutral version that would match the same question \
asked by a different host, e.g. "How did you get started writing about \
money?" Strip host-specific wording and in-jokes.
- topic: 1-4 word topic label.

Also list unexplored_threads: specific things the guest brought up in passing \
(a story, a person, a strong claim, a failure) that the host did not follow \
up on. Phrase each one so a future interviewer could use it as a starting \
point. Skip anything the host did explore.

Also list notable_quotes: 5-12 things the guest said that best capture their \
ideas, beliefs, stories or personality. Copy them exactly as they appear in \
the transcript; you may fix capitalisation and add punctuation, but do not \
reword, merge or tidy them. Use "..." only to cut a few filler words. Give \
the [m:ss] timestamp and a few words on what each quote is about.

Set guest_is_interviewed to false if this is not actually an interview of \
the named guest (e.g. a solo episode about them, a different person with the \
same name, a reaction video). If you are told who the guest is, use that to \
rule out namesakes. In that case return empty lists."""

SCHEMA = {
    "type": "object",
    "properties": {
        "guest_is_interviewed": {"type": "boolean"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string"},
                    "question": {"type": "string"},
                    "gist": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": ["timestamp", "question", "gist", "topic"],
                "additionalProperties": False,
            },
        },
        "unexplored_threads": {"type": "array", "items": {"type": "string"}},
        "notable_quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string"},
                    "quote": {"type": "string"},
                    "about": {"type": "string"},
                },
                "required": ["timestamp", "quote", "about"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["guest_is_interviewed", "questions", "unexplored_threads", "notable_quotes"],
    "additionalProperties": False,
}


def extract_with_claude(
    client, model: str, guest: str, episode: Episode, cache_dir: Path, about: str = "", effort: str = "medium"
) -> bool:
    """Fill episode.questions / unexplored_threads. Returns False if this isn't a real interview."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", episode.video.id)
    about_key = hashlib.sha1(about.encode()).hexdigest()[:8] if about else "any"
    cache = cache_dir / "extract" / f"{PROMPT_VERSION}-{model}-{about_key}-{safe_id}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
    else:
        user = (
            f"Guest: {guest}\n"
            + (f"Who this guest is: {about}\n" if about else "")
            + 
            f"Show/channel: {episode.video.channel}\n"
            f"Episode title: {episode.video.title}\n\n"
            f"<transcript>\n{to_prompt_text(episode.segments)}\n</transcript>"
        )
        data = call_json(client, model=model, system=SYSTEM, user=user, schema=SCHEMA, effort=effort)
        if data is None:
            return False
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, indent=1))

    if not data.get("guest_is_interviewed", True):
        return False
    episode.questions = [
        Question(
            episode_id=episode.video.id,
            question=q["question"].strip(),
            gist=(q.get("gist") or q["question"]).strip(),
            topic=q.get("topic", "").strip(),
            seconds=parse_timestamp(q.get("timestamp")),
        )
        for q in data.get("questions", [])
        if q.get("question", "").strip()
    ]
    episode.unexplored_threads = [t.strip() for t in data.get("unexplored_threads", []) if t.strip()]
    episode.quotes = [
        {"quote": q["quote"].strip(), "about": q.get("about", "").strip(), "seconds": parse_timestamp(q.get("timestamp"))}
        for q in data.get("notable_quotes", [])
        if q.get("quote", "").strip()
    ]
    return True


# ---------------------------------------------------------------- no-LLM fallback

_STARTERS = (
    "what", "why", "how", "when", "where", "who", "which", "do you", "did you", "does",
    "are you", "were you", "is it", "is there", "have you", "would you", "could you",
    "can you", "should", "tell me", "talk about", "walk me through", "explain",
)
_STARTER_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in _STARTERS) + r")\b", re.I)
_SMALL_TALK = re.compile(r"\b(how are you|thanks for (coming|having)|subscribe|sponsor)\b", re.I)


def extract_heuristic(episode: Episode) -> None:
    """Rough fallback: punctuated '?' sentences, else question-word openers.

    Works well on human captions / site transcripts, poorly on raw auto-captions.
    Can't tell host from guest, so it will include some of the guest's own questions.
    """
    qs: list[Question] = []
    for seg in _merge(episode.segments):
        for sent in re.split(r"(?<=[.?!])\s+", seg.text):
            sent = sent.strip()
            words = sent.split()
            if not (5 <= len(words) <= 60) or _SMALL_TALK.search(sent):
                continue
            if sent.endswith("?") or (
                "?" not in seg.text and _STARTER_RE.match(sent)
            ):
                qs.append(Question(episode_id=episode.video.id, question=sent, gist=sent, seconds=seg.start))
    episode.questions = qs


def _merge(segments: list[Segment], window: float = 15.0) -> list[Segment]:
    out: list[Segment] = []
    for s in segments:
        if out and out[-1].start is not None and s.start is not None and s.start - out[-1].start < window:
            out[-1].text += " " + s.text
        else:
            out.append(Segment(**asdict(s)))
    return out
