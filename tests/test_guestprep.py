import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from guestprep import cli, cluster, discover, extract, report, transcripts
from guestprep.models import Cluster, Episode, Question, Segment, Video, format_timestamp, parse_timestamp

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- parsing

def test_timestamps_roundtrip():
    assert parse_timestamp("1:02:03") == 3723
    assert parse_timestamp("[12:34]") == 754
    assert parse_timestamp("") is None
    assert parse_timestamp("abc") is None
    assert format_timestamp(3723) == "1:02:03"
    assert format_timestamp(754) == "12:34"


def test_video_id_from_url():
    vid = "JKoDsRyZUZk"
    for u in (f"https://www.youtube.com/watch?v={vid}&t=30s", f"https://youtu.be/{vid}", f"https://youtube.com/live/{vid}", vid):
        assert discover.video_id_from_url(u) == vid
    assert discover.video_id_from_url("https://example.com") is None


def test_mentions_guest():
    assert discover.mentions_guest("Morgan Housel on wealth", "Morgan Housel")
    assert discover.mentions_guest("so housel what do you think", "Morgan Housel")
    assert not discover.mentions_guest("Morgan Freeman", "Morgan Housel")
    # very short last names alone are too ambiguous
    assert not discover.mentions_guest("the li family", "Jet Li")
    assert discover.mentions_guest("an interview with jet li", "Jet Li")


def test_parse_json3():
    raw = json.dumps({"events": [{"tStartMs": 1500, "segs": [{"utf8": "hello "}, {"utf8": "there"}]}, {"tStartMs": 2000, "segs": [{"utf8": "\n"}]}]})
    assert transcripts.parse_json3(raw) == [Segment(start=1.5, text="hello there")]


def test_parse_vtt_dedupes_rolling_lines():
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello <c>world</c>\n\n00:00:02.000 --> 00:00:03.000\nhello world\nnext line\n"
    segs = transcripts.parse_cues(raw)
    assert [s.text for s in segs] == ["hello world", "next line"]
    assert segs[0].start == 1.0


def test_local_loading_all_formats():
    vids = transcripts.local_videos(FIXTURES)
    assert [v.title for v, _ in vids] == ["ep1", "ep2", "ep3"]
    ep1 = vids[0][1]
    assert ep1[1].start == 40 and ep1[1].text.startswith("So Jane")
    assert vids[2][1][1].start == 30  # srt comma millis


def test_prompt_text_has_timestamps():
    segs = [Segment(0, "a"), Segment(5, "b"), Segment(25, "c")]
    assert transcripts.to_prompt_text(segs) == "[0:00] a b\n[0:25] c"


def test_search_filters_and_dedupes(monkeypatch):
    entries = [
        {"id": "AAAAAAAAAAA", "title": "Jane Ortiz on money", "duration": 3600, "channel": "Show A"},
        {"id": "BBBBBBBBBBB", "title": "Clip", "duration": 60, "channel": "Show A"},
        {"id": "CCCCCCCCCCC", "title": "The Savings Expert!", "duration": 4000, "channel": "Show B"},
        {"id": "DDDDDDDDDDD", "title": "Jane Ortiz FULL", "duration": 3630, "channel": "Show A"},  # re-upload
        {"id": "UCchannelid_xx", "title": "a channel", "channel": "x"},
    ]

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def extract_info(self, q, download): return {"entries": entries}

    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    found = discover.search_youtube("Jane Ortiz", per_query=5, min_minutes=20)
    assert [v.id for v in found] == ["AAAAAAAAAAA", "CCCCCCCCCCC"]
    assert found[0].confirmed and not found[1].confirmed


def test_blocked_detection(monkeypatch, tmp_path):
    def boom(*a):
        raise RuntimeError("Sign in to confirm you're not a bot")
    monkeypatch.setattr(transcripts, "_via_transcript_api", boom)
    monkeypatch.setattr(transcripts, "_via_ytdlp", boom)
    with pytest.raises(transcripts.TranscriptBlocked):
        transcripts.fetch_youtube(Video(id="AAAAAAAAAAA", title="t"), tmp_path)


def test_transcript_cache(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(transcripts, "_via_transcript_api", lambda vid, proxy: calls.append(vid) or [Segment(1.0, "hi")])
    v = Video(id="AAAAAAAAAAA", title="t")
    assert transcripts.fetch_youtube(v, tmp_path) == [Segment(1.0, "hi")]
    assert transcripts.fetch_youtube(v, tmp_path) == [Segment(1.0, "hi")]
    assert calls == ["AAAAAAAAAAA"]


# ---------------------------------------------------------------- heuristics

def _local_episodes():
    return [Episode(video=v, segments=s) for v, s in transcripts.local_videos(FIXTURES)]


def test_heuristic_pipeline():
    eps = _local_episodes()
    for ep in eps:
        extract.extract_heuristic(ep)
    qs = [q.question for q in eps[0].questions]
    assert "So Jane, how did you first get started writing about money?" in qs
    assert not any("thank" in q.lower() for q in qs)
    clusters = cluster.cluster_heuristic(eps)
    top = clusters[0]
    assert top.times_asked == 3
    assert "started" in top.canonical_question


# ---------------------------------------------------------------- Claude paths (stubbed)

class FakeStream:
    def __init__(self, payload, stop_reason="end_turn", content=None):
        self.msg = SimpleNamespace(
            stop_reason=stop_reason,
            stop_details=None,
            content=content if content is not None else [SimpleNamespace(type="text", text=json.dumps(payload))],
        )
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get_final_message(self): return self.msg


class FakeClient:
    """Answers extraction calls from a per-title table and clustering calls by gist."""
    def __init__(self, extractions):
        self.extractions = extractions
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        assert kw["model"] == "claude-opus-5-5"
        assert kw["extra_body"] == {"fallbacks": "default"}
        assert kw["output_config"]["format"]["type"] == "json_schema"
        user = kw["messages"][0]["content"]
        if "<transcript>" in user:
            title = user.split("Episode title: ")[1].split("\n")[0]
            return FakeStream(self.extractions[title])
        # clustering: group ids whose gist text matches
        groups = {}
        for line in user.split("Questions:\n")[1].splitlines():
            qid, gist = line.split(" ", 1)[0], line.split("): ", 1)[1]
            groups.setdefault(gist, []).append(qid)
        # deliberately drop one id and include a bogus one to exercise validation
        clusters = [{"label": g[:20], "canonical_question": g, "ids": ids} for g, ids in groups.items()]
        clusters[-1]["ids"] = clusters[-1]["ids"][:-1] + ["e99q99"]
        return FakeStream({"clusters": clusters})


def _q(ts, gist, topic="origin"):
    return {"timestamp": ts, "question": gist + " (as asked)", "gist": gist, "topic": topic}


EXTRACTIONS = {
    "ep1": {"guest_is_interviewed": True, "questions": [_q("0:40", "How did you get started?"), _q("3:00", "Biggest savings mistake?", "savings")], "unexplored_threads": ["Her father went bankrupt twice"]},
    "ep2": {"guest_is_interviewed": True, "questions": [_q("1:00", "How did you get started?"), _q("2:00", "Biggest savings mistake?", "savings"), _q("3:00", "Crypto?", "crypto")], "unexplored_threads": []},
    "ep3": {"guest_is_interviewed": True, "questions": [_q("0:30", "How did you get started?"), _q("2:10", "Why do smart people decide badly?")], "unexplored_threads": []},
}


def test_claude_pipeline_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient(EXTRACTIONS)
    monkeypatch.setattr(cli, "make_client", lambda: fake)
    out = tmp_path / "prep.md"
    rc = cli.main(["Jane Ortiz", "--no-search", "--transcripts", str(FIXTURES), "-o", str(out), "--cache-dir", str(tmp_path / "cache")])
    assert rc == 0
    md = out.read_text()
    worn = md.split("## Worn out")[1].split("##")[0]
    assert "3/3" in worn and "How did you get started?" in worn
    assert "Biggest savings mistake?" in md.split("## Asked more than once")[1]
    assert "Her father went bankrupt twice" in md
    data = json.loads(out.with_suffix(".json").read_text())
    # every extracted question lands in exactly one cluster despite the bad ids
    assert sum(len(c["members"]) for c in data["clusters"]) == 7
    assert len(fake.calls) == 4

    # second run is served from the extraction cache: only the clustering call repeats
    fake.calls.clear()
    cli.main(["Jane Ortiz", "--no-search", "--transcripts", str(FIXTURES), "-o", str(out), "--cache-dir", str(tmp_path / "cache")])
    assert len(fake.calls) == 1


def test_non_interview_dropped(tmp_path):
    fake = FakeClient({"ep1": {"guest_is_interviewed": False, "questions": [], "unexplored_threads": []}})
    ep = _local_episodes()[0]
    assert extract.extract_with_claude(fake, "claude-opus-5-5", "Jane Ortiz", ep, tmp_path) is False


def test_refusal_and_fallback_text_handling():
    from guestprep.llm import call_json

    def client_with(stream):
        return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: stream)))

    kw = dict(model="claude-opus-5-5", system="s", user="u", schema={})
    assert call_json(client_with(FakeStream(None, stop_reason="refusal", content=[])), **kw) is None
    content = [
        SimpleNamespace(type="text", text='{"partial'),
        SimpleNamespace(type="fallback"),
        SimpleNamespace(type="text", text='{"ok": true}'),
    ]
    assert call_json(client_with(FakeStream(None, content=content)), **kw) == {"ok": True}


def test_report_tiers_and_links():
    v1 = Video(id="AAAAAAAAAAA", title="Ep | One", channel="Show", duration=3600)
    v2 = Video(id="BBBBBBBBBBB", title="Ep Two", channel="Show 2")
    q1 = Question("AAAAAAAAAAA", "q", "g", seconds=75)
    q2 = Question("BBBBBBBBBBB", "q", "g")
    eps = [Episode(v1, questions=[q1]), Episode(v2, questions=[q2])]
    md = report.render_markdown("X", eps, [Cluster("L", "Canon?", [q1, q2])], worn_min=2)
    assert "| 2/2 | **L**: Canon? | [#1 1:15](https://www.youtube.com/watch?v=AAAAAAAAAAA&t=75s), [#2](https://www.youtube.com/watch?v=BBBBBBBBBBB) |" in md
    assert "Ep \\| One" in md


def test_missing_key_is_a_clear_error(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert cli.main(["Jane Ortiz", "--no-search", "--transcripts", str(FIXTURES)]) == 2
    assert "ANTHROPIC_API_KEY isn't set" in capsys.readouterr().err


def test_about_reaches_prompt_and_cache_key(tmp_path):
    fake = FakeClient(EXTRACTIONS)
    ep = _local_episodes()[0]
    extract.extract_with_claude(fake, "claude-opus-5-5", "Jane Ortiz", ep, tmp_path, about="personal finance author")
    assert "Who this guest is: personal finance author" in fake.calls[0]["messages"][0]["content"]
    # a different --about must not reuse the cached answer
    extract.extract_with_claude(fake, "claude-opus-5-5", "Jane Ortiz", ep, tmp_path, about="a different person")
    extract.extract_with_claude(fake, "claude-opus-5-5", "Jane Ortiz", ep, tmp_path, about="personal finance author")
    assert len(fake.calls) == 2


def test_rate_limit_classified_separately(monkeypatch, tmp_path):
    def api(*a):
        raise RuntimeError("IpBlocked: YouTube is blocking requests from your IP. You have done too many requests")
    def ytdlp(*a):
        raise RuntimeError("ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests")
    monkeypatch.setattr(transcripts, "_via_transcript_api", api)
    monkeypatch.setattr(transcripts, "_via_ytdlp", ytdlp)
    with pytest.raises(transcripts.TranscriptRateLimited):
        transcripts.fetch_youtube(Video(id="AAAAAAAAAAA", title="t"), tmp_path)


def test_rate_limit_backoff_then_success(monkeypatch, tmp_path):
    attempts = []
    def fetch(v, cache_dir, proxy, cookies):
        attempts.append(1)
        if len(attempts) < 3:
            raise transcripts.TranscriptRateLimited("429")
        return [Segment(0, "hi")]
    monkeypatch.setattr(transcripts, "fetch_youtube", fetch)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    args = SimpleNamespace(cache_dir=tmp_path, proxy=None, cookies=None)
    assert cli._fetch_with_backoff(Video(id="AAAAAAAAAAA", title="t"), args) == [Segment(0, "hi")]
    assert len(attempts) == 3


def test_ytdlp_opts_cookies():
    assert transcripts.ytdlp_opts(None, "browser:chrome")["cookiesfrombrowser"] == ("chrome",)
    assert transcripts.ytdlp_opts(None, "c.txt")["cookiefile"] == "c.txt"
    assert "cookiefile" not in transcripts.ytdlp_opts(None, None)
