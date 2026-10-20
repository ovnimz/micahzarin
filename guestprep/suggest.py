"""Turn "what they've been asked" into "what to ask instead"."""
from __future__ import annotations

from dataclasses import dataclass, field

from .llm import call_json
from .models import Cluster, Episode

SYSTEM = """\
You are helping a podcast host prepare to interview a guest. You have every \
question the guest was asked across their previous interviews (grouped, with \
how many interviews asked each), and the threads they raised that no host \
followed up on. Your job is to find what nobody has asked them yet.

Write two lists.

fresh_questions (8-12): questions this guest has not been asked that are \
likely to get a thoughtful, unrehearsed answer. Build on the unexplored \
threads, on tensions between things they've said, and on the specifics of \
their work. Nothing that rephrases a question already on the asked list. \
Questions that would work for any guest only belong here if the angle is \
specific to this one. For each: the question, why it should land with this \
guest, and what it builds on (a thread or topic, or "").

no_context_prompts (8-12): standalone prompts the audience can follow with \
no background on the guest or their work, chosen because this particular \
guest would answer them in an unusual way. Use a range of techniques: thought \
experiments, forced choices, "what would you bet on", "what do you believe \
that most people in your field don't", steelmanning their critics, "what \
would change your mind", ranking or rapid-fire, explain-it-to-a-12-year-old, \
counterfactuals, "what's the most underrated...". Also none already asked. \
For each: the prompt, the technique (2-4 words), and why this guest would \
give an interesting answer.

Write everything in plain, conversational wording a host can read aloud."""

SCHEMA = {
    "type": "object",
    "properties": {
        "fresh_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "why": {"type": "string"},
                    "builds_on": {"type": "string"},
                },
                "required": ["question", "why", "builds_on"],
                "additionalProperties": False,
            },
        },
        "no_context_prompts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "technique": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["prompt", "technique", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fresh_questions", "no_context_prompts"],
    "additionalProperties": False,
}


@dataclass
class Suggestions:
    fresh_questions: list[dict] = field(default_factory=list)
    no_context_prompts: list[dict] = field(default_factory=list)


def suggest(client, model: str, guest: str, about: str, episodes: list[Episode], clusters: list[Cluster]) -> Suggestions:
    n = len(episodes)
    asked = "\n".join(f"- ({c.times_asked}/{n}) {c.canonical_question}" for c in clusters)
    threads = "\n".join(f"- {t}" for ep in episodes for t in ep.unexplored_threads) or "(none found)"
    titles = "\n".join(f"- {ep.video.title} [{ep.video.channel}]" for ep in episodes)
    user = (
        f"Guest: {guest}\n"
        + (f"Who they are: {about}\n" if about else "")
        + f"\nPrevious interviews:\n{titles}\n"
        + f"\nAlready asked (interviews that asked it / {n}):\n{asked}\n"
        + f"\nThreads nobody followed up on:\n{threads}\n"
    )
    data = call_json(client, model=model, system=SYSTEM, user=user, schema=SCHEMA, effort="high")
    if data is None:
        return Suggestions()
    return Suggestions(data.get("fresh_questions", []), data.get("no_context_prompts", []))
