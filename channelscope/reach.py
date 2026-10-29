"""Impressions and click-through rate.

The Analytics API doesn't expose them. Two ways in:
  - a YouTube Studio export (Advanced mode > Export > CSV): lifetime numbers, available now
  - the Reporting API's daily reach report: automatic, but only from when the job
    was first created, so it fills in over time
"""
from __future__ import annotations

import csv
import io
import re
import sys
from collections import defaultdict
from pathlib import Path

REPORT_TYPE = "channel_reach_basic_a1"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ---------------------------------------------------------------- Reporting API

def ensure_job(yr) -> tuple[str, bool]:
    """-> (job id, created just now)."""
    for job in yr.jobs().list().execute().get("jobs", []):
        if job.get("reportTypeId") == REPORT_TYPE:
            return job["id"], False
    job = yr.jobs().create(body={"reportTypeId": REPORT_TYPE, "name": "channelscope reach"}).execute()
    return job["id"], True


def download_reports(yr, creds, job_id: str, directory: Path) -> list[Path]:
    """Download any daily reports we don't have yet. Returns every report file on disk."""
    from google.auth.transport.requests import AuthorizedSession

    session = None
    token = None
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        resp = yr.jobs().reports().list(jobId=job_id, pageToken=token).execute()
        for rep in resp.get("reports", []):
            # YouTube sometimes regenerates a day; the file name keeps both so the newest wins.
            name = f"{rep['startTime'][:10]}_{rep['createTime'].replace(':', '')}.csv"
            path = directory / name
            if path.exists():
                continue
            session = session or AuthorizedSession(creds)
            r = session.get(rep["downloadUrl"], timeout=60)
            r.raise_for_status()
            path.write_bytes(r.content)
        token = resp.get("nextPageToken")
        if not token:
            break
    return sorted(directory.glob("*.csv"))


def aggregate_reports(paths: list[Path]) -> dict[str, dict]:
    """Sum daily impressions per video; CTR is clicks over impressions across all days."""
    latest: dict[str, Path] = {}
    for p in sorted(paths):  # names sort by day, then by creation time
        latest[p.name.split("_", 1)[0]] = p
    rows = []
    for p in latest.values():
        rows += list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8"))))
    if not rows:
        return {}
    ctrs = [_num(r.get("video_thumbnail_impressions_ctr")) for r in rows]
    # The docs call it a percentage but define it as clicks/impressions; accept either scale.
    scale = 100.0 if max((c for c in ctrs if c is not None), default=0) > 1 else 1.0
    imps: dict[str, float] = defaultdict(float)
    clicks: dict[str, float] = defaultdict(float)
    days: dict[str, set] = defaultdict(set)
    for r, ctr in zip(rows, ctrs):
        vid = r.get("video_id", "")
        n = _num(r.get("video_thumbnail_impressions"))
        if not vid or not n:
            continue
        imps[vid] += n
        clicks[vid] += n * (ctr or 0) / scale
        days[vid].add(r.get("date"))
    return {
        vid: {"impressions": int(imps[vid]), "ctr": clicks[vid] / imps[vid], "days": len(days[vid])}
        for vid in imps
    }


# ---------------------------------------------------------------- Studio export

def read_studio_csv(path: Path) -> dict[str, dict]:
    """Parse 'Table data.csv' from a YouTube Studio Content export.

    Columns are matched by name, so exports with extra or reordered columns work.
    """
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().lower() for h in next(reader, [])]
    try:
        id_col = next(i for i, h in enumerate(header) if h in ("content", "video", "video id"))
        imp_col = header.index("impressions")
        ctr_col = next(i for i, h in enumerate(header) if h.startswith("impressions click-through rate"))
    except (StopIteration, ValueError):
        raise ValueError(
            f"{path}: expected 'Content', 'Impressions' and 'Impressions click-through rate (%)' columns. "
            "In Studio: Analytics > Advanced mode > Content tab, add those metrics, then Export > CSV."
        ) from None
    out: dict[str, dict] = {}
    for row in reader:
        if len(row) <= max(id_col, imp_col, ctr_col) or not _VIDEO_ID.match(row[id_col].strip()):
            continue  # the "Total" row, blank lines
        imps, ctr = _num(row[imp_col]), _num(row[ctr_col])
        if imps is None or ctr is None:
            continue
        out[row[id_col].strip()] = {"impressions": int(imps), "ctr": ctr / 100}
    return out


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace(",", "").rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def apply(videos, studio: dict[str, dict], api: dict[str, dict]) -> None:
    """Studio exports are lifetime numbers, so they win over the partial Reporting API history."""
    for v in videos:
        if v.id in studio:
            v.impressions, v.ctr, v.ctr_source = studio[v.id]["impressions"], studio[v.id]["ctr"], "studio"
        elif v.id in api:
            d = api[v.id]
            v.impressions, v.ctr, v.ctr_source = d["impressions"], d["ctr"], f"reporting api ({d['days']}d)"


def report_status(created: bool, n_files: int) -> str | None:
    if created:
        return (
            "Started YouTube's daily reach report (impressions + CTR). The first files take up to "
            "48 hours to appear; run channelscope again after that. For lifetime CTR right now, "
            "export from Studio and pass --studio-csv."
        )
    if n_files == 0:
        return "YouTube hasn't generated the first reach report yet (it can take up to 48 hours)."
    return None


def print_status(msg: str | None) -> None:
    if msg:
        print(f"  {msg}", file=sys.stderr)
