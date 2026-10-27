"""Telegram bot process for Thunder AI (Phase 1 - text chat only).

Runs ALONGSIDE the web app as its own process: it starts no uvicorn, binds no
port, and changes nothing about the HTTP server. Opt-in by design - with
TELEGRAM_ENABLED=false (the default) this process refuses to start.

Reuses launcher.py's bootstrap so both processes agree on the portable data
dir, the persisted JWT/encryption secrets, and UTF-8 stdio in a frozen build.
Environment defaults are applied BEFORE app.config is imported, because
pydantic-settings reads the environment at import time.

Run from source:
    python backend/telegram_bot.py --enable
    python backend/telegram_bot.py --enable --allowed-user-ids 123456789
    TELEGRAM_BOT_TOKEN=123:ABC python backend/telegram_bot.py

Get a token from @BotFather: /newbot, then keep the token out of the repo -
it is a bearer credential that can send messages as your bot.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from launcher import (
    _configure_stdio,
    _ensure_backend_on_path,
    ensure_encryption_key,
    ensure_jwt_secret,
)

BANNER = "[thunder-telegram] Thunder AI Telegram bot - @{username}"
SHUTDOWN_GRACE_S = 15.0
PREFIX = "[thunder-telegram] "


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thunder-telegram",
        description="Thunder AI Telegram bot (text chat only)",
    )
    parser.add_argument(
        "--bot-token",
        default=None,
        help="Telegram bot token (default: TELEGRAM_BOT_TOKEN)",
    )
    parser.add_argument(
        "--allowed-user-ids",
        default=None,
        help="comma-separated Telegram user IDs allowed to use the bot; "
        "omit to fall back to TELEGRAM_ALLOWED_USER_IDS (empty = allow all)",
    )
    parser.add_argument(
        "--no-stream-edit",
        action="store_true",
        help="send one finished message instead of editing a placeholder",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="opt in for this run without editing the environment",
    )
    return parser


def apply_telegram_env(args: argparse.Namespace) -> None:
    """Push CLI choices into the environment before app.config is imported."""
    if args.enable:
        os.environ["TELEGRAM_ENABLED"] = "true"
    if args.bot_token:
        os.environ["TELEGRAM_BOT_TOKEN"] = args.bot_token
    if args.allowed_user_ids is not None:
        os.environ["TELEGRAM_ALLOWED_USER_IDS"] = args.allowed_user_ids
    if args.no_stream_edit:
        os.environ["TELEGRAM_STREAM_EDIT"] = "false"


def parse_allowed_user_ids(raw: str | None, configured: list[int]) -> list[int]:
    """CLI list wins; a malformed entry fails loudly instead of silently wide-open."""
    if raw is None:
        return list(configured or [])
    tokens = raw.replace(",", " ").split()
    if not tokens:
        return []
    try:
        return [int(token) for token in tokens]
    except ValueError:
        raise ValueError(
            "allowed user IDs must be numeric Telegram IDs"
        ) from None


def print_banner(username: str) -> None:
    print(PREFIX + BANNER.format(username=username))
    print(PREFIX + "Text messages are answered by the local chat pipeline.")
    print(PREFIX + "Ctrl-C stops polling; in-flight replies are allowed to finish.")


def _fail(message: str) -> int:
    print(PREFIX + message, file=sys.stderr)
    return 1


def _problem_fields(exc: Exception) -> str:
    """Field names from a pydantic ValidationError, never the rejected values."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return "unknown fields"
    names = sorted(
        {
            ".".join(str(part) for part in problem.get("loc", ()))
            for problem in errors()
            if problem.get("loc")
        }
    )
    return ", ".join(names) if names else "unknown fields"


async def run_bot(service) -> int:
    """Poll until a signal arrives, then shut down cooperatively."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop() -> None:
        if stop.is_set():
            return
        stop.set()
        asyncio.ensure_future(service.stop())

    installed = _install_signal_handlers(loop, request_stop)
    poll_task = asyncio.ensure_future(service.start(on_ready=print_banner))
    wait_stop = asyncio.ensure_future(stop.wait())
    try:
        done, _pending = await asyncio.wait(
            {poll_task, wait_stop}, return_when=asyncio.FIRST_COMPLETED
        )
        if poll_task in done:
            exc = poll_task.exception()
            if exc is not None:
                print(PREFIX + f"bot stopped ({type(exc).__name__})", file=sys.stderr)
                return 1
            return 0
        request_stop()
        try:
            await asyncio.wait_for(poll_task, timeout=SHUTDOWN_GRACE_S)
        except asyncio.TimeoutError:
            print(
                PREFIX + f"polling did not stop within {SHUTDOWN_GRACE_S:.0f}s",
                file=sys.stderr,
            )
            poll_task.cancel()
            return 1
        except Exception as exc:  # noqa: BLE001 - shutdown report only
            print(PREFIX + f"bot stopped ({type(exc).__name__})", file=sys.stderr)
            return 1
        return 0
    finally:
        wait_stop.cancel()
        _release_signal_handlers(loop, installed)


def _install_signal_handlers(loop, handler) -> list[str]:
    """add_signal_handler where supported; plain signal.signal on Windows."""
    installed: list[str] = []
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, RuntimeError):
            signal.signal(
                sig,
                lambda *_args: loop.call_soon_threadsafe(handler),
            )
        installed.append(name)
    return installed


def _release_signal_handlers(loop, names: list[str]) -> None:
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.remove_signal_handler(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _ensure_backend_on_path()
    _configure_stdio()
    apply_telegram_env(args)

    # Same portable secrets the web app uses: the bot reads provider API keys
    # from the same encrypted store, so it must resolve the same key material.
    try:
        ensure_jwt_secret()
        ensure_encryption_key()
    except Exception as exc:  # noqa: BLE001 - the secret store may be unwritable
        return _fail(
            f"could not prepare local secrets ({type(exc).__name__}); "
            "the bot needs the same key material as the web app."
        )

    try:
        from app.config import settings
    except Exception as exc:  # noqa: BLE001 - configuration errors are loud
        return _fail(
            f"configuration failed to load ({type(exc).__name__}): "
            f"{_problem_fields(exc)}. Check TELEGRAM_* and JWT_* values; "
            "no values are echoed."
        )

    if not settings.telegram_enabled:
        return _fail(
            "the bot is disabled. Set TELEGRAM_ENABLED=true or pass --enable."
        )

    token = (args.bot_token or settings.telegram_bot_token or "").strip()
    if not token:
        return _fail("no bot token. Set TELEGRAM_BOT_TOKEN or pass --bot-token.")

    try:
        allowed = parse_allowed_user_ids(
            args.allowed_user_ids, settings.telegram_allowed_user_ids
        )
    except ValueError as exc:
        return _fail(str(exc))

    from app.services.telegram_service import TelegramService

    service = TelegramService(
        bot_token=token,
        allowed_user_ids=allowed,
        stream_edit=settings.telegram_stream_edit,
    )
    if allowed:
        print(PREFIX + f"allow-list active for {len(allowed)} Telegram user ID(s).")
    else:
        print(
            PREFIX + "no allow-list: any Telegram user who finds the bot can talk to it."
        )

    try:
        return asyncio.run(run_bot(service))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
