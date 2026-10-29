"""Which title/thumbnail traits go with better CTR, retention and views, on this channel.

Channels grow and shrink, so raw views mislead: a video is scored against the
median of the videos published around it ("2.0x" = twice its neighbours).
With a few dozen videos these are hints, not proof; every row carries its n.
"""
from __future__ import annotations

from statistics import mean, median

from . import features, retention
from .models import ChannelVideo

MIN_VIEW_AGE_DAYS = 14  # younger videos haven't had time to collect views

# Thumbnail/title traits Claude tags (see vision.py) that are worth testing.
TAG_FEATURES = {
    "faces": "num", "text_words": "num", "readability": "num", "clutter": "num",
    "host_visible": "bool", "guest_visible": "bool", "eye_contact": "bool",
    "graphic_elements": "bool", "guest_named_in_title": "bool",
    "dominant_emotion": "cat", "title_style": "cat", "background": "cat",
}


def relative_scores(videos: list[ChannelVideo], value, eligible=lambda v: True, window: int = 5) -> dict[str, float]:
    """value(v) / median(value of up to `window` eligible videos on each side, by publish date)."""
    ordered = sorted((v for v in videos if eligible(v) and value(v) is not None), key=lambda v: v.published)
    out: dict[str, float] = {}
    for i, v in enumerate(ordered):
        neighbours = ordered[max(0, i - window):i] + ordered[i + 1:i + 1 + window]
        vals = [value(n) for n in neighbours]
        base = median(vals) if vals else None
        if base:
            out[v.id] = value(v) / base
    return out


def score_all(videos: list[ChannelVideo]) -> dict[str, dict]:
    """Per video: the targets we try to explain."""
    old_enough = lambda v: v.age_days() >= MIN_VIEW_AGE_DAYS
    views = relative_scores(videos, lambda v: v.views or None, old_enough)
    ctr = relative_scores(videos, lambda v: v.ctr)
    out = {}
    for v in videos:
        r = retention.summary(v.retention, v.duration) if v.retention else {}
        out[v.id] = {
            "views_vs_neighbours": views.get(v.id),
            "ctr": v.ctr,
            "ctr_vs_neighbours": ctr.get(v.id),
            "avg_view_pct": v.avg_view_pct,
            "held_at_30s": r.get("at_30s"),
            "subs_per_1k_views": v.subs_per_1k_views,
        }
    return out


def feature_rows(videos: list[ChannelVideo]) -> dict[str, dict]:
    rows = {}
    for v in videos:
        row = features.title_features(v.title)
        row.update(v.thumbnail_pixels)
        row["duration_min"] = round(v.duration / 60, 1)
        for k, kind in TAG_FEATURES.items():
            val = v.thumbnail_tags.get(k)
            if val is None:
                continue
            row[k] = bool(val) if kind == "bool" and not isinstance(val, bool) else val
        rows[v.id] = row
    return rows


def lift(feature_rows: dict[str, dict], targets: dict[str, dict], target: str, min_n: int = 3) -> list[dict]:
    """Effect of each trait on one target, biggest first.

    bool: mean target with vs. without the trait.  num: Spearman rank correlation.
    cat: mean target per category (categories with at least min_n videos).
    """
    ids = [i for i in feature_rows if targets.get(i, {}).get(target) is not None]
    names = sorted({k for i in ids for k in feature_rows[i]})
    out = []
    for name in names:
        pairs = [(feature_rows[i][name], targets[i][target]) for i in ids if name in feature_rows[i]]
        if len(pairs) < 2 * min_n:
            continue
        sample = pairs[0][0]
        if isinstance(sample, bool):
            yes = [t for f, t in pairs if f]
            no = [t for f, t in pairs if not f]
            if len(yes) < min_n or len(no) < min_n:
                continue
            m_yes, m_no = mean(yes), mean(no)
            out.append({
                "feature": name, "kind": "bool", "with": m_yes, "without": m_no, "n_with": len(yes),
                "n_without": len(no), "effect": (m_yes - m_no) / abs(m_no) if m_no else 0.0,
            })
        elif isinstance(sample, (int, float)):
            rho = spearman([f for f, _ in pairs], [t for _, t in pairs])
            if rho is not None:
                out.append({"feature": name, "kind": "num", "rho": rho, "n": len(pairs), "effect": rho})
        elif isinstance(sample, str):
            groups: dict[str, list[float]] = {}
            for f, t in pairs:
                groups.setdefault(f, []).append(t)
            groups = {k: g for k, g in groups.items() if len(g) >= min_n}
            if len(groups) < 2:
                continue
            overall = mean(t for _, t in pairs)
            cats = sorted(({"value": k, "mean": mean(g), "n": len(g)} for k, g in groups.items()), key=lambda c: -c["mean"])
            spread = (cats[0]["mean"] - cats[-1]["mean"]) / abs(overall) if overall else 0.0
            out.append({"feature": name, "kind": "cat", "categories": cats, "effect": spread})
    return sorted(out, key=lambda r: -abs(r["effect"]))


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def _ranks(xs: list[float]) -> list[float]:
    """Average ranks for ties."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks
