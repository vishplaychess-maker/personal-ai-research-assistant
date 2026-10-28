"""The legacy youtube_summarizer tool must work, through the ONE fetch impl.

B-3: the tool called YouTubeTranscriptApi.get_transcript(video_id) directly, a
classmethod REMOVED in youtube-transcript-api 1.x, so every real URL produced an
AttributeError error string. It now delegates the whole fetch to
app.tools.youtube_transcript_context (validate -> fetch -> classify) and only
renders the tool's free-text contract.

These tests execute the TOOL path (the LangChain tool object, not just the
helper) and prove there is no second, independent fetch implementation.
"""

from __future__ import annotations

import importlib
import sys
import types

GOOD = "https://www.youtube.com/watch?v=abc12345678"
VID = "abc12345678"


def _mods():
    return (importlib.import_module("app.tools.youtube_summarizer"),
            importlib.import_module("app.tools.youtube_transcript_context"))


def _segments():
    return [{"text": "alpha", "start": 0.0}, {"text": "beta", "start": 1.0}]


def test_tool_invoke_returns_the_transcript_text(monkeypatch):
    ys, ytc = _mods()
    calls: list = []

    def _fetch(video_id):
        calls.append(video_id)
        return _segments()

    monkeypatch.setattr(ytc, "_default_fetch", _fetch)
    out = ys.youtube_summarizer.invoke({"url": GOOD})
    assert out == "alpha beta"
    assert calls == [VID]              # validated video id, fetched exactly once


def test_tool_reports_an_unavailable_transcript_clearly(monkeypatch):
    ys, ytc = _mods()
    monkeypatch.setattr(ytc, "_default_fetch", lambda video_id: [])
    out = ys.youtube_summarizer.invoke({"url": GOOD})
    assert out == "[YouTube Summarizer] No transcript available for video " + VID + "."


def test_tool_reports_a_fetch_failure_without_the_upstream_message(monkeypatch):
    ys, ytc = _mods()

    def _boom(video_id):
        raise RuntimeError("CANARY-UPSTREAM-SECRET")

    monkeypatch.setattr(ytc, "_default_fetch", _boom)
    out = ys.youtube_summarizer.invoke({"url": GOOD})
    assert "Error fetching transcript for " + VID in out
    assert "RuntimeError" in out
    assert "CANARY-UPSTREAM-SECRET" not in out     # bounded reason only


def test_tool_rejects_a_non_youtube_url(monkeypatch):
    ys, _ytc = _mods()
    out = ys.youtube_summarizer.invoke({"url": "https://example.com/not-a-video"})
    assert "invalid YouTube URL" in out
    assert "youtube.com or youtu.be" in out


def test_fetch_transcript_reports_a_missing_video_id_without_fetching(monkeypatch):
    ys, ytc = _mods()

    def _never(video_id):
        raise AssertionError("no network call may happen without a video id")

    monkeypatch.setattr(ytc, "_default_fetch", _never)
    out = ys.fetch_transcript("https://www.youtube.com/feed/subscriptions")
    assert out == ("[YouTube Summarizer] Error: could not extract a video ID "
                   "from 'https://www.youtube.com/feed/subscriptions'.")


def test_fetch_transcript_truncates_with_the_existing_marker(monkeypatch):
    ys, ytc = _mods()
    segments = [{"text": "x" * 900, "start": float(i)} for i in range(6)]
    monkeypatch.setattr(ytc, "_default_fetch", lambda video_id: segments)
    out = ys.fetch_transcript(GOOD)
    marker = "[Transcript truncated at 4000 chars]"
    assert out.endswith(marker)
    assert len(out) == 4000 + 2 + len(marker)


def test_fetch_transcript_delegates_to_the_single_implementation(monkeypatch):
    ys, ytc = _mods()
    seen: list = []

    def _spy(url, **kwargs):
        seen.append(url)
        return ytc.TranscriptResult(
            ytc.STATUS_OK, VID, (ytc.TranscriptSegment(0.0, "spy text"),), None)

    monkeypatch.setattr(ytc, "load_transcript", _spy)
    assert ys.fetch_transcript(GOOD) == "spy text"
    assert seen == [GOOD]


def test_tool_never_calls_the_library_directly(monkeypatch):
    """A direct library call would hit this bomb; delegation must not."""
    ys, ytc = _mods()

    class _BombApi:
        def __init__(self, *a, **k):
            raise AssertionError("direct library call from the tool")

        @staticmethod
        def get_transcript(*a, **k):
            raise AssertionError("legacy classmethod called from the tool")

    monkeypatch.setitem(sys.modules, "youtube_transcript_api",
                        types.SimpleNamespace(YouTubeTranscriptApi=_BombApi))
    monkeypatch.setattr(ytc, "_default_fetch", lambda video_id: _segments())
    assert ys.fetch_transcript(GOOD) == "alpha beta"
    assert ys.youtube_summarizer.invoke({"url": GOOD}) == "alpha beta"


def test_snippet_objects_still_render_through_the_tool(monkeypatch):
    """The 1.x API returns snippet OBJECTS; the tool must render them too."""
    ys, ytc = _mods()

    class _Snippet:
        def __init__(self, text, start):
            self.text = text
            self.start = start

    monkeypatch.setattr(ytc, "_default_fetch",
                        lambda video_id: [_Snippet("object shaped", 0.0)])
    assert ys.youtube_summarizer.invoke({"url": GOOD}) == "object shaped"
