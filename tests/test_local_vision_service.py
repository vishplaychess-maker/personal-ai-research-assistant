"""Local Vision (ZERO API cost) — moondream via Ollama, CPU-only.

All tests are MOCKED: no network, no Ollama, no model. The single network seam
(local_vision_service._post_ollama) is monkeypatched, so the tests prove the
validation, fail-closed, privacy and context-injection behaviour without ever
contacting Ollama.

Invariants pinned here:
  1. flag OFF -> bounded response and ZERO Ollama calls
  2. JPEG / PNG / WebP accepted; anything else fails closed
  3. oversize fails closed BEFORE any Ollama call
  4. connection error / timeout / non-200 -> bounded notice, chat still works
  5. image bytes and the description NEVER reach a log line (hash + size only)
  6. success injects the exact untrusted context line into the chat context
  7. defaults are OFF and exactly as specified
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import ResearchSession
from app.services import local_vision_service as lv
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_session

_HMAC = "local-vision-unit-test-hmac-key-0123456789"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
WEBP_MAGIC = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP"


def _png(payload: bytes = b"\x00" * 64) -> bytes:
    return PNG_MAGIC + payload


def _jpeg() -> bytes:
    return JPEG_MAGIC + b"\x00" * 32


def _webp() -> bytes:
    return WEBP_MAGIC + b"\x00" * 32


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    # Durable flags the chat-submission test needs, so it exercises the real
    # seam rather than failing on a gate.
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "enable_deep_research", False)
    # The flag under test starts OFF in every test; individual tests turn it on.
    monkeypatch.setattr(settings, "local_vision_enabled", False)
    lv.clear_cache()
    yield
    lv.clear_cache()


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


def _patch_no_retrieval(monkeypatch):
    import app.services.memory_service as memsvc
    import app.services.rag_service as rag

    monkeypatch.setattr(rag, "retrieve_chunks", lambda session_id, question, db: [])
    monkeypatch.setattr(memsvc, "get_memory_enabled", lambda db: False)


def _never_call(name, calls):
    def _f(*a, **k):
        calls.append((name, a, k))
        return (True, "SHOULD NOT BE CALLED")
    return _f


# ── defaults + validation ─────────────────────────────────────────────


def test_defaults_are_off_and_exact():
    assert settings.local_vision_enabled is False
    assert settings.local_vision_model == "moondream"
    assert settings.local_vision_max_bytes == 5242880
    assert settings.local_vision_timeout_s == 60.0


def test_ollama_base_repairs_the_host_port_env_convention(monkeypatch):
    """OLLAMA_HOST="0.0.0.0:11434" yields http://0.0.0.0:11434:11434 — an
    invalid URL that would break every call. The resolver repairs it."""
    monkeypatch.setattr(settings, "ollama_host", "0.0.0.0:11434")
    monkeypatch.setattr(settings, "ollama_port", 11434)
    assert lv._ollama_base() == "http://127.0.0.1:11434"


def test_ollama_base_prefers_a_well_formed_url(monkeypatch):
    monkeypatch.setattr(settings, "ollama_host", "127.0.0.1")
    monkeypatch.setattr(settings, "ollama_port", 11434)
    assert lv._ollama_base() == "http://127.0.0.1:11434"


def test_ollama_base_keeps_an_explicit_remote_port(monkeypatch):
    monkeypatch.setattr(settings, "ollama_host", "ollama.internal:9999")
    monkeypatch.setattr(settings, "ollama_port", 11434)
    assert lv._ollama_base() == "http://ollama.internal:9999"


def test_flag_off_returns_a_bounded_response_and_never_calls_ollama(monkeypatch):
    """INVARIANT 1: with the flag OFF there is no model call at all."""
    calls: list = []
    monkeypatch.setattr(lv, "_post_ollama", _never_call("ollama", calls))

    result = lv.describe_image(_png())
    assert result.status == lv.STATUS_DISABLED
    assert result.description is None
    assert result.notice == lv.DISABLED_NOTICE
    assert lv.context_line(result) is None
    assert calls == []


@pytest.mark.parametrize("raw", [_jpeg(), _png(), _webp()])
def test_supported_types_are_accepted(monkeypatch, raw):
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    monkeypatch.setattr(lv, "_post_ollama", lambda b64: (True, "ok"))
    result = lv.describe_image(raw)
    assert result.status == lv.STATUS_OK


@pytest.mark.parametrize("raw", [b"GIF89a" + b"\x00" * 32, b"not an image", b""])
def test_unsupported_type_fails_closed_without_calling_ollama(monkeypatch, raw):
    """INVARIANT 2: a filename / client content type is never trusted."""
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    calls: list = []
    monkeypatch.setattr(lv, "_post_ollama", _never_call("ollama", calls))

    result = lv.describe_image(raw)
    assert result.status == lv.STATUS_UNSUPPORTED_TYPE
    assert result.notice == lv.UNSUPPORTED_TYPE_NOTICE
    assert lv.context_line(result) is None
    assert calls == []


def test_oversize_fails_closed_before_any_ollama_call(monkeypatch):
    """INVARIANT 3."""
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    monkeypatch.setattr(settings, "local_vision_max_bytes", 1024)
    calls: list = []
    monkeypatch.setattr(lv, "_post_ollama", _never_call("ollama", calls))

    result = lv.describe_image(_png(b"\x00" * 4096))
    assert result.status == lv.STATUS_TOO_LARGE
    assert result.notice == lv.TOO_LARGE_NOTICE
    assert calls == []


# ── success + context injection ───────────────────────────────────────


def test_success_returns_description_and_the_exact_untrusted_context_line(monkeypatch):
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    calls: list = []

    def _fake(b64):
        calls.append(b64)
        return True, "A box and an arrow on a white background."

    monkeypatch.setattr(lv, "_post_ollama", _fake)

    result = lv.describe_image(_png())
    assert result.status == lv.STATUS_OK
    assert result.description == "A box and an arrow on a white background."
    assert result.notice == ""
    assert calls and isinstance(calls[0], str)
    assert lv.context_line(result) == (
        "[Image description (untrusted)]: A box and an arrow on a white background."
    )
    # The SAME image is described once: the bounded cache serves the second call
    # (the frontend pre-analysis and the chat hook see one attachment).
    assert lv.describe_image(_png()).description == result.description
    assert len(calls) == 1


def test_context_line_is_none_on_every_failure(monkeypatch):
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    monkeypatch.setattr(lv, "_post_ollama", lambda b64: (False, None))
    result = lv.describe_image(_png())
    assert result.status == lv.STATUS_FAILED
    assert lv.context_line(result) is None
    assert lv.notice_line(result) is not None
    assert lv.FAILED_NOTICE in lv.notice_line(result)


def test_chat_hook_injects_the_untrusted_context_line_on_success(eng, db, monkeypatch):
    """INVARIANT 6: the legacy pre-model hook injects the exact line."""
    from app.services import streaming_service as ss

    _patch_no_retrieval(monkeypatch)
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    monkeypatch.setattr(lv, "_post_ollama",
                        lambda b64: (True, "A pipeline diagram with three stages."))

    ctx = ss.prepare_chat_context(
        session_id=1, user_input="what is in this image?", db=db, user_id=7,
        image_url=_data_url(_png()),
    )
    assert ("[Image description (untrusted)]: A pipeline diagram with three stages."
            in ctx.system_prompt)


def test_chat_hook_surfaces_the_bounded_notice_on_failure(eng, db, monkeypatch):
    """INVARIANT 4: a broken vision call never breaks the chat; the user is told."""
    from app.services import streaming_service as ss

    _patch_no_retrieval(monkeypatch)
    monkeypatch.setattr(settings, "local_vision_enabled", True)

    def _boom(b64):
        raise RuntimeError("ollama is down")

    monkeypatch.setattr(lv, "_post_ollama", _boom)

    ctx = ss.prepare_chat_context(
        session_id=1, user_input="what is in this image?", db=db, user_id=7,
        image_url=_data_url(_png()),
    )
    assert lv.FAILED_NOTICE in ctx.system_prompt
    assert ctx.user_input == "what is in this image?"


def test_chat_hook_is_inert_when_the_flag_is_off(eng, db, monkeypatch):
    from app.services import streaming_service as ss

    _patch_no_retrieval(monkeypatch)
    monkeypatch.setattr(settings, "local_vision_enabled", False)
    calls: list = []
    monkeypatch.setattr(lv, "_post_ollama", _never_call("ollama", calls))

    ctx = ss.prepare_chat_context(
        session_id=1, user_input="what is in this image?", db=db, user_id=7,
        image_url=_data_url(_png()),
    )
    assert "Image description" not in ctx.system_prompt
    assert calls == []


def test_chat_submission_is_still_queued_when_vision_is_broken(eng, db, monkeypatch):
    """INVARIANT 1/4: vision is never on the durable submission path, so a chat
    submission still succeeds and queues even while vision is broken."""
    from app.run_engine import messages_stream as ms

    _patch_no_retrieval(monkeypatch)
    monkeypatch.setattr(settings, "local_vision_enabled", True)

    def _boom(b64):
        raise RuntimeError("vision exploded")

    monkeypatch.setattr(lv, "_post_ollama", _boom)

    ctx = make_ctx(db, user_id=7)
    run, created = ms.resolve_and_submit(
        db, ctx, session_id=1, message="just a normal question",
        idempotency_key=uuid.uuid4().hex,
    )
    assert created is True
    assert run.status == "queued"


# ── privacy ───────────────────────────────────────────────────────────


def test_logs_carry_hash_and_size_only_never_content(monkeypatch, caplog):
    """INVARIANT 5: no image bytes and no description in any log line."""
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    description = "CANARY-DESCRIPTION the zebra migration crosses the river"
    monkeypatch.setattr(lv, "_post_ollama", lambda b64: (True, description))

    raw = _png()
    digest = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")

    with caplog.at_level(logging.INFO, logger="app.services.local_vision_service"):
        result = lv.describe_image(raw)

    assert result.description == description
    blob = caplog.text
    assert digest[:12] in blob, "the hash is the ONLY identifying datum logged"
    assert "bytes=%d" % len(raw) in blob
    assert description not in blob
    assert "CANARY" not in blob
    assert encoded not in blob


def test_decode_image_data_url_is_bounded_and_rejects_non_data_urls():
    raw = _png()
    assert lv.decode_image_data_url(_data_url(raw)) == raw
    # a plain http(s) URL is NEVER fetched (SSRF surface) -> None
    assert lv.decode_image_data_url("https://example.com/x.png") is None
    # malformed base64 -> None, never an exception
    assert lv.decode_image_data_url("data:image/png;base64,!!!!") is None
    # not base64 -> None
    assert lv.decode_image_data_url("data:image/png,raw") is None


# ── the standalone route ──────────────────────────────────────────────


def _route_client():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.auth_service import get_current_user
    from app.services.cookie_service import require_csrf

    app.dependency_overrides[get_current_user] = lambda: type("U", (), {"id": 7})()
    app.dependency_overrides[require_csrf] = lambda: None
    return TestClient(app), app


def _post_image(client):
    return client.post("/api/vision/describe",
                       files={"file": ("x.png", _png(), "image/png")})


def test_route_is_registered():
    from app.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/vision/describe" in paths


def test_route_flag_off_returns_a_bounded_disabled_result(monkeypatch):
    """INVARIANT 1 at the HTTP seam."""
    monkeypatch.setattr(settings, "local_vision_enabled", False)
    calls: list = []
    monkeypatch.setattr(lv, "_post_ollama", _never_call("ollama", calls))
    client, app = _route_client()
    try:
        resp = _post_image(client)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == lv.STATUS_DISABLED
    assert body["notice"] == lv.DISABLED_NOTICE
    assert body["description"] is None
    assert body["context"] is None
    assert calls == []


def test_route_success_returns_description_and_context(monkeypatch):
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    monkeypatch.setattr(lv, "_post_ollama", lambda b64: (True, "A cat on a mat."))
    client, app = _route_client()
    try:
        resp = _post_image(client)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == lv.STATUS_OK
    assert body["description"] == "A cat on a mat."
    assert body["context"] == "[Image description (untrusted)]: A cat on a mat."


def test_route_oversize_fails_closed_without_calling_ollama(monkeypatch):
    monkeypatch.setattr(settings, "local_vision_enabled", True)
    monkeypatch.setattr(settings, "local_vision_max_bytes", 1024)
    calls: list = []
    monkeypatch.setattr(lv, "_post_ollama", _never_call("ollama", calls))
    client, app = _route_client()
    try:
        resp = client.post("/api/vision/describe",
                           files={"file": ("big.png", _png(b"\x00" * 8192), "image/png")})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == lv.STATUS_TOO_LARGE
    assert body["notice"] == lv.TOO_LARGE_NOTICE
    assert calls == []


def test_route_ollama_error_returns_the_bounded_notice(monkeypatch):
    monkeypatch.setattr(settings, "local_vision_enabled", True)

    def _boom(b64):
        raise OSError("connection refused")

    monkeypatch.setattr(lv, "_post_ollama", _boom)
    client, app = _route_client()
    try:
        resp = _post_image(client)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == lv.STATUS_FAILED
    assert body["notice"] == lv.FAILED_NOTICE
