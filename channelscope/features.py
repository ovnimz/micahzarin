"""Measurable title and thumbnail traits, computed locally (no API calls)."""
from __future__ import annotations

import re
from pathlib import Path

from guestprep.models import parse_timestamp

_WORD = re.compile(r"[A-Za-z0-9'’]+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_TIMESTAMP = re.compile(r"(?<![\d:])((?:\d{1,2}:)?\d{1,2}:\d{2})(?![\d:])")

NEGATIVE = {
    "never", "stop", "worst", "wrong", "mistake", "mistakes", "lie", "lies", "lied", "fail", "failed",
    "failure", "dead", "die", "dying", "death", "broke", "danger", "dangerous", "warning", "regret",
    "hate", "fear", "afraid", "scary", "truth", "secret", "nobody", "ruin", "ruined", "crisis", "end",
}
FIRST_PERSON = {"i", "i'm", "im", "my", "me", "we", "our", "us"}
SECOND_PERSON = {"you", "your", "you're", "youre", "yours"}
QUESTION_WORDS = {"why", "how", "what", "who", "when", "where", "which", "should", "can", "is", "are", "does", "do"}


def title_features(title: str) -> dict:
    words = _WORD.findall(title)
    lower = [w.lower().replace("’", "'") for w in words]
    caps = [w for w in words if len(w) >= 2 and w.isupper() and not w.isdigit()]
    return {
        "title_chars": len(title),
        "title_words": len(words),
        "has_number": any(ch.isdigit() for ch in title),
        "is_question": title.rstrip().endswith("?") or (bool(lower) and lower[0] in QUESTION_WORDS),
        "has_separator": bool(re.search(r"\s[|:–—-]\s|:\s", title)),
        "has_quote": bool(re.search(r"[\"“”]", title)),
        "has_brackets": bool(re.search(r"[(\[]", title)),
        "has_emoji": bool(_EMOJI.search(title)),
        "has_exclamation": "!" in title,
        "caps_words": len(caps),
        "first_person": any(w in FIRST_PERSON for w in lower),
        "second_person": any(w in SECOND_PERSON for w in lower),
        "negative_words": sum(w in NEGATIVE for w in lower),
    }


def thumbnail_pixels(path: Path) -> dict:
    """Brightness, contrast, saturation and colourfulness, each 0-100-ish."""
    from PIL import Image, ImageStat

    with Image.open(path) as im:
        im = im.convert("RGB").resize((320, 180))
        gray = ImageStat.Stat(im.convert("L"))
        sat = ImageStat.Stat(im.convert("HSV").getchannel("S"))
        raw = im.tobytes()
    r, g, b = raw[0::3], raw[1::3], raw[2::3]
    # Hasler & Süsstrunk colourfulness
    rg = [x - y for x, y in zip(r, g)]
    yb = [(x + y) / 2 - z for x, y, z in zip(r, g, b)]
    def mean_std(xs):
        m = sum(xs) / len(xs)
        return m, (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5
    (mrg, srg), (myb, syb) = mean_std(rg), mean_std(yb)
    colourfulness = (srg ** 2 + syb ** 2) ** 0.5 + 0.3 * (mrg ** 2 + myb ** 2) ** 0.5
    return {
        "brightness": round(gray.mean[0] / 2.55, 1),
        "contrast": round(gray.stddev[0] / 1.28, 1),
        "saturation": round(sat.mean[0] / 2.55, 1),
        "colourfulness": round(colourfulness, 1),
    }


def comment_timestamps(text: str, max_seconds: float | None = None) -> list[float]:
    """'the bit at 41:20 was incredible' -> [2480.0]."""
    out = []
    for m in _TIMESTAMP.finditer(text):
        s = parse_timestamp(m.group(1))
        if s is not None and (max_seconds is None or s <= max_seconds):
            out.append(s)
    return out
