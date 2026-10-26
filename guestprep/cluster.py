"""Group the same question asked across different interviews."""
from __future__ import annotations

import re

from .llm import call_json
from .models import Cluster, Episode, Question

SYSTEM = """\
You are helping a podcast host prepare to interview a guest. Below are \
questions the guest has already been asked across many previous interviews \
on different shows. Group together questions that are essentially the same \
ask: ones that would pull the same story, anecdote, or answer out of the \
guest, even if worded differently. Questions on the same broad topic that \
would get different answers belong in different groups.

For each group give:
- label: 2-6 word name for the question
- canonical_question: one clear phrasing of what keeps being asked
- ids: every question id in the group

Every id must appear in exactly one group. A question nobody else asked is a \
group of one."""

SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "canonical_question": {"type": "string"},
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "on_topic": {"type": "boolean"},
                },
                "required": ["label", "canonical_question", "ids", "on_topic"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clusters"],
    "additionalProperties": False,
}


def _index(episodes: list[Episode]) -> dict[str, Question]:
    ids: dict[str, Question] = {}
    for e_i, ep in enumerate(episodes, 1):
        for q_i, q in enumerate(ep.questions, 1):
            ids[f"e{e_i}q{q_i}"] = q
    return ids


def cluster_with_claude(
    client, model: str, guest: str, episodes: list[Episode], topic: str = "", effort: str = "medium"
) -> list[Cluster]:
    ids = _index(episodes)
    if not ids:
        return []
    ep_num = {ep.video.id: i for i, ep in enumerate(episodes, 1)}
    lines = [f"{qid} (interview {ep_num[q.episode_id]}): {q.gist}" for qid, q in ids.items()]
    focus = (
        f"The host's interview will focus on: {topic}. Set on_topic for groups about that topic.\n\n"
        if topic
        else "There is no particular focus; set on_topic to true for every group.\n\n"
    )
    user = f"Guest: {guest}\n{focus}Questions:\n" + "\n".join(lines)
    data = call_json(client, model=model, system=SYSTEM, user=user, schema=SCHEMA, effort=effort, max_tokens=64000)
    if data is None:
        return cluster_heuristic(episodes)
    return _assemble(data.get("clusters", []), ids)


def _assemble(raw_clusters: list[dict], ids: dict[str, Question]) -> list[Cluster]:
    """Trust but verify: drop unknown/duplicate ids, give any missed ids their own cluster."""
    used: set[str] = set()
    out: list[Cluster] = []
    for c in raw_clusters:
        members = []
        for qid in c.get("ids", []):
            if qid in ids and qid not in used:
                used.add(qid)
                members.append(ids[qid])
        if members:
            out.append(Cluster(
                label=c.get("label", ""),
                canonical_question=c.get("canonical_question") or members[0].gist,
                members=members,
                on_topic=c.get("on_topic", True),
            ))
    for qid, q in ids.items():
        if qid not in used:
            out.append(Cluster(label=q.topic or q.gist, canonical_question=q.gist, members=[q]))
    return sort_clusters(out)


def sort_clusters(clusters: list[Cluster]) -> list[Cluster]:
    return sorted(clusters, key=lambda c: (-c.times_asked, -len(c.members), c.label.lower()))


# ---------------------------------------------------------------- no-LLM fallback

_STOP = set("""
a an the and or but if so of to in on at for with about from by as is are was were be been being
do does did done you your yours i me my we our us it its this that these those what why how when
where who which would could should can will just really kind sort like think mean know get got
tell talk walk through explain there their they them he she his her have has had not no yes
some any more most very much many one thing things lot
""".split())


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z']+", text.lower()) if w not in _STOP and len(w) > 2}


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def cluster_heuristic(episodes: list[Episode], threshold: float = 0.5) -> list[Cluster]:
    """Greedy keyword-overlap grouping. Catches near-verbatim repeats, misses paraphrases."""
    groups: list[tuple[set[str], list[Question]]] = []
    for ep in episodes:
        for q in ep.questions:
            toks = _tokens(q.gist)
            if not toks:
                continue
            best, best_score = None, 0.0
            for g in groups:
                score = len(toks & g[0]) / len(toks | g[0])
                if score > best_score:
                    best, best_score = g, score
            if best is not None and best_score >= threshold:
                best[1].append(q)
            else:
                groups.append((toks, [q]))
    return sort_clusters([Cluster(label=qs[0].topic or qs[0].gist, canonical_question=qs[0].gist, members=qs) for _, qs in groups])
