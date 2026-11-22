"""Which model and effort each stage runs on.

The work differs a lot between stages, so they don't all need the same model:

- transcripts / web: finding the questions in a transcript, or searching for
  appearances, is careful but mechanical. Sonnet 5.5 at medium does it as well
  as Opus, and it's most of the tokens.
- books: reading a dense book or paper and quoting it exactly. Opus 5.5, high.
- grouping: matching the same question across differently worded interviews. Opus 5.5, medium.
- guide / questions: understanding the ideas, connecting them and finding
  angles nobody has asked. Opus 5.5 at high, or xhigh for a dense guest.

A guest counts as dense when you give it their books or papers (--book, --papers)
or say so with --dense. --model, --reader-model and --effort override the choices;
--fast runs every stage on Sonnet 5.5 at medium.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .llm import DEFAULT_MODEL, FAST_MODEL

STAGES = ("transcripts", "web", "books", "grouping", "guide", "questions")
LABELS = {
    "transcripts": "reading interviews",
    "web": "web search",
    "books": "books & papers",
    "grouping": "grouping repeats",
    "guide": "guide",
    "questions": "fresh questions",
}
NAMES = {DEFAULT_MODEL: "Opus 5.5", FAST_MODEL: "Sonnet 5.5"}


@dataclass
class Plan:
    stages: dict[str, tuple[str, str]] = field(default_factory=dict)
    dense: bool = False

    def model(self, stage: str) -> str:
        return self.stages[stage][0]

    def effort(self, stage: str) -> str:
        return self.stages[stage][1]

    def describe(self) -> str:
        parts = [f"{LABELS[s]}: {NAMES.get(m, m)} ({e})" for s, (m, e) in self.stages.items()]
        why = " · dense guest, so the guide thinks harder" if self.dense else ""
        return "Models: " + " · ".join(parts) + why


def make_plan(
    *, fast: bool = False, dense: bool = False, model: str | None = None, reader_model: str | None = None,
    effort: str | None = None,
) -> Plan:
    if fast:
        stages = {s: (FAST_MODEL, "medium") for s in STAGES}
    else:
        main = model or DEFAULT_MODEL
        reader = reader_model or FAST_MODEL
        deep = "xhigh" if dense else "high"
        stages = {
            "transcripts": (reader, "medium"),
            "web": (reader, "medium"),
            "books": (model or DEFAULT_MODEL, "high"),
            "grouping": (main, "medium"),
            "guide": (main, deep),
            "questions": (main, deep),
        }
    if effort:
        stages = {s: (m, effort) for s, (m, _) in stages.items()}
    return Plan(stages=stages, dense=dense)
