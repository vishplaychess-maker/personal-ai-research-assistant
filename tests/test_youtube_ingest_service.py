"""YouTube Knowledge Ingestion — Phase 1 (MVP, backend-only).

All tests are MOCKED: no network, no model, no youtube-transcript-api. The
transcript fetcher is injected at the service seam
(``ingest_message_youtube(fetch_transcript=...)``) and, for the wiring tests, at
the single real seam the durable path uses
(``youtube_transcript_context._default_fetch``). The extraction model call is
injected as ``generate_json`` so no provider is ever contacted.

Invariants pinned here:
  1. flag OFF -> nothing happens (None / zero rows / nothing injected)
  2. fail-closed -> zero saves + bounded notice for every unavailable transcript
  3. no transcript CONTENT in any log line (caplog)
  4. config limits honoured (max_chars, chunk size, max facts per video)
  5. user-initiated ingest writes directly and creates NO approval
  8. dependency present and pinned
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import Memory, PendingApproval, ResearchSession
from app.run_engine import chat_stream
from app.run_engine import messages_stream as ms
from app.services import youtube_ingest_service as yt
from app.tools.youtube_transcript_context import STATUS_FETCH_FAILED, STATUS_NO_TRANSCRIPT
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_session

_HMAC = "youtube-ingest-unit-test-hmac-key-0123456789"

VID = "dQw4w9WgXcQ"
URL = "https://www.youtube.com/watch?v=%s" % VID
URL_SHORT = "https://youtu.be/%s" % VID

#: A distinctive canary that must NEVER appear in a log record.
CANARY = "the ZEBRA-MIGRATION crosses the river at dawn"
SECOND_FACT = "Rainfall in the basin averages four hundred millimetres"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    # Every durable flag the submission path needs, so the WIRING tests exercise
    # the real path rather than failing on a gate.
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 3)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_sse_poll_interval_s", 1)
    monkeypatch.setattr(settings, "run_sse_heartbeat_s", 0)
    monkeypatch.setattr(settings, "run_sse_max_seconds", 30)
    # The flag under test starts OFF in every test; individual tests turn it on.
    monkeypatch.setattr(settings, "youtube_ingest_enabled", False)
    ms._active_slots.clear()
    from app.services.rate_limiter import reset_rate_limiter

    reset_rate_limiter()
    yield
    ms._active_slots.clear()


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


@pytest.fixture
def db(eng):
    s = mk_session(eng)
    ensure_user(s, 7)
    s.add(ResearchSession(id=1, title="t", user_id=7))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _segments(*texts, start=0.0):
    return [{"text": t, "start": start + i} for i, t in enumerate(texts)]


def _fact_json(*facts):
    return json.dumps({"facts": [{"fact": f, "quote": "q"} for f in facts]})


def _patch_no_retrieval(monkeypatch):
    import app.services.memory_service as memsvc
    import app.services.rag_service as rag

    monkeypatch.setattr(rag, "retrieve_chunks", lambda session_id, question, db: [])
    monkeypatch.setattr(memsvc, "get_memory_enabled", lambda db: False)


def _patch_transcript_fetch(monkeypatch, *, segments=None, error=None, calls=None):
    """Fake the ONE real fetch seam used by the durable path, and make a call
    into the older second implementation fatal."""
    import importlib

    import app.tools.youtube_transcript_context as ytc

    def _fake(video_id):
        if calls is not None:
            calls.append(video_id)
        if error is not None:
            raise error
        return _segments(CANARY) if segments is None else segments

    monkeypatch.setattr(ytc, "_default_fetch", _fake)
    ys = importlib.import_module("app.tools.youtube_summarizer")

    def _bomb(url):
        raise AssertionError("second fetch implementation used: " + str(url))

    monkeypatch.setattr(ys, "fetch_transcript", _bomb)
    return ytc


def _memories(db) -> list:
    return db.query(Memory).order_by(Memory.id.asc()).all()


# ── URL detection ─────────────────────────────────────────────────────


@pytest.mark.parametrize("message", [
    "https://www.youtube.com/watch?v=%s" % VID,
    "https://youtu.be/%s" % VID,
    "https://www.youtube.com/shorts/%s" % VID,
    "https://www.youtube.com/embed/%s" % VID,
    "https://www.youtube.com/live/%s" % VID,
    "https://www.youtube.com/watch?v=%s&t=95s" % VID,
    "https://www.youtube.com/watch?v=%s&list=PL123&index=4" % VID,
    "https://m.youtube.com/watch?v=%s" % VID,
    "look at this %s please" % URL,
    "(%s)" % URL,
])
def test_supported_url_variants_are_detected(message):
    detected = yt.detect_youtube_url(message)
    assert detected is not None, message
    assert detected[1] == VID


@pytest.mark.parametrize("message", [
    "",
    "no link here at all",
    "https://example.com/watch?v=%s" % VID,
    "https://vimeo.com/123456789",
    "https://www.youtube.com/",                       # no video id
    "https://www.youtube.com/watch?v=tooshort",       # not 11 chars
    "https://notyoutube.com/watch?v=%s" % VID,
])
def test_non_youtube_or_idless_messages_are_ignored(message):
    assert yt.detect_youtube_url(message) is None


# ── flag OFF ──────────────────────────────────────────────────────────


def test_flag_off_ignores_the_link_completely(db, monkeypatch):
    """INVARIANT 1: flag OFF -> no fetch at all, no rows, no result."""
    calls: list = []
    _patch_transcript_fetch(monkeypatch, calls=calls)
    monkeypatch.setattr(settings, "youtube_ingest_enabled", False)

    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message="watch %s" % URL,
        generate_json=lambda *a, **k: pytest.fail("model called with the flag off"),
    )
    assert result is None
    assert calls == [], "the flag was off, so nothing may be fetched"
    assert _memories(db) == []
    assert db.query(PendingApproval).count() == 0


def test_flag_off_submission_injects_nothing_and_writes_nothing(eng, db, monkeypatch):
    """INVARIANT 1, at the real durable seam: with the flag OFF the ingest is a
    complete no-op — zero memory rows and NOTHING injected. (The pre-existing
    transcript GROUNDING is untouched by this feature and still does its own
    fetch; only the ingest notice/rows must be absent.)"""
    _patch_no_retrieval(monkeypatch)
    _patch_transcript_fetch(monkeypatch)
    monkeypatch.setattr(settings, "youtube_ingest_enabled", False)

    ctx = make_ctx(db, user_id=7)
    run, _created = ms.resolve_and_submit(
        db, ctx, session_id=1, message="please summarise %s" % URL,
        idempotency_key=uuid.uuid4().hex)
    assert db.query(Memory).count() == 0
    assert db.query(PendingApproval).count() == 0
    ctx_snapshot = chat_stream.read_context_snapshot(db, run, _first_step(db, run.id))
    assert ctx_snapshot.get("youtube_ingest_notice") is None


def test_flag_off_never_calls_the_model_or_the_ingest_service(eng, db, monkeypatch):
    """INVARIANT 1, the stronger form: no extraction call is ever attempted."""
    _patch_no_retrieval(monkeypatch)
    _patch_transcript_fetch(monkeypatch)
    monkeypatch.setattr(settings, "youtube_ingest_enabled", False)
    monkeypatch.setattr(yt, "_default_generate_json",
                        lambda *a, **k: pytest.fail("model called with the flag off"))

    ctx = make_ctx(db, user_id=7)
    ms.resolve_and_submit(db, ctx, session_id=1, message="summarise %s" % URL,
                          idempotency_key=uuid.uuid4().hex)
    assert db.query(Memory).count() == 0


# ── successful ingest ─────────────────────────────────────────────────


def test_successful_ingest_saves_facts_with_source_metadata(db, monkeypatch):
    """INVARIANT 5: a user-pasted link writes DIRECTLY - no approval created."""
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)

    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message="what about %s" % URL,
        generate_json=lambda messages, system_prompt: _fact_json(CANARY, SECOND_FACT),
        fetch_metadata=lambda url: {"title": "River Documentary", "channel": "Geo Chan",
                                    "language": "en"},
    )
    assert result is not None
    assert result.status == yt.STATUS_SAVED
    assert result.facts_saved == 2
    assert result.facts_found == 2
    assert result.video_id == VID

    rows = _memories(db)
    assert len(rows) == 2
    for row in rows:
        assert row.source_url == URL
        assert row.source_kind == yt.SOURCE_KIND_YOUTUBE
        assert row.source_title == "River Documentary"
        assert row.category == "fact"
        assert row.content_hash and len(row.content_hash) == 64
        meta = json.loads(row.source_meta)
        assert meta["channel"] == "Geo Chan"
        assert meta["transcript_status"] == "ok"
        assert "fetched_at" in meta

    # The confirmation is bounded and names the title + count.
    assert result.notice == "Saved 2 facts from River Documentary."
    # No approval is ever created by a user-initiated ingest.
    assert db.query(PendingApproval).count() == 0

    # INVARIANT 3: source_meta carries provenance only, never transcript text.
    assert CANARY not in rows[0].source_meta


def test_confirmation_falls_back_to_url_without_a_title(db, monkeypatch):
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)
    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL,
        generate_json=lambda *a, **k: _fact_json(CANARY),
        fetch_metadata=lambda url: {"title": None, "channel": None, "language": None},
    )
    assert result.status == yt.STATUS_SAVED
    assert result.notice == "Saved 1 facts from %s." % URL


def test_oembed_failure_is_not_fatal(db, monkeypatch):
    """Metadata is best effort: a raising lookup degrades to NULL, no failure."""
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)

    def _boom(url):
        raise RuntimeError("oembed exploded")

    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL,
        generate_json=lambda *a, **k: _fact_json(CANARY),
        fetch_metadata=_boom,
    )
    assert result.status == yt.STATUS_SAVED
    assert result.source_title is None
    assert _memories(db)[0].source_title is None


# ── fail-closed ───────────────────────────────────────────────────────


@pytest.mark.parametrize("error", [RuntimeError("private video"),
                                   ValueError("transcripts disabled"),
                                   ConnectionError("network down")])
def test_fetch_error_saves_nothing_and_reports_bounded_reason(db, monkeypatch, error):
    """INVARIANT 2: a raising fetch -> ZERO saves + bounded notice."""
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch, error=error)

    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL,
        generate_json=lambda *a, **k: pytest.fail("must not extract without a transcript"),
    )
    assert result is not None
    assert result.status == yt.STATUS_TRANSCRIPT_UNAVAILABLE
    assert result.transcript_status == STATUS_FETCH_FAILED
    assert result.facts_saved == 0
    assert result.notice == yt.FETCH_FAILED_NOTICE
    assert _memories(db) == []
    # The raw exception message must never leak into the notice.
    assert "private video" not in result.notice
    assert "network down" not in result.notice


def test_missing_transcript_saves_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch, segments=[])

    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL,
        generate_json=lambda *a, **k: pytest.fail("must not extract"),
    )
    assert result.status == yt.STATUS_TRANSCRIPT_UNAVAILABLE
    assert result.transcript_status == STATUS_NO_TRANSCRIPT
    assert result.notice == yt.NO_TRANSCRIPT_NOTICE
    assert _memories(db) == []


def test_extraction_call_failure_saves_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL, generate_json=_boom)
    assert result.status == yt.STATUS_EXTRACTION_FAILED
    assert result.facts_saved == 0
    assert result.notice == yt.EXTRACTION_FAILED_NOTICE
    assert _memories(db) == []


def test_unparseable_model_output_saves_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)
    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL,
        generate_json=lambda *a, **k: "not json at all")
    assert result.status == yt.STATUS_EXTRACTION_FAILED
    assert result.notice == yt.NO_FACTS_NOTICE
    assert _memories(db) == []


def test_sensitive_extracted_fact_is_refused(db, monkeypatch):
    """The shared sensitive-content gate still applies to ingested facts."""
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)
    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL,
        generate_json=lambda *a, **k: _fact_json("The api_key is hunter2", CANARY))
    assert result.facts_saved == 1
    contents = [m.content for m in _memories(db)]
    assert contents == [CANARY]


# ── dedupe ────────────────────────────────────────────────────────────


def test_duplicate_url_skips_reingest_with_a_notice(db, monkeypatch):
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)
    gen = lambda *a, **k: _fact_json(CANARY)  # noqa: E731

    first = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL, generate_json=gen,
        fetch_metadata=lambda url: {"title": "River Documentary", "channel": None,
                                    "language": None})
    assert first.status == yt.STATUS_SAVED
    assert len(_memories(db)) == 1

    second = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message="again " + URL, generate_json=gen,
        fetch_metadata=lambda url: {"title": "River Documentary", "channel": None,
                                    "language": None})
    assert second.status == yt.STATUS_DUPLICATE
    assert second.facts_saved == 0
    assert len(_memories(db)) == 1, "a duplicate source must not add rows"
    assert second.notice == "Already saved knowledge from River Documentary - skipped re-ingesting it."


def test_duplicate_fact_from_a_different_video_is_not_stored_twice(db, monkeypatch):
    """Fact-level dedupe reuses save_markers.save_content_hash."""
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)
    gen = lambda *a, **k: _fact_json(CANARY)  # noqa: E731

    yt.ingest_message_youtube(db, user_id=7, session_id=1, message=URL,
                              generate_json=gen, fetch_metadata=lambda url: {})
    other = "https://youtu.be/ZZZZZZZZZZZ"
    second = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=other, generate_json=gen,
        fetch_metadata=lambda url: {})
    assert len(_memories(db)) == 1
    assert second.facts_skipped_duplicate == 1
    assert second.status == yt.STATUS_DUPLICATE


def test_one_users_ingest_does_not_dedupe_another_users(db, monkeypatch):
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch)
    ensure_user(db, 8)
    gen = lambda *a, **k: _fact_json(CANARY)  # noqa: E731

    yt.ingest_message_youtube(db, user_id=7, session_id=1, message=URL,
                              generate_json=gen, fetch_metadata=lambda url: {})
    yt.ingest_message_youtube(db, user_id=8, session_id=1, message=URL,
                              generate_json=gen, fetch_metadata=lambda url: {})
    assert len(_memories(db)) == 2
    assert {m.user_id for m in _memories(db)} == {7, 8}


# ── limits ────────────────────────────────────────────────────────────


def test_oversize_transcript_is_truncated_with_a_notice(db, monkeypatch):
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    monkeypatch.setattr(settings, "youtube_ingest_max_chars", 50)
    monkeypatch.setattr(settings, "youtube_ingest_chunk_size", 20)
    seen: list = []
    _patch_transcript_fetch(monkeypatch, segments=_segments("A" * 200))

    def _gen(messages, system_prompt):
        seen.append(messages[0]["content"])
        return _fact_json(CANARY)

    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL, generate_json=_gen,
        fetch_metadata=lambda url: {})
    assert result.truncated is True
    assert result.transcript_chars == 50
    assert result.notice.endswith(yt.TRUNCATION_NOTICE)
    assert all(len(chunk) <= 20 for chunk in seen)
    assert sum(len(c) for c in seen) <= 50


def test_max_facts_per_video_is_enforced(db, monkeypatch):
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    monkeypatch.setattr(settings, "youtube_ingest_max_facts_per_video", 3)
    _patch_transcript_fetch(monkeypatch, segments=_segments("body text " * 40))
    counter = {"n": 0}

    def _gen(messages, system_prompt):
        counter["n"] += 1
        return _fact_json(*["fact number %d" % (counter["n"] * 100 + i) for i in range(10)])

    result = yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL, generate_json=_gen,
        fetch_metadata=lambda url: {})
    assert result.facts_saved == 3
    assert len(_memories(db)) == 3


def test_chunk_size_is_honoured_and_empty_text_yields_no_chunks():
    assert yt.chunk_text("", 100) == []
    assert yt.chunk_text("   ", 100) == []
    chunks = yt.chunk_text("word " * 50, 40)
    assert chunks and all(len(c) <= 40 for c in chunks)
    # No content is lost.
    assert "".join(chunks).replace(" ", "") == ("word" * 50)


def test_apply_char_limit():
    assert yt.apply_char_limit("hello", 10) == ("hello", False)
    assert yt.apply_char_limit("hello world", 5) == ("hello", True)


def test_parse_facts_tolerates_shapes_and_drops_junk():
    assert yt.parse_facts(_fact_json("a real fact here"), limit=5)[0].content == "a real fact here"
    assert yt.parse_facts(json.dumps(["bare string fact"]), limit=5)[0].content == "bare string fact"
    assert yt.parse_facts(json.dumps({"facts": []}), limit=5) == []
    assert yt.parse_facts("not json", limit=5) == []
    assert yt.parse_facts(json.dumps({"facts": [{"fact": ""}, {"fact": "x"}, 7, None]}), limit=5) == []
    # Duplicates inside one reply collapse; the limit is respected.
    out = yt.parse_facts(_fact_json("same fact", "same fact", "second fact"), limit=1)
    assert [f.content for f in out] == ["same fact"]


def test_config_defaults_match_the_specified_limits():
    """INVARIANT 4: the documented defaults."""
    assert settings.youtube_ingest_enabled is False
    assert settings.youtube_ingest_max_chars == 120000
    assert settings.youtube_ingest_chunk_size == 3000
    assert settings.youtube_ingest_max_facts_per_video == 20


# ── privacy ───────────────────────────────────────────────────────────


def test_no_transcript_content_or_model_output_reaches_the_logs(db, monkeypatch, caplog):
    """INVARIANT 3: logs carry url / video id / counts / hashes only."""
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    secret_sentence = "the CANARY-TRANSCRIPT-SENTENCE only exists in the transcript"
    secret_fact = "the CANARY-FACT-SENTENCE only exists in the model output"
    _patch_transcript_fetch(monkeypatch, segments=_segments(secret_sentence))

    with caplog.at_level(logging.DEBUG):
        result = yt.ingest_message_youtube(
            db, user_id=7, session_id=1, message=URL,
            generate_json=lambda *a, **k: _fact_json(secret_fact),
            fetch_metadata=lambda url: {"title": "A Title", "channel": "C", "language": "en"},
        )
    assert result.status == yt.STATUS_SAVED
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_sentence not in blob
    assert secret_fact not in blob
    assert "CANARY" not in blob
    # ...but the bounded observability IS present.
    assert VID in blob
    assert "youtube_ingest:" in blob


def test_exception_messages_are_never_logged(db, monkeypatch, caplog):
    """The upstream exception MESSAGE is dropped; only a bounded verdict plus the
    url/video id (both non-secret) is observable."""
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    _patch_transcript_fetch(monkeypatch, error=RuntimeError("LEAKED-UPSTREAM-TEXT"))

    with caplog.at_level(logging.DEBUG):
        yt.ingest_message_youtube(db, user_id=7, session_id=1, message=URL,
                                  generate_json=lambda *a, **k: "{}")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "LEAKED-UPSTREAM-TEXT" not in blob
    assert "status=fetch_failed" in blob
    assert VID in blob


def test_context_line_is_none_only_when_no_ingest_was_attempted():
    """`None` means "nothing to say" — the flag was off or there was no link.

    EVERY attempted-but-unsuccessful ingest must produce a line instead, so a
    pasted link is never a silent no-op."""
    assert yt.context_line(None) is None

    saved = yt.YouTubeIngestResult(status=yt.STATUS_SAVED, facts_saved=2,
                                   notice="Saved 2 facts from X.")
    line = yt.context_line(saved)
    assert line and "Saved 2 facts from X." in line

    duplicate = yt.YouTubeIngestResult(
        status=yt.STATUS_DUPLICATE, notice="Already saved knowledge from X.")
    assert "Already saved knowledge from X." in yt.context_line(duplicate)


@pytest.mark.parametrize("status,notice", [
    (yt.STATUS_TRANSCRIPT_UNAVAILABLE, yt.FETCH_FAILED_NOTICE),
    (yt.STATUS_TRANSCRIPT_UNAVAILABLE, yt.NO_TRANSCRIPT_NOTICE),
    (yt.STATUS_EXTRACTION_FAILED, yt.EXTRACTION_FAILED_NOTICE),
    (yt.STATUS_EXTRACTION_FAILED, yt.NO_FACTS_NOTICE),
    (yt.STATUS_SAVE_FAILED, ""),
])
def test_failed_ingest_yields_the_exact_bounded_notice(status, notice):
    """The UX gap: a failed ingest must NOT be silent. The user-facing notice is
    the ONE fixed template — it never echoes a reason code or upstream text."""
    result = yt.YouTubeIngestResult(status=status, notice=notice, url=URL,
                                    video_id=VID)
    assert result.saved is False
    line = yt.context_line(result)
    assert line is not None, "a failed ingest must not be silent"
    assert ("I could not process the knowledge from that video. "
            "The chat will continue normally.") in line
    # ...and it carries no reason code, transcript text, or exception text.
    for leak in ("fetch_failed", "no_transcript", "extraction_failed", "save_failed",
                 "ProviderRateLimited", "neuron", "784"):
        assert leak not in line


def test_fetch_error_surfaces_the_notice_through_the_durable_path(eng, db, monkeypatch):
    """End to end through the real submission path: a transcript FETCH error must
    land the bounded notice in the sealed context snapshot and the rendered
    envelope — not vanish."""
    _patch_no_retrieval(monkeypatch)
    _patch_transcript_fetch(monkeypatch, error=RuntimeError("private video"))
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)

    ctx = make_ctx(db, user_id=7)
    run, _created = ms.resolve_and_submit(
        db, ctx, session_id=1, message="summarise %s" % URL,
        idempotency_key=uuid.uuid4().hex)

    assert db.query(Memory).count() == 0, "a fetch error must save nothing"
    snapshot = chat_stream.read_context_snapshot(db, run, _first_step(db, run.id))
    notice = snapshot["youtube_ingest_notice"]
    assert notice is not None, "the failure notice must reach the snapshot"
    assert yt.INGEST_FAILED_NOTICE in notice
    assert "private video" not in notice

    envelope = chat_stream.render_context_envelope(snapshot)
    assert yt.INGEST_FAILED_NOTICE in envelope


# ── durable-path wiring ───────────────────────────────────────────────


def _first_step(db, run_id):
    from app.models.models import RunStep

    return (db.query(RunStep).filter(RunStep.run_id == run_id)
            .order_by(RunStep.seq.asc()).first())


def test_enabled_submission_saves_facts_and_injects_the_notice(eng, db, monkeypatch):
    """The full backend flow: pasted link -> facts saved -> bounded context line
    injected for the SAME turn.

    The transcript text and the extracted fact text are deliberately DIFFERENT
    strings: the pre-existing transcript GROUNDING block legitimately contains
    the transcript, so this test proves the INGEST notice carries neither."""
    _patch_no_retrieval(monkeypatch)
    fact_text = "Rainfall in the basin averages four hundred millimetres"
    _patch_transcript_fetch(monkeypatch, segments=_segments("unrelated narration text"))
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    monkeypatch.setattr(yt, "fetch_oembed_metadata",
                        lambda url, **k: {"title": "River Documentary", "channel": "Geo",
                                          "language": "en"})
    monkeypatch.setattr(yt, "_default_generate_json",
                        lambda messages, system_prompt: _fact_json(fact_text))

    ctx = make_ctx(db, user_id=7)
    run, _created = ms.resolve_and_submit(
        db, ctx, session_id=1, message="what is this about %s" % URL,
        idempotency_key=uuid.uuid4().hex)

    rows = _memories(db)
    assert len(rows) == 1
    assert rows[0].content == fact_text
    assert rows[0].source_url == URL

    step = _first_step(db, run.id)
    snapshot = chat_stream.read_context_snapshot(db, run, step)
    notice = snapshot["youtube_ingest_notice"]
    assert "Saved 1 facts from River Documentary." in notice
    assert fact_text not in notice, "the notice must not carry extracted fact text"

    envelope = chat_stream.render_context_envelope(snapshot)
    assert "Saved 1 facts from River Documentary." in envelope
    assert fact_text not in envelope, "the injected line must not carry fact text"


def test_submission_without_a_link_injects_no_ingest_notice(eng, db, monkeypatch):
    _patch_no_retrieval(monkeypatch)
    _patch_transcript_fetch(monkeypatch)
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)

    ctx = make_ctx(db, user_id=7)
    run, _created = ms.resolve_and_submit(
        db, ctx, session_id=1, message="just a normal question",
        idempotency_key=uuid.uuid4().hex)
    step = _first_step(db, run.id)
    snapshot = chat_stream.read_context_snapshot(db, run, step)
    assert snapshot.get("youtube_ingest_notice") is None
    assert db.query(Memory).count() == 0


def test_ingest_failure_never_blocks_the_chat_submission(eng, db, monkeypatch):
    """INVARIANT 2: chat continues normally when the ingest blows up."""
    _patch_no_retrieval(monkeypatch)
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)

    def _explode(*a, **k):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(yt, "ingest_message_youtube", _explode)

    ctx = make_ctx(db, user_id=7)
    run, created = ms.resolve_and_submit(
        db, ctx, session_id=1, message="look %s" % URL,
        idempotency_key=uuid.uuid4().hex)
    assert run is not None and created is True
    assert run.status == "queued"
    assert db.query(Memory).count() == 0


def test_replay_does_not_ingest_twice(eng, db, monkeypatch):
    """The same idempotency key must not re-run the ingest (it is a fresh-
    submission-only side effect)."""
    _patch_no_retrieval(monkeypatch)
    calls: list = []
    _patch_transcript_fetch(monkeypatch, calls=calls)
    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    monkeypatch.setattr(yt, "fetch_oembed_metadata", lambda url, **k: {})
    monkeypatch.setattr(yt, "_default_generate_json",
                        lambda messages, system_prompt: _fact_json(CANARY))

    ctx = make_ctx(db, user_id=7)
    key = uuid.uuid4().hex
    _run_a, created_a = ms.resolve_and_submit(
        db, ctx, session_id=1, message="about %s" % URL, idempotency_key=key)
    ingest_calls_after_first = len(calls)
    _run_b, created_b = ms.resolve_and_submit(
        db, ctx, session_id=1, message="about %s" % URL, idempotency_key=key)

    assert created_a is True and created_b is False
    assert len(calls) == ingest_calls_after_first, "a replay must not re-ingest"
    assert len(_memories(db)) == 1


# ── recall path ───────────────────────────────────────────────────────


def test_ingested_rows_are_reachable_through_the_existing_recall_path(db, monkeypatch):
    """The pre-existing memory retrieval is user-scoped and unfiltered by kind,
    so ingested knowledge is included with no second retrieval implementation."""
    from app.services.memory_service import retrieve_relevant_memories

    monkeypatch.setattr(settings, "youtube_ingest_enabled", True)
    monkeypatch.setattr(settings, "memory_max_results", 10)
    monkeypatch.setattr(settings, "memory_decay_ttl_days", 3650)
    _patch_transcript_fetch(monkeypatch)
    yt.ingest_message_youtube(
        db, user_id=7, session_id=1, message=URL,
        generate_json=lambda *a, **k: _fact_json(CANARY),
        fetch_metadata=lambda url: {"title": "T", "channel": None, "language": None})

    found = retrieve_relevant_memories(db, user_id=7)
    assert [m.content for m in found] == [CANARY]
    assert found[0].source_kind == yt.SOURCE_KIND_YOUTUBE


# ── schema + dependency ───────────────────────────────────────────────


def test_memories_source_columns_are_additive_and_nullable(eng, db):
    """A memory written with no source keeps NULL and behaves as before."""
    from app.services.memory_service import save_memory

    row = save_memory(db, 7, "A plain memory with no source", "preference", 1)
    assert row is not None
    assert row.source_url is None and row.source_kind is None
    assert row.source_title is None and row.source_meta is None
    # ...and it still gets the shared digest, so fact-level dedupe works for
    # every caller of save_memory, not only the YouTube ingest.
    assert row.content_hash == yt.save_content_hash(
        "memory", {"content": "A plain memory with no source", "category": "preference"})
    assert len(row.content_hash) == 64


def test_youtube_transcript_api_is_pinned_in_requirements():
    """INVARIANT 8: the dependency is declared and pinned (no Python lockfile
    exists in this repo - requirements.txt is the only manifest)."""
    from pathlib import Path

    text = Path("backend/requirements.txt").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("youtube-transcript-api")]
    assert lines, "youtube-transcript-api must be declared"
    assert any(ln.startswith("youtube-transcript-api==") for ln in lines), \
        "the dependency must be pinned: %r" % lines
