"""Focused tests for the independent YouTube transcript grounding slice."""
from __future__ import annotations

import inspect

import pytest

from app.tools import youtube_transcript_context as ytc

GOOD = ["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ"]


def _boom(_vid):
    raise AssertionError("fetch must not be called for an invalid URL")


def test_every_supported_url_form_extracts_the_id_before_fetching():
    seen = []
    def _fetch(vid):
        seen.append(vid)
        return [{"start": 12.5, "text": "hello world"}]
    for url in GOOD:
        res = ytc.load_transcript(url, fetch=_fetch)
        assert res.status == ytc.STATUS_OK and res.video_id == "dQw4w9WgXcQ"
        assert res.segments[0].text == "hello world"
    assert seen == ["dQw4w9WgXcQ"] * len(GOOD)


def test_a_non_youtube_url_never_reaches_the_fetcher():
    for bad in ("", "https://example.com/watch?v=dQw4w9WgXcQ", "not a url", None):
        res = ytc.load_transcript(bad, fetch=_boom)
        assert res.status == ytc.STATUS_INVALID_URL
        assert res.video_id is None
        assert ytc.build_grounded_context(res) == ytc.INVALID_URL_NOTICE


def test_no_transcript_is_a_clear_status_not_an_empty_string():
    res = ytc.load_transcript(GOOD[0], fetch=lambda _v: [])
    assert res.status == ytc.STATUS_NO_TRANSCRIPT and res.segments == ()
    ctx = ytc.build_grounded_context(res)
    assert "unknown" in ctx and "Do not guess" in ctx


def test_a_fetch_failure_is_bounded_and_never_echoes_the_message():
    canary = "sk-live-CANARY upstream body 429"
    def _bad(_v):
        raise RuntimeError(canary)
    res = ytc.load_transcript(GOOD[0], fetch=_bad)
    assert res.status == ytc.STATUS_FETCH_FAILED
    assert res.reason == "RuntimeError"
    ctx = ytc.build_grounded_context(res)
    assert canary not in ctx and canary not in (res.reason or "")
    assert "429" not in ctx


def test_the_context_is_timestamped_and_bounded():
    def _fetch(_v):
        return [{"start": 0, "text": "intro"}, {"start": 65.2, "text": "sixty five"}]
    res = ytc.load_transcript(GOOD[0], fetch=_fetch)
    ctx = ytc.build_grounded_context(res)
    assert "[00:00] intro" in ctx and "[01:05] sixty five" in ctx
    assert "never state anything" in ctx
    small = ytc.build_grounded_context(res, max_chars=20)
    assert "truncated" in small and len(small) < 400


def test_this_slice_performs_no_memory_or_knowledge_graph_writes():
    src = inspect.getsource(ytc)
    for forbidden in ("memory_service", "knowledge_graph", "KnowledgeGraph",
                      "db.add(", "session.add(", "embedding"):
        assert forbidden not in src, forbidden
