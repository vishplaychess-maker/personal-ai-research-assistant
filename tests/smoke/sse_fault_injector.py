"""D6C-J1 test-only ASGI SSE fault injector.

FIDELITY: this produces a **deterministic test-only server-side truncated
SSE / early EOF**. It is NOT a physical network sever, browser offline emulation,
TCP reset or Wi-Fi loss. It proves the production client's documented
reconnect-on-incomplete-EOF path plus durable server resume semantics.

TEST-ONLY. This module lives under tests/smoke and is imported ONLY by
tests/smoke/durable_smoke_app.py. Nothing in `app/**` imports it, and it is not
selectable through a public header, query parameter, request body, production
environment variable, production config flag or any normal application startup
path.

What it does: wraps an ASGI app; for the FIRST authenticated
`POST /api/sessions/{id}/messages/stream` it forwards COMPLETE SSE frames only,
up to and including the first complete `event: token` frame, then ends the
response with `more_body=False` and unwinds the application coroutine. Every
later request passes through untouched.

It never touches persisted state, the database, the provider, the worker, the
notifier or the approval gateway; it only observes and re-chunks the ASGI
response stream of one HTTP request.
"""

from __future__ import annotations

import time
from typing import Any, Optional

_FRAME_SEP = b"\n\n"


class _Cut(BaseException):
    """Raised inside the wrapped `send` to unwind the application coroutine."""


def _contains_cut(exc: BaseException) -> bool:
    """True when `exc` is our cut, or an ExceptionGroup wrapping it."""
    if isinstance(exc, _Cut):
        return True
    subs = getattr(exc, "exceptions", None)
    if subs:
        return any(_contains_cut(sub) for sub in subs)
    return False


def _frame_meta(frame: bytes) -> tuple[Optional[str], Optional[str]]:
    """(event type, event id) for one complete SSE frame. Never raises."""
    etype: Optional[str] = None
    eid: Optional[str] = None
    for line in frame.split(b"\n"):
        if line.startswith(b"event: "):
            etype = line[7:].strip().decode("utf-8", "replace")
        elif line.startswith(b"id: "):
            eid = line[4:].strip().decode("utf-8", "replace")
    return etype, eid


def is_target_scope(scope: dict) -> bool:
    """Only the authenticated durable submission is a target."""
    if scope.get("type") != "http":
        return False
    if (scope.get("method") or "").upper() != "POST":
        return False
    return (scope.get("path") or "").endswith("/messages/stream")


class SSEStreamTruncator:
    """ASGI middleware that truncates exactly one durable SSE response.

    Disabled by default. `enabled=True` arms it for one cut per instance.
    """

    def __init__(self, app: Any, *, enabled: bool = False,
                 cut_after_token_frames: int = 1, on_cut=None) -> None:
        if cut_after_token_frames < 1:
            raise ValueError("cut_after_token_frames must be >= 1")
        self._on_cut = on_cut
        self._app = app
        self._enabled = bool(enabled)
        self._max_cuts = int(cut_after_token_frames)
        self._token_frames_seen = 0
        self.cuts = 0
        self.records: list[dict] = []
        self._t0 = time.monotonic()

    def arm(self) -> None:
        """Arm the injector (test-only, in-process)."""
        self._enabled = True
        self._token_frames_seen = 0

    async def __call__(self, scope, receive, send) -> None:
        # One-shot: once the budgeted cut has happened, every later request
        # (the reconnect) passes through byte-for-byte.
        if not self._enabled or self.cuts >= self._max_cuts or not is_target_scope(scope):
            return await self._app(scope, receive, send)

        buf = b""
        rec: dict = {"status": None, "path": scope.get("path"),
                     "event_types": [], "event_ids": [], "forwarded_bytes": 0,
                     "terminated_early": False, "cut_at_ms": -1,
                     "token_frames_forwarded": 0}

        async def _send(message):
            nonlocal buf
            mtype = message.get("type")
            if mtype != "http.response.body":
                if mtype == "http.response.start":
                    rec["status"] = message.get("status")
                await send(message)
                return

            buf += message.get("body") or b""
            more = bool(message.get("more_body"))

            while True:
                idx = buf.find(_FRAME_SEP)
                if idx == -1:
                    break
                frame = buf[: idx + len(_FRAME_SEP)]
                buf = buf[idx + len(_FRAME_SEP):]
                etype, eid = _frame_meta(frame)

                await send({"type": "http.response.body", "body": frame,
                            "more_body": True})
                rec["forwarded_bytes"] += len(frame)
                rec["event_types"].append(etype)
                rec["event_ids"].append(eid)

                if etype == "token":
                    rec["token_frames_forwarded"] += 1
                    # Cut here: end the response early and unwind the app.
                    rec["terminated_early"] = True
                    rec["cut_at_ms"] = int((time.monotonic() - self._t0) * 1000)
                    self.cuts += 1
                    self.records.append(rec)
                    if self._on_cut is not None:
                        try:
                            self._on_cut(dict(rec))
                        except Exception:  # noqa: BLE001 - evidence only
                            pass
                    await send({"type": "http.response.body", "body": b"",
                                "more_body": False})
                    raise _Cut()

            if not more:
                # Application finished normally (non-target-path only, since a
                # target response is cut above before this can run).
                await send({"type": "http.response.body", "body": buf,
                            "more_body": False})
                rec["forwarded_bytes"] += len(buf)
                buf = b""

        try:
            await self._app(scope, receive, _send)
        except BaseException as exc:  # noqa: BLE001 - unwinding our own cut
            if not _contains_cut(exc):
                raise
        finally:
            if rec["terminated_early"] and self.records and self.records[-1] is not rec:
                self.records.append(rec)


__all__ = ["SSEStreamTruncator", "is_target_scope"]
