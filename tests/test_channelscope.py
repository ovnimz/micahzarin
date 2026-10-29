import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from channelscope import auth, cli, features, moments, reach, retention, stats, strategy, vision, youtube
from channelscope.models import ChannelVideo, parse_iso_duration
from guestprep import transcripts
from guestprep.models import Segment


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- small pieces

def test_iso_duration():
    assert parse_iso_duration("PT1H2M3S") == 3723
    assert parse_iso_duration("PT45S") == 45
    assert parse_iso_duration("P1DT1H") == 90000
    assert parse_iso_duration("") == 0


def test_title_features():
    f = features.title_features("Why I QUIT My $200k Job | Jane Doe")
    assert f["is_question"] and f["first_person"] and f["has_number"] and f["has_separator"]
    assert f["caps_words"] == 1 and not f["second_person"]
    g = features.title_features("The truth about negotiation: you're doing it wrong")
    assert g["negative_words"] == 2 and g["second_person"] and not g["is_question"]


def test_comment_timestamps():
    assert features.comment_timestamps("41:20 was insane, also 1:02:03") == [2480, 3723]
    assert features.comment_timestamps("at 99:00", max_seconds=600) == []
    assert features.comment_timestamps("version 1.2:3 and 12:345") == []


def test_thumbnail_pixels(tmp_path):
    from PIL import Image

    p = tmp_path / "t.jpg"
    Image.new("RGB", (64, 36), (255, 0, 0)).save(p)
    px = features.thumbnail_pixels(p)
    assert px["saturation"] > 90 and px["contrast"] < 5


def _curve(cliff_at=0.40, cliff=0.08, spike_at=0.70, spike=0.03):
    """Smooth decay from 0.9 to 0.3, plus one cliff and one rewatch bump."""
    pts, w = [], 0.9
    for i in range(1, 101):
        r = i / 100
        w -= 0.006
        if abs(r - cliff_at) < 1e-9:
            w -= cliff
        if abs(r - spike_at) < 1e-9:
            w += spike
        if abs(r - spike_at - 0.01) < 1e-9:
            w -= spike
        pts.append((r, w, 0.5))
    return pts


def test_retention_events_find_cliff_and_spike():
    curve = _curve()
    evs = retention.events(curve, duration=3600)
    drops = [e for e in evs if e.kind == "drop"]
    spikes = [e for e in evs if e.kind == "spike"]
    assert drops and abs(drops[0].end - 0.40 * 3600) < 1
    assert round(drops[0].size, 3) == 0.086
    assert len(spikes) == 1 and abs(spikes[0].end - 0.70 * 3600) < 1
    # the fall right after the spike is people going back to normal, not leaving
    assert len(drops) == 1


def test_retention_summary_interpolates():
    curve = [(0.0, 1.0, None), (0.5, 0.5, None), (1.0, 0.2, None)]
    s = retention.summary(curve, 100)
    assert s["at_25pct"] == pytest.approx(0.75)
    assert s["at_90pct"] == pytest.approx(0.26)
    assert s["relative_avg"] is None


def test_transcript_window():
    segs = [Segment(10, "a"), Segment(40, "b"), Segment(80, "c")]
    e = retention.Event("drop", 70, 90, 0.05, 0.04)
    assert retention.event_context(segs, e, lead=45, tail=15) == "b c"


def test_studio_csv(tmp_path):
    p = tmp_path / "Table data.csv"
    p.write_text(
        "﻿Content,Video title,Video publish time,Impressions,Impressions click-through rate (%),Views\n"
        "Total,,,\"120,000\",4.5,9000\n"
        "AAAAAAAAAAA,One,\"Jan 1, 2026\",\"100,000\",5.2,8000\n"
        "BBBBBBBBBBB,Two,\"Jan 8, 2026\",20000,1.0,1000\n"
    )
    got = reach.read_studio_csv(p)
    assert got == {"AAAAAAAAAAA": {"impressions": 100000, "ctr": pytest.approx(0.052)},
                   "BBBBBBBBBBB": {"impressions": 20000, "ctr": pytest.approx(0.01)}}
    bad = tmp_path / "bad.csv"
    bad.write_text("Content,Views\nAAAAAAAAAAA,3\n")
    with pytest.raises(ValueError, match="Advanced mode"):
        reach.read_studio_csv(bad)


def _reach_file(d: Path, day: str, created: str, rows: list[tuple]):
    lines = ["date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr"]
    lines += [f"{day.replace('-', '')},UCx,{vid},{n},{ctr}" for vid, n, ctr in rows]
    (d / f"{day}_{created}.csv").write_text("\n".join(lines) + "\n")


def test_reach_aggregation_weights_and_dedupes(tmp_path):
    _reach_file(tmp_path, "2026-10-01", "2026-10-02T000000Z", [("AAAAAAAAAAA", 1000, 0.10)])
    # day regenerated later with different numbers: only the newest copy counts
    _reach_file(tmp_path, "2026-10-01", "2026-10-05T000000Z", [("AAAAAAAAAAA", 1000, 0.04)])
    _reach_file(tmp_path, "2026-10-02", "2026-10-03T000000Z", [("AAAAAAAAAAA", 3000, 0.08), ("BBBBBBBBBBB", 0, 0)])
    got = reach.aggregate_reports(sorted(tmp_path.glob("*.csv")))
    assert got["AAAAAAAAAAA"]["impressions"] == 4000
    assert got["AAAAAAAAAAA"]["ctr"] == pytest.approx((40 + 240) / 4000)
    assert got["AAAAAAAAAAA"]["days"] == 2
    assert "BBBBBBBBBBB" not in got


def test_reach_percent_scale(tmp_path):
    _reach_file(tmp_path, "2026-10-01", "x", [("AAAAAAAAAAA", 1000, 5.0), ("BBBBBBBBBBB", 1000, 2.5)])
    got = reach.aggregate_reports(list(tmp_path.glob("*.csv")))
    assert got["AAAAAAAAAAA"]["ctr"] == pytest.approx(0.05)


def test_reach_apply_prefers_studio():
    a, b = ChannelVideo("AAAAAAAAAAA", "a", _days_ago(30)), ChannelVideo("BBBBBBBBBBB", "b", _days_ago(30))
    reach.apply([a, b], {"AAAAAAAAAAA": {"impressions": 10, "ctr": 0.1}},
                {"AAAAAAAAAAA": {"impressions": 5, "ctr": 0.2, "days": 3}, "BBBBBBBBBBB": {"impressions": 5, "ctr": 0.2, "days": 3}})
    assert (a.ctr, a.ctr_source) == (0.1, "studio")
    assert (b.ctr, b.ctr_source) == (0.2, "reporting api (3d)")


def test_spearman_and_ranks():
    assert stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert stats.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert stats._ranks([5, 1, 5]) == [1.5, 0, 1.5]
    assert stats.spearman([1, 1, 1], [1, 2, 3]) is None


def test_relative_scores_control_for_growth():
    # A channel that doubles every video: steady growth means every video scores ~1x vs neighbours
    # except the one that broke out.
    vids = [ChannelVideo(f"v{i:010d}", "t", _days_ago(400 - 30 * i), views=100 * 2 ** i) for i in range(10)]
    vids[5].views *= 10
    s = stats.relative_scores(vids, lambda v: v.views)
    assert s[vids[5].id] > 3 * max(s[v.id] for v in vids if v is not vids[5]) / 2


def test_feature_rows_include_thumbnail_tags():
    v = ChannelVideo("AAAAAAAAAAA", "Why spies never talk?", _days_ago(30), duration=3600,
                     thumbnail_tags={"faces": 2, "eye_contact": True, "title_style": "question", "summary": "x"})
    row = stats.feature_rows([v])["AAAAAAAAAAA"]
    assert row["faces"] == 2 and row["eye_contact"] is True and row["title_style"] == "question"
    assert "summary" not in row and row["is_question"] and row["duration_min"] == 60


def test_lift_bool_num_cat():
    rows = {f"v{i}": {"has_number": i % 2 == 0, "faces": i, "title_style": "question" if i < 4 else "bold claim"} for i in range(8)}
    targets = {f"v{i}": {"ctr": 0.02 + 0.01 * (i % 2 == 0) + 0.001 * i} for i in range(8)}
    out = {r["feature"]: r for r in stats.lift(rows, targets, "ctr")}
    assert out["has_number"]["with"] > out["has_number"]["without"]
    assert out["faces"]["kind"] == "num" and out["faces"]["n"] == 8
    assert {c["value"] for c in out["title_style"]["categories"]} == {"question", "bold claim"}


# ---------------------------------------------------------------- whole run against fake Google + Claude

class Req:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result() if callable(self.result) else self.result


VIDEO_IDS = [f"vid{i:08d}" for i in range(8)] + ["short000001"]


def _video_item(i: int, vid: str) -> dict:
    short = vid.startswith("short")
    return {
        "id": vid,
        "snippet": {
            "title": "Short!" if short else (f"Why {i} spies never talk?" if i % 2 else f"Jane Doe {i}: the negotiator"),
            "publishedAt": _days_ago(20 + 14 * i),
            "thumbnails": {"maxres": {"url": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"}},
            "liveBroadcastContent": "none",
            "channelTitle": "Test Show",
        },
        "contentDetails": {"duration": "PT50S" if short else "PT1H"},
        "statistics": {"viewCount": str(1000 * (i + 1)), "likeCount": "10", "commentCount": "3"},
        "status": {"privacyStatus": "public"},
    }


class FakeYT:
    def channels(self):
        return SimpleNamespace(list=lambda **kw: Req({"items": [{
            "id": "UCtest", "snippet": {"title": "Test Show", "description": "Interviews.", "publishedAt": "2020-01-01T00:00:00Z"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}},
            "statistics": {"subscriberCount": "5000", "viewCount": "100000", "videoCount": "9"},
        }]}))

    def playlistItems(self):
        return SimpleNamespace(list=lambda **kw: Req({"items": [{"contentDetails": {"videoId": v}} for v in VIDEO_IDS]}))

    def videos(self):
        def lst(part, id):
            ids = id.split(",")
            if part == "snippet":  # titles_for: videos on other channels
                return Req({"items": [{"id": v, "snippet": {"title": f"Other {v}", "channelTitle": "Other Show"}} for v in ids]})
            return Req({"items": [_video_item(VIDEO_IDS.index(v), v) for v in ids]})
        return SimpleNamespace(list=lst)

    def commentThreads(self):
        def lst(videoId, **kw):
            return Req({"items": [{"snippet": {"totalReplyCount": 0, "topLevelComment": {"snippet": {
                "textDisplay": "24:05 is where it got good" if n == 0 else f"comment {n}", "likeCount": 20 - n}}}} for n in range(3)]})
        return SimpleNamespace(list=lst)


class FakeYA:
    def __init__(self):
        self.calls = []

    def reports(self):
        return SimpleNamespace(query=self.query)

    def query(self, ids, **kw):
        self.calls.append(kw)
        dims, metrics = kw.get("dimensions", ""), kw["metrics"].split(",")
        filters = kw.get("filters", "")
        if dims == "video":
            vids = filters.split("==")[1].split(",")
            rows = [[v, 500, 3000.0, 1200, 35.0 + i, 10, 2, 1, 20 + i, 1] for i, v in enumerate(vids)]
        elif dims == "elapsedVideoTimeRatio":
            if "audienceType" in filters:
                return Req({"columnHeaders": [], "rows": []})  # exercise the fallback
            rows = [list(p) for p in _curve()]
        elif dims == "insightTrafficSourceType":
            rows = [["RELATED_VIDEO", 300, 100.0], ["YT_SEARCH", 100, 50.0]]
        elif dims == "insightTrafficSourceDetail":
            rows = [["other000001", 40]] if "RELATED_VIDEO" in filters else [["how to negotiate", 25]]
        elif dims == "ageGroup,gender":
            rows = [["age25-34", "male", 40.0], ["age35-44", "female", 20.0]]
        elif dims == "country":
            rows = [["US", 600, 1000.0], ["GB", 100, 200.0]]
        elif dims == "deviceType":
            rows = [["MOBILE", 500, 800.0], ["TV", 200, 900.0]]
        elif dims == "subscribedStatus":
            rows = [["SUBSCRIBED", 300, 500.0, 700], ["UNSUBSCRIBED", 700, 900.0, 300]]
        else:
            raise AssertionError(dims)
        names = (dims.split(",") if dims else []) + metrics
        return Req({"columnHeaders": [{"name": n} for n in names], "rows": rows})


class FakeYR:
    def __init__(self):
        self.created = []

    def jobs(self):
        return SimpleNamespace(
            list=lambda: Req({"jobs": []}),
            create=lambda body: Req(lambda: self.created.append(body) or {"id": "job1"}),
            reports=lambda: SimpleNamespace(list=lambda **kw: Req({"reports": []})),
        )


@pytest.fixture
def fake_google(monkeypatch, tmp_path):
    from PIL import Image

    ya, yr = FakeYA(), FakeYR()
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(auth, "credentials", lambda *a: object())
    monkeypatch.setattr(auth, "services", lambda creds: (FakeYT(), ya, yr))

    def thumb(v, d):
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{v.id}-maxresdefault.jpg"
        Image.new("RGB", (64, 36), (40 * (hash(v.id) % 6), 90, 200)).save(p)
        return p
    monkeypatch.setattr(youtube, "download_thumbnail", thumb)
    segs = [Segment(float(t), f"line at {t}") for t in range(0, 3600, 15)]
    monkeypatch.setattr(transcripts, "fetch_youtube", lambda v, *a: segs)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return SimpleNamespace(ya=ya, yr=yr, tmp=tmp_path)


def _args(tmp_path, *extra):
    out = tmp_path / "out" / "report.md"
    out.parent.mkdir()
    return cli.parse_args(["--cache-dir", str(tmp_path / "cache"), "-o", str(out), *extra]), out


def test_run_numbers_only(fake_google, tmp_path):
    args, out = _args(tmp_path, "--no-llm")
    assert cli.run(args) == 0
    md = out.read_text()
    assert "# Audience & packaging report: Test Show" in md
    assert "Started YouTube's daily reach report" in md  # job created, no files yet
    assert fake_google.yr.created == [{"reportTypeId": reach.REPORT_TYPE, "name": "channelscope reach"}]
    assert "## Biggest drop-offs" in md and "line at" in md  # transcript excerpt shown without Claude
    assert "Short!" not in md
    data = json.loads(out.with_suffix(".json").read_text())
    assert len(data["videos"]) == 8
    v1 = data["videos"][0]
    assert v1["retention"] and v1["traffic"] == {"RELATED_VIDEO": 300, "YT_SEARCH": 100}
    assert v1["thumbnail_pixels"]["brightness"] > 0
    assert (out.parent / "report-thumbnails").is_dir()
    assert out.with_suffix(".csv").read_text().count("\n") == 9
    assert "Top countries" in out.with_name("report-data.md").read_text()
    page = out.with_suffix(".html").read_text()
    assert page.startswith("<!doctype html>") and "<table>" in page and "prefers-color-scheme: dark" in page
    # second run reads per-video analytics from cache
    n = len(fake_google.ya.calls)
    out.unlink()
    assert cli.run(args) == 0
    assert len(fake_google.ya.calls) - n == 1  # only the batched totals query


def test_saved_studio_export_is_used(fake_google, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "studio.csv").write_text(
        "Content,Impressions,Impressions click-through rate (%)\n" + "".join(f"{v},1000,4\n" for v in VIDEO_IDS))
    args, out = _args(tmp_path, "--no-llm", "--no-reach")
    assert cli.run(args) == 0
    data = json.loads(out.with_suffix(".json").read_text())
    assert all(v["ctr"] == pytest.approx(0.04) for v in data["videos"])


def test_run_with_studio_csv_and_limits(fake_google, tmp_path):
    csv_path = tmp_path / "studio.csv"
    csv_path.write_text("Content,Impressions,Impressions click-through rate (%)\n"
                        + "".join(f"{v},{10000 * (i + 1)},{2 + i}\n" for i, v in enumerate(VIDEO_IDS)))
    args, out = _args(tmp_path, "--no-llm", "--studio-csv", str(csv_path), "-n", "6", "--no-reach", "--shorts")
    assert cli.run(args) == 0
    data = json.loads(out.with_suffix(".json").read_text())
    assert len(data["videos"]) == 6
    assert all(v["ctr_source"] == "studio" for v in data["videos"])
    md = out.read_text()
    assert "Best CTR vs. neighbouring uploads" in md and "<img src=\"report-thumbnails/" in md
    assert "**CTR vs. neighbours**" in md


STRATEGY = {
    "audience_model": {
        "summary": "Curious professionals.", "who": "25-44, mostly US.", "what_they_come_for": ["tactics"],
        "why_they_click": ["a named stake"], "why_they_stay": ["stories"], "why_they_leave": ["long setups (V2)"],
        "words_they_use": ["negotiate"], "what_else_they_watch": ["Other Show"],
    },
    "titles": {"do": [{"rule": "Lead with the stake", "evidence": "V1 2x", "confidence": "medium"}], "avoid": [], "templates": ["Why X never Y"]},
    "thumbnails": {"do": [], "avoid": [{"rule": "No text walls", "evidence": "V3", "confidence": "low"}], "recipe": "Guest face, 3 words."},
    "interviewing": {"do_more": [{"rule": "Ask for the story", "evidence": "spikes in V1, V4", "confidence": "high"}],
                     "do_less": [], "openings": "Cold open on the best quote.", "topics_that_hold": ["fear"], "topics_that_lose": ["bio"]},
    "content_opportunities": [{"idea": "Hostage talks", "why": "search demand"}],
    "experiments": [{"hypothesis": "Faces beat text", "how": "Test & Compare", "metric": "watch time share"}],
    "next_episode": {"titles": [{"title": "The FBI's Quiet Trick", "why": "stake + secret"}],
                     "thumbnails": [{"concept": "Close-up", "text_overlay": "SHH", "why": "curiosity"}],
                     "opening": "Start with the bank story.", "angles": ["silence as a weapon"]},
    "caveats": ["8 videos is a small sample."],
}

TAGS = {
    "faces": 2, "host_visible": True, "guest_visible": True, "eye_contact": True, "dominant_emotion": "serious/intense",
    "text_overlay": "NEVER TALK", "text_words": 2, "text_vs_title": "complements", "graphic_elements": False,
    "background": "studio/set", "colours": "black, red", "readability": 4, "clutter": 2, "curiosity": "Why not?",
    "title_style": "question", "guest_named_in_title": False, "guest_name": "", "title_promise": "a secret", "summary": "Two faces.",
}


class FakeClaude:
    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        system, content = kw["system"], kw["messages"][0]["content"]
        if system == vision.SYSTEM:
            assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
            payload = TAGS
        elif system == moments.SYSTEM:
            ids = [line.split()[1] for line in content.splitlines() if line.startswith("- d") or line.startswith("- s")]
            assert "- 24:00 (" in content  # timestamp cited in a comment, bucketed to 30s
            payload = {
                "opening": {"what_happens": "Cold open.", "assessment": "Held well."},
                "events": [{"id": i, "what_was_happening": "Long sponsor read", "category": "sponsor/ad read", "likely_reason": "ad"} for i in ids],
                "cited_moments": [{"timestamp": "24:05", "what": "the bank story"}],
                "comments": {"liked": ["the bank story"], "disliked": [], "requests": ["more spies"], "wished_asked": [], "about_host": []},
                "interview_technique": [{"timestamp": "12:00", "observation": "Let silence work", "effect": "helped"}],
            }
        else:
            assert system == strategy.SYSTEM
            assert "## V1 " in content and "Drop-offs by cause" in content and "The creator's next episode: Jane Doe" in content
            payload = STRATEGY
        msg = SimpleNamespace(stop_reason="end_turn", stop_details=None, content=[SimpleNamespace(type="text", text=json.dumps(payload))])
        return _Stream(msg)


class _Stream:
    def __init__(self, msg):
        self.msg = msg
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass
    def get_final_message(self):
        return self.msg


def test_run_with_claude(fake_google, tmp_path, monkeypatch):
    claude = FakeClaude()
    monkeypatch.setattr(cli, "make_client", lambda: claude)
    args, out = _args(tmp_path, "--next", "Jane Doe, ex-FBI negotiator")
    assert cli.run(args) == 0
    md = out.read_text()
    assert "## Your audience" in md and "Curious professionals." in md
    assert "## Next episode: Jane Doe, ex-FBI negotiator" in md and "The FBI's Quiet Trick" in md
    assert "**sponsor/ad read**: Long sponsor read" in md
    assert "Lead with the stake" in md and "_(medium confidence)_" in md
    systems = [c["system"] for c in claude.calls]
    assert systems.count(vision.SYSTEM) == 8 and systems.count(moments.SYSTEM) == 8 and systems[-1] == strategy.SYSTEM
    assert claude.calls[-1]["model"] == "claude-opus-5-5"
    assert {c["model"] for c in claude.calls[:-1]} == {"claude-sonnet-5-5"}
    data = json.loads(out.with_suffix(".json").read_text())
    assert data["videos"][0]["thumbnail_tags"]["text_overlay"] == "NEVER TALK"

    # cached: a re-run only repeats the final synthesis
    n = len(claude.calls)
    assert cli.run(args) == 0
    assert len(claude.calls) - n == 1


def test_missing_client_secret(monkeypatch, tmp_path):
    with pytest.raises(auth.SetupError, match="YouTube Analytics API"):
        auth.credentials(tmp_path / "nope.json", tmp_path / "token.json", auth.READ_SCOPES)


def test_main_needs_key_unless_no_llm(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert cli.main([]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_bad_saved_studio_export_is_ignored(fake_google, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "studio.csv").write_text("Traffic source,Impressions click-through rate (%)\nSearch,5\n")
    args, out = _args(tmp_path, "--no-llm", "--no-reach")
    assert cli.run(args) == 0
    assert "Ignored the saved Studio export" in out.read_text()


def test_no_channel_forgets_sign_in(fake_google, tmp_path, monkeypatch):
    monkeypatch.setattr(FakeYT, "channels", lambda self: SimpleNamespace(list=lambda **kw: Req({"items": []})))
    token = tmp_path / "config" / "token-default.json"
    token.parent.mkdir(parents=True)
    token.write_text("{}")
    args, _ = _args(tmp_path, "--no-llm")
    monkeypatch.setattr(cli, "parse_args", lambda argv=None: args)
    assert cli.main([]) == 2
    assert not token.exists()


def test_claude_failure_still_writes_report(fake_google, tmp_path, monkeypatch):
    from guestprep.llm import LLMError

    def boom(*a, **kw):
        raise LLMError("credit balance is too low")
    monkeypatch.setattr(cli, "make_client", lambda: object())
    monkeypatch.setattr(cli, "analyse_with_claude", boom)
    args, out = _args(tmp_path)
    assert cli.run(args) == 0
    md = out.read_text()
    assert "Claude's analysis didn't run" in md and "settings/billing" in md and "## Every video" in md


def test_report_escapes_html():
    from channelscope import report

    assert report._md("Me <vs> You | <img src=x onerror=alert(1)>") == "Me &lt;vs&gt; You \\| &lt;img src=x onerror=alert(1)&gt;"
    page = report.render_html("# " + report._md("<script>alert(1)</script>"), "<b>x</b>")
    assert "<script>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
