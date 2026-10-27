"""Telegram bot service (Phase 1 — text chat only).

aiogram is imported lazily inside ``TelegramService.start()``, so every test
here runs without aiogram, without a Bot object, and without touching
Telegram, the network, or the chat pipeline. The module-level chat helpers
(``prepare_chat_context`` / ``stream_chat_response`` / ``save_assistant_message``)
are monkeypatched the same way the streaming suites patch providers.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import telegram_bot
from app.config import Settings
from app.models.models import ResearchSession, User
from app.services import telegram_service as ts
from app.database import SessionLocal

# The autouse fixture below swaps these module attributes for fakes, so the
# database-backed tests keep references to the originals.
_REAL_RESOLVE = ts.resolve_telegram_session
_REAL_RESET = ts.reset_telegram_session


class _Identity:
    def __init__(self, identifier):
        self.id = identifier


class _Message:
    """Duck-typed stand-in for aiogram's Message."""

    def __init__(self, text, user_id=7, chat_id=55, message_id=1):
        self.text = text
        self.chat = _Identity(chat_id)
        self.from_user = _Identity(user_id)
        self.message_id = message_id


class _RecordingService(ts.TelegramService):
    """Captures outbound traffic instead of calling the Telegram API."""

    def __init__(self, **kwargs):
        kwargs.setdefault("bot_token", "")
        super().__init__(**kwargs)
        self.sent = []
        self.edited = []
        self._ids = 100

    async def _send(self, chat_id, text):
        self._ids += 1
        self.sent.append((chat_id, text))
        return self._ids

    async def _edit(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))


def _run(coro):
    return asyncio.run(coro)


def _fake_stream(*chunks, error=None, content=None):
    """Build a stand-in for stream_chat_response yielding SSE frames."""

    async def _gen(context):
        for chunk in chunks:
            yield f'event: token\ndata: {{"token": "{chunk}"}}\n\n'
        if error is not None:
            yield f'event: error\ndata: {{"code": "{error}", "detail": "boom"}}\n\n'
            return
        yield (
            'event: complete\ndata: {"content": "'
            + (content if content is not None else "".join(chunks))
            + '", "confidence": null, "confidence_reason": null}\n\n'
        )

    return _gen


@pytest.fixture(autouse=True)
def _patched_pipeline(monkeypatch):
    """Keep the service off the real DB / real provider."""
    calls = {"saved": [], "context_calls": []}

    class _Context:
        session_id = 42
        citations = []
        sources_used = False
        memories_used = False

    def prepare_chat_context(session_id, user_input, db, user_id=1, **kwargs):
        calls["context_calls"].append((session_id, user_input, user_id))
        return _Context()

    def save_assistant_message(**kwargs):
        calls["saved"].append(kwargs)
        return type("M", (), {"id": 999})()

    def resolve_telegram_session(db, telegram_user_id):
        return (1, 42)

    def reset_telegram_session(db, telegram_user_id):
        return (1, 43)

    monkeypatch.setattr(ts, "prepare_chat_context", prepare_chat_context)
    monkeypatch.setattr(ts, "save_assistant_message", save_assistant_message)
    monkeypatch.setattr(ts, "resolve_telegram_session", resolve_telegram_session)
    monkeypatch.setattr(ts, "reset_telegram_session", reset_telegram_session)
    monkeypatch.setattr(ts, "SessionLocal", lambda: _NullDb())
    return calls


class _NullDb:
    def close(self):
        pass


def _service(**kwargs):
    kwargs.setdefault("stream_edit", False)
    return _RecordingService(**kwargs)


# ── authorisation ────────────────────────────────────────────


def test_authorized_user_receives_response(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream("Hello ", "there"))
    service = _service(allowed_user_ids=(7,))

    _run(service.handle_message(_Message("hi")))

    assert service.sent == [(55, "Hello there")]
    assert _patched_pipeline["saved"], "assistant reply must be persisted"
    assert _patched_pipeline["saved"][0]["content"] == "Hello there"


def test_empty_allowlist_means_open_but_still_replies(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream("ok"))
    service = _service(allowed_user_ids=())

    _run(service.handle_message(_Message("hi", user_id=123456)))

    assert service.sent == [(55, "ok")]


def test_unauthorized_user_is_rejected_without_generating(_patched_pipeline, monkeypatch):
    def _never(context):  # pragma: no cover - must not run
        raise AssertionError("generation must not start for an unauthorized user")
        yield

    monkeypatch.setattr(ts, "stream_chat_response", _never)
    service = _service(allowed_user_ids=(999,))

    _run(service.handle_message(_Message("hi", user_id=7)))

    assert service.sent == [(55, ts.UNAUTHORIZED_MESSAGE)]
    assert _patched_pipeline["saved"] == []


# ── commands ─────────────────────────────────────────────────


def test_start_command_greets(_patched_pipeline):
    service = _service()
    _run(service.handle_message(_Message("/start")))
    assert service.sent == [(55, ts.START_MESSAGE)]


def test_help_command_lists_commands(_patched_pipeline):
    service = _service()
    _run(service.handle_message(_Message("/help")))
    text = service.sent[0][1]
    assert "/start" in text and "/clear" in text and "/help" in text


def test_clear_command_resets_context_and_confirms(_patched_pipeline):
    service = _service()
    _run(service.handle_message(_Message("/start")))
    _run(service.handle_message(_Message("/clear")))
    assert service.sent[-1] == (55, ts.CLEARED_MESSAGE)


def test_command_with_bot_suffix_is_recognised(_patched_pipeline):
    service = _service()
    _run(service.handle_message(_Message("/start@thunder_bot")))
    assert service.sent == [(55, ts.START_MESSAGE)]


# ── rate limiting ────────────────────────────────────────────


def test_rate_limit_blocks_eleventh_message_in_a_minute(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream("pong"))
    clock = {"t": 0.0}
    service = _service(rate_limit=10, rate_window_s=60.0, clock=lambda: clock["t"])

    for _ in range(10):
        _run(service.handle_message(_Message("ping")))
    _run(service.handle_message(_Message("ping")))

    assert service.sent.count((55, "pong")) == 10
    assert service.sent[-1] == (55, ts.RATE_LIMITED_MESSAGE)


def test_rate_limit_window_expires(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream("pong"))
    clock = {"t": 1000.0}
    service = _service(rate_limit=2, rate_window_s=60.0, clock=lambda: clock["t"])

    _run(service.handle_message(_Message("a")))
    _run(service.handle_message(_Message("b")))
    _run(service.handle_message(_Message("c")))
    assert service.sent[-1] == (55, ts.RATE_LIMITED_MESSAGE)

    clock["t"] += 61.0
    _run(service.handle_message(_Message("d")))
    assert service.sent[-1] == (55, "pong")


def test_rate_limit_is_per_user(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream("pong"))
    service = _service(rate_limit=1)

    _run(service.handle_message(_Message("hi", user_id=1, chat_id=11)))
    _run(service.handle_message(_Message("hi", user_id=2, chat_id=12)))

    assert ("pong" in [t for _, t in service.sent]) is True
    assert service.sent[-1] == (12, "pong")


# ── streaming edits ─────────────────────────────────────────


def test_stream_edit_sends_placeholder_then_edits_in_place(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream("Alpha ", "Beta ", "Gamma"))
    ticks = iter([0.0, 0.1, 5.0, 5.1, 9.0])
    service = _service(stream_edit=True, edit_throttle_s=1.5, clock=lambda: next(ticks))

    _run(service.handle_message(_Message("go")))

    assert service.sent == [(55, ts.THINKING_MESSAGE)]
    assert [text for _, _, text in service.edited][-1] == "Alpha Beta Gamma"
    # Throttled: only the 5.0 and 9.0 ticks crossed the 1.5s gap.
    assert len(service.edited) == 2


def test_identical_preview_is_not_re_edited(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream("same", ""))
    ticks = iter([0.0, 5.0, 6.0, 20.0])
    service = _service(stream_edit=True, edit_throttle_s=1.0, clock=lambda: next(ticks))

    _run(service.handle_message(_Message("go")))

    # Only the first crossing edits; repeats of identical text are skipped
    # because Telegram rejects "message is not modified" edits.
    assert service.edited == [(55, 101, "same")]


def test_long_reply_is_split_into_telegram_sized_messages(_patched_pipeline, monkeypatch):
    body = "x" * (ts.MAX_MESSAGE_LEN + 2500)
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream(body, content=body))
    service = _service(stream_edit=False)

    _run(service.handle_message(_Message("long")))

    assert len(service.sent) == 2
    assert all(len(text) <= ts.MAX_MESSAGE_LEN for _, text in service.sent)
    assert "".join(text for _, text in service.sent) == body


def test_empty_generation_says_so(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream())
    service = _service()

    _run(service.handle_message(_Message("hi")))

    assert service.sent == [(55, ts.EMPTY_REPLY_MESSAGE)]


def test_markers_are_stripped_before_the_reply_is_sent(_patched_pipeline, monkeypatch):
    """The persisted (cleaned) text is what the user sees, not the raw stream."""
    raw = "noted [SAVE_MEMORY: prefers terse answers]"
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream(raw, content=raw))
    monkeypatch.setattr(
        ts, "_strip_markers", lambda content, db, user_id, session_id: "noted"
    )
    service = _service()

    _run(service.handle_message(_Message("remember that")))

    assert service.sent == [(55, "noted")]
    assert _patched_pipeline["saved"][0]["content"] == "noted"


def test_markers_are_stripped_from_the_final_streamed_edit(
    _patched_pipeline, monkeypatch
):
    raw = "answer [USE_MEMORY: old note]"
    monkeypatch.setattr(ts, "stream_chat_response", _fake_stream(raw, content=raw))
    monkeypatch.setattr(
        ts, "_strip_markers", lambda content, db, user_id, session_id: "answer"
    )
    service = _service(stream_edit=True, clock=lambda: 100.0)

    _run(service.handle_message(_Message("go")))

    assert service.sent == [(55, ts.THINKING_MESSAGE)]
    assert service.edited == [(55, 101, "answer")]


# ── error handling ──────────────────────────────────────────


def test_provider_error_yields_bounded_message_not_a_crash(_patched_pipeline, monkeypatch):
    monkeypatch.setattr(
        ts, "stream_chat_response", _fake_stream("partial", error="PROVIDER_ERROR")
    )
    service = _service()

    _run(service.handle_message(_Message("hi")))

    assert service.sent == [(55, ts.GENERATION_ERROR_MESSAGE)]


def test_generator_exception_is_contained(_patched_pipeline, monkeypatch):
    async def _boom(context):
        raise RuntimeError("provider exploded with a secret https://t.me/whatever")
        yield

    monkeypatch.setattr(ts, "stream_chat_response", _boom)
    service = _service(bot_token="123:SECRET-TOKEN-VALUE")

    _run(service.handle_message(_Message("hi")))

    assert service.sent == [(55, ts.GENERATION_ERROR_MESSAGE)]


def test_prepare_failure_is_contained(_patched_pipeline, monkeypatch):
    def _missing(session_id, user_input, db, user_id=1, **kwargs):
        raise ValueError("Session 42 not found")

    monkeypatch.setattr(ts, "prepare_chat_context", _missing)
    service = _service()

    _run(service.handle_message(_Message("hi")))

    assert service.sent == [(55, ts.GENERATION_ERROR_MESSAGE)]


def test_no_secrets_written_to_logs(_patched_pipeline, monkeypatch, caplog):
    async def _boom(context):
        raise RuntimeError("provider exploded")
        yield

    monkeypatch.setattr(ts, "stream_chat_response", _boom)
    service = _service(bot_token="42:SECRETTOKENabcdef")

    with caplog.at_level("DEBUG", logger="app.services.telegram_service"):
        _run(service.handle_message(_Message("super private question")))

    blob = "\n".join(f"{r.levelname}:{r.getMessage()}" for r in caplog.records)
    assert "SECRETTOKENabcdef" not in blob
    assert "super private question" not in blob


def test_unauthorized_probe_does_not_log_message_text(_patched_pipeline, caplog):
    service = _service(allowed_user_ids=(999,), bot_token="42:SECRETTOKENabcdef")

    with caplog.at_level("DEBUG", logger="app.services.telegram_service"):
        _run(service.handle_message(_Message("guess the password", user_id=7)))

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "guess the password" not in blob
    assert "SECRETTOKENabcdef" not in blob


# ── session mapping against the real database ───────────────


@pytest.fixture
def bot_user():
    db = SessionLocal()
    name = f"telegram_{uuid.uuid4().hex[:10]}"
    user = User(username=name, email=f"{name}@test.com")
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id
    db.close()
    try:
        yield user_id
    finally:
        db = SessionLocal()
        db.query(ResearchSession).filter(ResearchSession.user_id == user_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
        db.close()


def _point_at(bot_user_id, monkeypatch):
    """resolve_telegram_session picks the default user; pin it to our row."""
    monkeypatch.setattr(
        ts,
        "_default_thunder_user_id",
        lambda db: bot_user_id,
    )


@pytest.fixture
def real_resolvers(monkeypatch):
    """Put back the resolvers the autouse fixture replaced with fakes."""
    monkeypatch.setattr(ts, "resolve_telegram_session", _REAL_RESOLVE)
    monkeypatch.setattr(ts, "reset_telegram_session", _REAL_RESET)


def test_session_is_created_once_then_reused(real_resolvers, bot_user, monkeypatch):
    _point_at(bot_user, monkeypatch)
    db = SessionLocal()
    try:
        first = ts.resolve_telegram_session(db, 123456)
        second = ts.resolve_telegram_session(db, 123456)
        assert first == second
        rows = (
            db.query(ResearchSession)
            .filter(ResearchSession.user_id == bot_user)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].title == "Telegram 123456"
    finally:
        db.close()


def test_reset_archives_and_reopens(real_resolvers, bot_user, monkeypatch):
    _point_at(bot_user, monkeypatch)
    db = SessionLocal()
    try:
        old_user_id, old_session_id = ts.resolve_telegram_session(db, 654321)
        new_user_id, new_session_id = ts.reset_telegram_session(db, 654321)
        assert new_session_id != old_session_id

        old = db.get(ResearchSession, old_session_id)
        new = db.get(ResearchSession, new_session_id)
        assert "archived" in old.title
        assert new.title == "Telegram 654321"
    finally:
        db.close()


def test_missing_default_user_fails_bounded(real_resolvers, monkeypatch):
    monkeypatch.setattr(ts, "_default_thunder_user_id", lambda db: None)
    db = SessionLocal()
    try:
        with pytest.raises(ts.TelegramSessionError):
            ts.resolve_telegram_session(db, 1)
    finally:
        db.close()


# ── configuration ───────────────────────────────────────────


def test_telegram_defaults_are_off():
    settings = Settings(_env_file=None)
    assert settings.telegram_enabled is False
    assert settings.telegram_bot_token == ""
    assert settings.telegram_allowed_user_ids == []
    assert settings.telegram_stream_edit is True


def test_enabled_without_a_token_fails_at_config_load():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telegram_enabled=True, telegram_bot_token="")


def test_enabled_with_a_token_loads():
    settings = Settings(
        _env_file=None, telegram_enabled=True, telegram_bot_token="42:abc"
    )
    assert settings.telegram_enabled is True


def test_allowed_ids_env_accepts_comma_separated(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123456,789012")
    assert Settings(_env_file=None).telegram_allowed_user_ids == [123456, 789012]


def test_allowed_ids_env_accepts_json_list(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "[123456]")
    assert Settings(_env_file=None).telegram_allowed_user_ids == [123456]


def test_allowed_ids_env_accepts_empty(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    assert Settings(_env_file=None).telegram_allowed_user_ids == []


def test_allowed_ids_env_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "me,friend")
    # ValueError, not a silent empty list: an unparsable allow-list must not
    # fall open and admit every Telegram user.
    with pytest.raises(ValueError):
        Settings(_env_file=None).telegram_allowed_user_ids


def test_allowed_ids_raw_string_is_kept_out_of_the_typed_view(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "7 8 9")
    settings = Settings(_env_file=None)
    assert settings.telegram_allowed_user_ids_raw == "7 8 9"
    assert settings.telegram_allowed_user_ids == [7, 8, 9]


# ── entry point (no aiogram, no network, no polling) ────────


def test_cli_allowlist_overrides_configuration():
    assert telegram_bot.parse_allowed_user_ids("1,2", [9]) == [1, 2]


def test_cli_allowlist_absent_keeps_configuration():
    assert telegram_bot.parse_allowed_user_ids(None, [9]) == [9]


def test_cli_allowlist_empty_opens_the_bot():
    assert telegram_bot.parse_allowed_user_ids("", [9]) == []


def test_cli_allowlist_rejects_non_numeric():
    with pytest.raises(ValueError):
        telegram_bot.parse_allowed_user_ids("abc", [])


def test_cli_flags_apply_before_config_import(monkeypatch):
    env = {}
    monkeypatch.setattr(telegram_bot.os, "environ", env)
    args = telegram_bot.build_parser().parse_args(
        ["--enable", "--no-stream-edit", "--allowed-user-ids", "7", "--bot-token", "42:t"]
    )

    telegram_bot.apply_telegram_env(args)

    assert env == {
        "TELEGRAM_ENABLED": "true",
        "TELEGRAM_BOT_TOKEN": "42:t",
        "TELEGRAM_ALLOWED_USER_IDS": "7",
        "TELEGRAM_STREAM_EDIT": "false",
    }


def test_no_flags_touch_the_environment(monkeypatch):
    env = {}
    monkeypatch.setattr(telegram_bot.os, "environ", env)
    args = telegram_bot.build_parser().parse_args([])

    telegram_bot.apply_telegram_env(args)

    assert env == {}


def _stub_local_secrets(monkeypatch):
    """Keep main() probes from touching the real secret store on disk."""
    monkeypatch.setattr(telegram_bot, "ensure_jwt_secret", lambda: None)
    monkeypatch.setattr(telegram_bot, "ensure_encryption_key", lambda: None)


def test_main_refuses_to_poll_when_disabled(monkeypatch, capsys):
    _stub_local_secrets(monkeypatch)
    monkeypatch.setattr("app.config.settings", Settings(_env_file=None))

    assert telegram_bot.main([]) == 1
    assert "disabled" in capsys.readouterr().err


def test_main_refuses_to_poll_without_a_usable_token(monkeypatch, capsys):
    _stub_local_secrets(monkeypatch)
    # Config load already refuses enabled-without-token, so main()'s own guard
    # covers the value that survives validation: whitespace. A real Settings
    # cannot be built for that case, so the import-time singleton is stubbed.
    monkeypatch.setattr(
        "app.config.settings",
        SimpleNamespace(
            telegram_enabled=True,
            telegram_bot_token="   ",
            telegram_stream_edit=True,
            telegram_allowed_user_ids=[],
        ),
    )

    assert telegram_bot.main([]) == 1
    assert "bot token" in capsys.readouterr().err


def test_config_failure_names_the_field_but_not_its_value():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, telegram_enabled="maybe")

    reported = telegram_bot._problem_fields(caught.value)

    assert reported == "telegram_enabled"
    assert "maybe" not in reported


# ── aiogram wiring (skipped when the dependency is absent) ──


@pytest.fixture
def fake_aiogram(monkeypatch):
    """Replace aiogram's Bot/Dispatcher so no Telegram traffic can happen."""
    aiogram = pytest.importorskip("aiogram")

    class _Events(list):
        def __init__(self):
            super().__init__()
            self.dispatchers = []

    events = _Events()

    class _Session:
        async def close(self):
            events.append("session closed")

    class _Bot:
        def __init__(self, token=None, **kwargs):
            events.append("bot built")
            self.session = _Session()

        async def get_me(self):
            return type("Me", (), {"username": "thunder_test_bot"})()

    class _MessageObserver:
        """aiogram-style observer: handlers attach through register()."""

        def __init__(self):
            self.handlers = []

        def register(self, callback, *filters, **kwargs):
            self.handlers.append(callback)
            events.append("handler registered")
            return callback

        def __call__(self, *filters, **kwargs):
            def wrapper(callback):
                return self.register(callback, *filters, **kwargs)

            return wrapper

    class _Dispatcher:
        def __init__(self, *args, **kwargs):
            self.message = _MessageObserver()
            self.polling_kwargs = None
            events.dispatchers.append(self)

        async def start_polling(self, bot, **kwargs):
            self.polling_kwargs = kwargs
            events.append("polling started")

        def stop_polling(self):
            events.append("stop requested")

    monkeypatch.setattr(aiogram, "Bot", _Bot)
    monkeypatch.setattr(aiogram, "Dispatcher", _Dispatcher)
    return events


def test_start_builds_bot_registers_handler_and_polls(fake_aiogram, caplog):
    service = ts.TelegramService(bot_token="123:WIRETESTSECRETTOKEN")
    seen = []

    with caplog.at_level("DEBUG", logger="app.services.telegram_service"):
        _run(service.start(on_ready=seen.append))

    assert fake_aiogram == [
        "bot built",
        "handler registered",
        "polling started",
        "session closed",
    ]
    assert seen == ["thunder_test_bot"]
    # The handler must actually be attached to the message observer; aiogram's
    # observer treats positional arguments as filters, so a bare call registers
    # nothing and the bot would poll forever without answering.
    (dispatcher,) = fake_aiogram.dispatchers
    [registered] = dispatcher.message.handlers
    assert asyncio.iscoroutinefunction(registered)
    # The entry point owns SIGINT/SIGTERM, so aiogram must not take them over.
    assert dispatcher.polling_kwargs == {
        "allowed_updates": ["message"],
        "handle_signals": False,
    }
    assert "WIRETESTSECRETTOKEN" not in "\n".join(r.getMessage() for r in caplog.records)


def test_start_closes_the_session_when_polling_fails(fake_aiogram, monkeypatch):
    import aiogram

    class _BoomDispatcher(aiogram.Dispatcher):
        async def start_polling(self, bot, **kwargs):
            raise RuntimeError("telegram 401 unauthorized")

    monkeypatch.setattr(aiogram, "Dispatcher", _BoomDispatcher)
    service = ts.TelegramService(bot_token="123:WIRETESTSECRETTOKEN")

    with pytest.raises(RuntimeError):
        _run(service.start())

    assert fake_aiogram == ["bot built", "handler registered", "session closed"]


def test_stop_asks_the_dispatcher_to_finish(fake_aiogram):
    service = ts.TelegramService(bot_token="123:WIRETESTSECRETTOKEN")

    async def _start_then_stop():
        polling = asyncio.ensure_future(service.start())
        await asyncio.sleep(0)  # let start() reach start_polling
        await service.stop()
        await polling

    _run(_start_then_stop())

    assert "stop requested" in fake_aiogram


def test_stop_before_start_is_a_no_op(fake_aiogram):
    service = ts.TelegramService(bot_token="123:WIRETESTSECRETTOKEN")

    _run(service.stop())

    assert fake_aiogram == []


def test_stop_never_raises_when_the_poller_refuses(fake_aiogram):
    """aiogram rejects stop_polling() outside a polling run; shutdown must not."""
    service = ts.TelegramService(bot_token="123:WIRETESTSECRETTOKEN")

    def refusing():
        raise RuntimeError("Polling is not started")

    async def _start_then_stop():
        polling = asyncio.ensure_future(service.start())
        await asyncio.sleep(0)
        fake_aiogram.dispatchers[0].stop_polling = refusing
        await service.stop()
        await polling

    _run(_start_then_stop())
