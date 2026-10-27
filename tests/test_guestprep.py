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
        self.creates = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream, create=self._create))

    def _create(self, **kw):
        self.creates.append(kw)
        assert kw["tools"][-1]["name"] == "submit_guide"
        block = SimpleNamespace(type="tool_use", name="submit_guide", input=GUIDE)
        return SimpleNamespace(content=[block], stop_reason="tool_use")

    def _stream(self, **kw):
        self.calls.append(kw)
        user_ = kw["messages"][0]["content"]
        # transcripts are read by the fast model; grouping/suggestions use the main one
        assert kw["model"] in ("claude-sonnet-5-5", "claude-opus-5-5")
        assert kw["extra_body"] == {"fallbacks": "default"}
        assert kw["output_config"]["format"]["type"] == "json_schema"
        user = kw["messages"][0]["content"]
        if "<transcript>" in user:
            title = user.split("Episode title: ")[1].split("\n")[0]
            return FakeStream(self.extractions[title])
        if "Already asked" in user:
            return FakeStream(SUGGESTIONS)
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


GUIDE = {
    "summary": "Jane Ortiz is a nurse turned money writer.",
    "background": "Para one.\n\nPara two.",
    "timeline": [{"when": "2015", "what": "Started a debt blog"}],
    "ideas": [{
        "name": "Slow money",
        "explanation": "Getting rich slowly.",
        "quotes": [
            {"quote": "I was a hospital nurse and I started a blog about my debt.", "source_title": "Ep 1", "source_url": "tests/fixtures/ep1.txt"},
            {"quote": "Money is a feeling, not a number.", "source_title": "Made up", "source_url": "about:blank"},
        ],
    }],
    "reading_list": [{"title": "Slow Money", "url": "https://example.com/book", "kind": "book", "background_needed": "none", "why": "Her main book."}],
    "criticisms": [{"point": "Too cautious.", "source_url": "https://example.com/crit"}],
}

SUGGESTIONS = {
    "fresh_questions": [{"question": "What did your father's bankruptcies teach you?", "why": "Raised, never explored.", "builds_on": "Her father went bankrupt twice"}],
    "no_context_prompts": [{"prompt": "You get $1M but can never check your balance. Take it?", "technique": "forced choice", "why": "Tests her looking-rich idea."}],
}

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
    assert len(fake.calls) == 5
    # transcripts are read by the fast model; grouping and suggestions use the main one
    for kw in fake.calls:
        reading = "<transcript>" in kw["messages"][0]["content"]
        assert kw["model"] == ("claude-sonnet-5-5" if reading else "claude-opus-5-5")
    assert fake.creates[0]["model"] == "claude-opus-5-5"
    fresh = md.split("## Fresh questions")[1].split("##")[0]
    assert "father's bankruptcies" in fresh and "Builds on: Her father" in fresh
    assert "forced choice" in md.split("## No-context prompts")[1]
    assert md.index("## Worn out") < md.index("## Fresh questions") < md.index("## Asked more than once")
    assert data["suggestions"]["fresh_questions"][0]["builds_on"]

    # guide: rendered before the questions, real quote verified, invented one flagged
    assert md.index("## Who they are") < md.index("## Their ideas") < md.index("# Questions") < md.index("## Worn out")
    assert "Para two." in md and "**2015**: Started a debt blog" in md
    assert "1. **[Slow Money](https://example.com/book)** _(book)_" in md
    ideas = md.split("## Their ideas")[1].split("## What to read")[0]
    real, fake_q = ideas.split("> \"I was a hospital nurse")[1], ideas.split("> \"Money is a feeling")[1]
    assert "couldn't find" not in real.split("> \"Money")[0]
    assert "couldn't find this exact wording" in fake_q
    assert len(fake.creates) == 1

    # second run is served from the caches: only clustering + suggestions repeat
    fake.calls.clear()
    cli.main(["Jane Ortiz", "--no-search", "--transcripts", str(FIXTURES), "-o", str(out), "--cache-dir", str(tmp_path / "cache")])
    assert len(fake.calls) == 2 and len(fake.creates) == 1


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


# ---------------------------------------------------------------- --web

def _resp(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def test_web_discover_resumes_pause_turn_and_caches(tmp_path):
    from guestprep import web

    report = SimpleNamespace(type="tool_use", name="report_appearances", input={"appearances": [
        {"url": "https://www.youtube.com/watch?v=AAAAAAAAAAA", "title": "Show", "outlet": "S", "kind": "youtube"},
        {"url": "https://pod.example/ep1", "title": "Ep 1", "outlet": "Pod", "kind": "transcript_page"},
        {"url": "https://audio.example/ep", "title": "Audio", "outlet": "A", "kind": "audio_only"},
        {"url": "not a url", "title": "x", "outlet": "", "kind": "other"},
    ]})
    replies = [_resp([SimpleNamespace(type="server_tool_use")], "pause_turn"), _resp([report], "tool_use")]
    calls = []

    def create(**kw):
        calls.append(kw)
        assert kw["tools"][0]["type"] == "web_search_20260209"
        assert kw["extra_body"] == {"fallbacks": "default"}
        return replies.pop(0)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    found = web.discover(client, "claude-opus-5-5", "Jane Ortiz", "author", tmp_path)
    assert len(found) == 3 and len(calls) == 2
    assert calls[1]["messages"][-1]["role"] == "assistant"  # paused turn echoed back, no extra user message
    yt, pages, audio = web.split_appearances(found)
    assert yt == ["https://www.youtube.com/watch?v=AAAAAAAAAAA"]
    assert [p["url"] for p in pages] == ["https://pod.example/ep1"]
    assert [a["url"] for a in audio] == ["https://audio.example/ep"]
    # cached: no further API calls
    assert web.discover(client, "claude-opus-5-5", "Jane Ortiz", "author", tmp_path) == found


def test_web_discover_salvages_urls_from_prose(tmp_path):
    from guestprep import web

    text = SimpleNamespace(type="text", text="I found https://pod.example/a and (https://pod.example/b).")
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: _resp([text]))))
    found = web.discover(client, "m", "X", "", tmp_path)
    assert [f["url"] for f in found] == ["https://pod.example/a", "https://pod.example/b"]


def test_html_to_text_strips_chrome():
    from guestprep import web

    html = "<html><head><title> Ep 1 | Pod </title><style>x{}</style></head><body><nav>Home About</nav>" \
           "<p>HOST: How did you start?</p><p>GUEST: By accident.</p><script>evil()</script><footer>(c)</footer></body></html>"
    title, text = web.html_to_text(html)
    assert title == "Ep 1 | Pod"
    assert text == "HOST: How did you start?\nGUEST: By accident."


def test_fetch_page_builds_episode(monkeypatch, tmp_path):
    import requests
    from guestprep import web

    body = "<p>" + "Q: How did you start? A: It was an accident. " * 60 + "</p>"
    resp = SimpleNamespace(text=body, headers={"content-type": "text/html"}, raise_for_status=lambda: None)
    monkeypatch.setattr(requests, "get", lambda url, **kw: resp)
    video, segs = web.fetch_page({"url": "https://www.pod.example/ep1", "title": "Ep 1", "outlet": ""}, tmp_path)
    assert video.id.startswith("web:") and video.link(75) == "https://www.pod.example/ep1"
    assert video.channel == "pod.example"
    assert segs[0].start is None
    # too-short pages are rejected
    resp.text = "<p>Listen on Spotify</p>"
    assert web.fetch_page({"url": "https://pod.example/short", "title": "", "outlet": ""}, tmp_path) is None


def test_web_requires_claude(capsys):
    assert cli.main(["X", "--web", "--no-llm"]) == 2


# ---------------------------------------------------------------- --topic

def test_topic_filters_report():
    v1 = Video(id="AAAAAAAAAAA", title="One", channel="S")
    v2 = Video(id="BBBBBBBBBBB", title="Two", channel="S")
    q = lambda e, g: Question(e, g, g)
    eps = [Episode(v1), Episode(v2)]
    on = Cluster("Self", "Who are you, really?", [q("AAAAAAAAAAA", "a"), q("BBBBBBBBBBB", "a")], on_topic=True)
    off = Cluster("Growth", "How did you grow your newsletter?", [q("AAAAAAAAAAA", "b"), q("BBBBBBBBBBB", "b")], on_topic=False)
    md = report.render_markdown("X", eps, [on, off], worn_min=2, topic="being a person")
    assert md.startswith("# Guest prep: X on being a person")
    worn = md.split("## Worn out")[1].split("<details>")[0].split("##")[0]
    assert "Who are you, really?" in worn and "newsletter" not in worn
    assert "Asked, but not about being a person (1)" in md and "How did you grow your newsletter?" in md
    # without a topic nothing is hidden
    md2 = report.render_markdown("X", eps, [on, off], worn_min=2)
    assert "newsletter" in md2.split("## Worn out")[1].split("##")[0]


def test_topic_reaches_cluster_and_suggest_prompts():
    seen = []

    def stream(**kw):
        seen.append(kw["messages"][0]["content"])
        if "Already asked" in kw["messages"][0]["content"]:
            return FakeStream({"fresh_questions": [], "no_context_prompts": []})
        return FakeStream({"clusters": [{"label": "L", "canonical_question": "C", "ids": ["e1q1"], "on_topic": False}]})

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    ep = Episode(Video(id="AAAAAAAAAAA", title="t"), questions=[Question("AAAAAAAAAAA", "q", "g")])
    clusters = cluster.cluster_with_claude(client, "m", "X", [ep], topic="being a person")
    assert clusters[0].on_topic is False
    assert "focus on: being a person" in seen[0]
    from guestprep import suggest
    suggest.suggest(client, "m", "X", "", [ep], clusters, topic="being a person")
    assert "focus on this topic: being a person" in seen[1]



# ---------------------------------------------------------------- guide

def test_quote_matching():
    from guestprep.guide import _norm, quote_in

    text = _norm("So honestly, I don\u2019t think nothing matters. Which means everything does, right? Yeah.")
    assert quote_in("I don't think nothing matters.", text)
    assert quote_in("i dont think nothing matters ... which means everything does", text)
    assert not quote_in("I think nothing matters, which means everything does", text)  # reworded
    assert not quote_in("Yeah.", text)  # too short to check


def test_extraction_keeps_quotes(tmp_path):
    payload = dict(EXTRACTIONS["ep1"], notable_quotes=[{"timestamp": "1:10", "quote": "It was an accident.", "about": "origin"}])
    fake = FakeClient({"ep1": payload})
    ep = _local_episodes()[0]
    extract.extract_with_claude(fake, "claude-opus-5-5", "Jane Ortiz", ep, tmp_path)
    assert ep.quotes == [{"quote": "It was an accident.", "about": "origin", "seconds": 70.0}]


def test_fast_uses_reader_model_everywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient(EXTRACTIONS)
    monkeypatch.setattr(cli, "make_client", lambda: fake)
    rc = cli.main(["Jane Ortiz", "--fast", "--no-search", "--transcripts", str(FIXTURES),
                   "-o", str(tmp_path / "p.md"), "--cache-dir", str(tmp_path / "c")])
    assert rc == 0
    assert {kw["model"] for kw in fake.calls + fake.creates} == {"claude-sonnet-5-5"}


# ---------------------------------------------------------------- --writing

def test_pick_posts_keeps_newest_and_most_liked():
    from guestprep.writing import pick_posts

    posts = [{"url": f"u{i}", "date": f"2026-01-{30 - i:02d}", "likes": l} for i, l in enumerate([1, 2, 3, 4, 5, 99, 0, 50])]
    picked = pick_posts(posts, 7)
    urls = [p["url"] for p in picked]
    assert {"u0", "u1", "u2", "u3", "u4"} <= set(urls) and "u5" in urls and "u7" in urls
    assert urls == sorted(urls, key=lambda u: int(u[1:]))  # newest first


def test_writing_only_run(tmp_path, monkeypatch):
    """No interviews found, but the guest's Substack still yields a guide and questions."""
    import requests
    from guestprep import writing as writing_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    archive = [{"title": "Slow money", "subtitle": "why", "canonical_url": "https://jane.substack.com/p/slow", "post_date": "2026-01-01T00:00:00Z", "reaction_count": 9, "audience": "everyone", "type": "newsletter"}]
    body = "<p>I was a hospital nurse and I started a blog about my debt. " + "More words here. " * 200 + "</p>"

    def get(url, params=None, **kw):
        if url.endswith("/api/v1/archive"):
            return SimpleNamespace(status_code=200, json=lambda: archive if params["offset"] == 0 else [])
        return SimpleNamespace(text=body, headers={"content-type": "text/html"}, raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "get", get)
    fake = FakeClient({})
    monkeypatch.setattr(cli, "make_client", lambda: fake)
    out = tmp_path / "p.md"
    rc = cli.main(["Jane Ortiz", "--no-search", "--writing", "https://jane.substack.com", "-o", str(out), "--cache-dir", str(tmp_path / "c")])
    assert rc == 0
    guide_prompt = fake.creates[0]["messages"][0]["content"]
    assert "The guest's own writing" in guide_prompt and "url: https://jane.substack.com/p/slow" in guide_prompt
    md = out.read_text()
    # the GUIDE fixture's real quote is found in the Substack post, so it's verified
    assert "## Fresh questions" in md and "Slow money" in [l for l in fake.calls if "Already asked" in l["messages"][0]["content"]][0]["messages"][0]["content"]
    assert md.split('> "I was a hospital nurse')[1].split("\n\n")[0].count("couldn't find") == 0
