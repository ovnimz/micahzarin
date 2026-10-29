"""Claude looks at each thumbnail (with its title) and tags what's in it."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from guestprep.llm import call_json

from .models import ChannelVideo

PROMPT_VERSION = "v1"

SYSTEM = """\
You are analysing a YouTube channel's thumbnails and titles so the creator can \
learn which packaging gets clicked. You see one thumbnail and its title. \
Describe what is actually there, plainly and consistently, so hundreds of \
these can be compared. Don't judge whether it is good; just tag it.

The channel host appears in most thumbnails; a guest usually appears too. If \
you can't tell who is who, the person who is the subject of the title is the \
guest. Use the channel description if given to identify the host.

Fields:
- faces: number of human faces.
- host_visible / guest_visible: whether each appears.
- eye_contact: someone looks straight at the camera.
- dominant_emotion: the strongest expression shown.
- text_overlay: the exact words printed on the thumbnail ("" if none).
- text_words: how many words that is.
- text_vs_title: "repeats" if the overlay mostly repeats the title, \
"complements" if it adds something different, "none" if there is no text.
- graphic_elements: arrows, circles, logos, product shots, red X's, etc. \
(true if any).
- background: the kind of background.
- colours: 2-4 main colours, comma-separated.
- readability: 1-5, how readable the whole thing is at phone size (small).
- clutter: 1-5, how busy it is.
- curiosity: what question it makes a viewer want answered, in one short \
sentence ("" if none).
- title_style: the title's main device.
- guest_named_in_title: the guest's name is in the title.
- guest_name: the guest's name if you can tell from the title ("" if not).
- title_promise: what the title promises the viewer will get, in a few words.
- summary: one sentence describing the thumbnail."""

EMOTIONS = ["neutral", "smiling/happy", "laughing", "serious/intense", "shocked/surprised", "worried/sad", "angry", "thinking/curious", "no faces"]
STYLES = ["guest name only", "quote from the episode", "bold claim", "question", "how-to / advice", "story / personal", "list / number", "contrarian / warning", "topic label", "other"]
BACKGROUNDS = ["studio/set", "plain colour", "photo / location", "graphic / collage", "screenshot", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "faces": {"type": "integer"},
        "host_visible": {"type": "boolean"},
        "guest_visible": {"type": "boolean"},
        "eye_contact": {"type": "boolean"},
        "dominant_emotion": {"type": "string", "enum": EMOTIONS},
        "text_overlay": {"type": "string"},
        "text_words": {"type": "integer"},
        "text_vs_title": {"type": "string", "enum": ["repeats", "complements", "none"]},
        "graphic_elements": {"type": "boolean"},
        "background": {"type": "string", "enum": BACKGROUNDS},
        "colours": {"type": "string"},
        "readability": {"type": "integer"},
        "clutter": {"type": "integer"},
        "curiosity": {"type": "string"},
        "title_style": {"type": "string", "enum": STYLES},
        "guest_named_in_title": {"type": "boolean"},
        "guest_name": {"type": "string"},
        "title_promise": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": [
        "faces", "host_visible", "guest_visible", "eye_contact", "dominant_emotion", "text_overlay",
        "text_words", "text_vs_title", "graphic_elements", "background", "colours", "readability",
        "clutter", "curiosity", "title_style", "guest_named_in_title", "guest_name", "title_promise", "summary",
    ],
    "additionalProperties": False,
}

MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def tag_thumbnail(client, model: str, video: ChannelVideo, channel_about: str, cache_dir: Path, effort: str = "low") -> dict:
    path = Path(video.thumbnail_path)
    cache = cache_dir / "vision" / f"{PROMPT_VERSION}-{model}-{path.stem}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg"),
                "data": base64.standard_b64encode(path.read_bytes()).decode(),
            },
        },
        {"type": "text", "text": f"Channel: {channel_about}\nTitle: {video.title}"},
    ]
    data = call_json(client, model=model, system=SYSTEM, user=content, schema=SCHEMA, effort=effort, max_tokens=16000)
    if data is None:
        return {}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data
