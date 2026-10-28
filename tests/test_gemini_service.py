"""Gemini API service + provider adapter (mocked; no real API calls).

google-generativeai is an OPTIONAL dependency and is NOT installed in this
environment, so every test injects a fake google.generativeai module (or forces
an ImportError) and asserts the fail-closed / privacy behaviour.

Invariants pinned here:
  1. chat_completion success + correct message/role conversion
  2. API error -> bounded GeminiAPIError (code only, never upstream text)
  3. missing SDK / missing key -> bounded GeminiAPIError, no call made
  4. vision_describe success (prompt + PIL image) and bounded failure
  5. no content / key / upstream text in any log line
  6. the provider factory dispatches "gemini" to GeminiProvider
"""

from __future__ import annotations

import base64
import builtins
import logging
import sys
import types

import pytest

from app.services.gemini_service import GeminiAPIError, GeminiService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _real_png() -> bytes:
    """A genuinely decodable PNG (PIL must be able to open it)."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


REAL_PNG = _real_png()


class _Resp:
    def __init__(self, text):
        self.text = text


def _install_fake_genai(monkeypatch, *, text="A generated answer.", raise_exc=None):
    """Inject a fake google.generativeai and return the recorded call info."""
    calls = {"configure": [], "models": []}
    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.generativeai")

    def _configure(**kwargs):
        calls["configure"].append(kwargs)

    class _Model:
        def __init__(self, model_name, **kwargs):
            self.model_name = model_name
            self.kwargs = kwargs
            calls["models"].append({"model": model_name, "kwargs": kwargs})

        def generate_content(self, contents, request_options=None):
            calls["models"][-1]["contents"] = contents
            calls["models"][-1]["request_options"] = request_options
            if raise_exc is not None:
                raise raise_exc
            return _Resp(text)

    fake_genai.configure = _configure
    fake_genai.GenerativeModel = _Model
    fake_google.generativeai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)
    return calls


# ── chat_completion ───────────────────────────────────────────────────


def test_chat_completion_success(monkeypatch):
    calls = _install_fake_genai(monkeypatch, text="Hello from Gemini.")
    svc = GeminiService(api_key="KEY", model="gemini-1.5-flash", timeout=12.5)

    out = svc.chat_completion([
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ])

    assert out == "Hello from Gemini."
    assert calls["configure"][0]["api_key"] == "KEY"
    assert calls["models"][0]["model"] == "gemini-1.5-flash"
    assert calls["models"][0]["kwargs"]["system_instruction"] == "You are terse."
    assert calls["models"][0]["kwargs"]["generation_config"]["max_output_tokens"] == 2048
    contents = calls["models"][0]["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert calls["models"][0]["request_options"]["timeout"] == 12.5


def test_message_format_conversion_roles_and_system():
    system, contents = GeminiService._to_gemini_contents([
        {"role": "system", "content": "S1"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "A"},
        {"role": "user", "content": "U2"},
    ], system_prompt="S0")

    assert system == "S0\n\nS1"
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[0]["parts"] == [{"text": "U"}]
    assert contents[1]["parts"] == [{"text": "A"}]


def test_chat_completion_multimodal_data_url_becomes_inline_data(monkeypatch):
    calls = _install_fake_genai(monkeypatch)
    svc = GeminiService(api_key="KEY")
    data_url = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")

    svc.chat_completion([{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}])

    parts = calls["models"][0]["contents"][0]["parts"]
    assert parts[0] == {"text": "what is this"}
    assert parts[1]["mime_type"] == "image/png"
    assert parts[1]["data"] == PNG


def test_chat_completion_api_error_is_bounded_and_hides_text(monkeypatch):
    _install_fake_genai(monkeypatch, raise_exc=RuntimeError("CANARY-UPSTREAM-TEXT"))
    svc = GeminiService(api_key="KEY")

    with pytest.raises(GeminiAPIError) as ei:
        svc.chat_completion([{"role": "user", "content": "hi"}])

    assert ei.value.code == "gemini_error"
    assert "CANARY-UPSTREAM-TEXT" not in str(ei.value)


def test_chat_completion_empty_response_is_bounded(monkeypatch):
    _install_fake_genai(monkeypatch, text="")
    svc = GeminiService(api_key="KEY")

    with pytest.raises(GeminiAPIError) as ei:
        svc.chat_completion([{"role": "user", "content": "hi"}])

    assert ei.value.code == "gemini_empty_response"


def test_missing_key_is_bounded_and_makes_no_call(monkeypatch):
    calls = _install_fake_genai(monkeypatch)
    svc = GeminiService(api_key="")

    with pytest.raises(GeminiAPIError) as ei:
        svc.chat_completion([{"role": "user", "content": "hi"}])

    assert ei.value.code == "gemini_not_configured"
    assert calls["models"] == []


def test_missing_sdk_is_bounded(monkeypatch):
    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "google.generativeai" or name.startswith("google.generativeai."):
            raise ImportError("no sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    monkeypatch.delitem(sys.modules, "google.generativeai", raising=False)
    monkeypatch.delitem(sys.modules, "google", raising=False)

    with pytest.raises(GeminiAPIError) as ei:
        GeminiService(api_key="KEY").chat_completion([{"role": "user", "content": "hi"}])

    assert ei.value.code == "gemini_sdk_missing"


# ── vision_describe ───────────────────────────────────────────────────


def test_vision_describe_success(monkeypatch):
    calls = _install_fake_genai(monkeypatch, text="A blue square on a white background.")
    svc = GeminiService(api_key="KEY", vision_model="gemini-1.5-flash")

    out = svc.vision_describe(REAL_PNG, prompt="Describe this.")

    assert out == "A blue square on a white background."
    assert calls["models"][0]["model"] == "gemini-1.5-flash"
    contents = calls["models"][0]["contents"]
    assert contents[0] == "Describe this."
    from PIL import Image
    assert isinstance(contents[1], Image.Image)


def test_vision_describe_api_error_is_bounded(monkeypatch):
    _install_fake_genai(monkeypatch, raise_exc=RuntimeError("CANARY-VISION"))
    svc = GeminiService(api_key="KEY")

    with pytest.raises(GeminiAPIError) as ei:
        svc.vision_describe(REAL_PNG)

    assert ei.value.code == "gemini_error"
    assert "CANARY-VISION" not in str(ei.value)


def test_vision_describe_invalid_bytes_is_bounded(monkeypatch):
    _install_fake_genai(monkeypatch)
    svc = GeminiService(api_key="KEY")

    with pytest.raises(GeminiAPIError) as ei:
        svc.vision_describe(b"not an image at all")

    assert ei.value.code == "gemini_error"


def test_no_content_or_key_in_logs(monkeypatch, caplog):
    _install_fake_genai(monkeypatch, raise_exc=RuntimeError("CANARY-UPSTREAM-TEXT"))
    svc = GeminiService(api_key="SECRET-KEY")

    with caplog.at_level(logging.INFO, logger="app.services.gemini_service"):
        with pytest.raises(GeminiAPIError):
            svc.chat_completion([{"role": "user", "content": "CANARY-PROMPT"}])
        with pytest.raises(GeminiAPIError):
            svc.vision_describe(REAL_PNG)

    blob = caplog.text
    for canary in ("CANARY-UPSTREAM-TEXT", "CANARY-PROMPT", "SECRET-KEY"):
        assert canary not in blob, canary
    assert "gemini:" in blob  # the bounded, type-only warning was logged


# ── provider factory dispatch ─────────────────────────────────────────


def test_provider_factory_dispatches_gemini():
    from app.services.llm_providers import _build_provider
    from app.services.llm_providers.gemini_provider import GeminiProvider

    provider = _build_provider("gemini")

    assert isinstance(provider, GeminiProvider)
    assert provider.name == "Gemini"
    assert provider.default_model == "gemini-1.5-flash"


def test_provider_stream_emits_one_token_then_done(monkeypatch):
    import asyncio
    from app.services.llm_providers.gemini_provider import GeminiProvider

    monkeypatch.setattr(GeminiProvider, "generate_response",
                        lambda self, m, system_prompt=None, model_name=None: "Full answer.")
    provider = GeminiProvider()

    async def _collect():
        return [c async for c in provider.generate_stream_async(
            [{"role": "user", "content": "hi"}])]

    chunks = asyncio.run(_collect())
    assert chunks[0] == {"type": "token", "token": "Full answer."}
    assert chunks[-1] == {"type": "done", "response": "Full answer."}


def test_provider_stream_error_chunk_is_bounded(monkeypatch):
    import asyncio
    from app.services.gemini_service import GeminiAPIError
    from app.services.llm_providers.gemini_provider import GeminiProvider

    def _boom(self, m, system_prompt=None, model_name=None):
        raise GeminiAPIError("gemini_not_configured")

    monkeypatch.setattr(GeminiProvider, "generate_response", _boom)
    provider = GeminiProvider()

    async def _collect():
        return [c async for c in provider.generate_stream_async(
            [{"role": "user", "content": "hi"}])]

    chunks = asyncio.run(_collect())
    assert chunks == [{"type": "error", "error": "gemini_not_configured"}]
