"""Deterministic, offline fakes for the MP-3C3D6B real-browser smoke.

TEST-ONLY. Nothing in `app/**` imports this module; it is loaded exclusively
by `tests/smoke/durable_smoke_app.py`, which the smoke launcher starts.

These fakes replace ONLY nondeterministic outbound transports. The real
FastAPI app, lifespan, supervisor, worker, auth, CSRF, durable submission,
SQLite, Run tables, SSE, notifier, frontend and browser all stay real.

Two hard rules this module enforces:

1. **No provider selection switch.** The fakes are installed by mutating
   in-process objects before startup. They are never reachable from an
   environment variable, config field, route, header, request body, or any
   production startup path.
2. **No outbound network.** `install_network_tripwire` blocks and records
   every non-loopback connection and every connection to a known local
   service port (Ollama, ChromaDB), so "no external call happened" is
   enforced rather than asserted.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

# ── the deterministic response ──────────────────────────────────────

# Markdown-neutral on purpose: no `* _ # ` > | ~`, no brackets, no newlines, no
# repeated spaces. That keeps browser DOM text normalisation a no-op so the
# rendered text can be compared EXACTLY with the persisted text, and it keeps
# every executable/deferred tool marker impossible by construction.
#: Unique extraction sentinels. Alphanumeric only ON PURPOSE: no `_` `*` `#` `>`
#: `|` `~` or brackets, so no CommonMark/GFM rule can transform them and the
#: rendered text can be compared byte-for-byte with no normalisation.
SENTINEL_BEGIN = "THUNDERD6BRESPONSEBEGIN"
SENTINEL_END = "THUNDERD6BRESPONSEEND"

_UNIT = "నమస్కారం🌩️D6BSMOKE "
#: ~18 000 bytes of body so each deterministic chunk crosses the TextEventBuffer
#: byte threshold, producing several separately committed text events and thus
#: several observable progressive renders.
_UNITS = 450
SMOKE_RESPONSE: str = SENTINEL_BEGIN + (_UNIT * _UNITS).strip() + " END" + SENTINEL_END

#: D6B-R4 test-only deterministic delivery schedule (bounded total runtime).
DEFAULT_CHUNK_BYTES = 4500
DEFAULT_CHUNK_DELAY_S = 0.45

#: The unique prompt the browser types. The run-specific nonce is appended by
#: the launcher so the DB assertion can never match an unrelated row.
PROMPT_PREFIX = "D6B deterministic durable chat smoke"


class SmokeInvocationLimitExceeded(RuntimeError):
    """The deterministic provider was invoked more times than the smoke allows."""


def _chunk_utf8(text: str, max_bytes: int) -> Iterator[str]:
    """Split `text` into pieces of at most `max_bytes` UTF-8 bytes, never
    splitting a codepoint. The final piece is the partial remainder."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    buf: list[str] = []
    size = 0
    for ch in text:
        cl = len(ch.encode("utf-8"))
        if buf and size + cl > max_bytes:
            yield "".join(buf)
            buf, size = [], 0
        buf.append(ch)
        size += cl
    if buf:
        yield "".join(buf)


class DeterministicChatProvider:
    """Minimal provider object exposing the `generate_stream_async` seam that
    `chat_stream.default_chat_provider` also returns, so `chat_stream.run_stream`
    drives it through the exact same code path as a real provider."""

    def __init__(self, *, response: str = SMOKE_RESPONSE, chunk_bytes: int = 1500,
                 chunk_delay_s: float = 0.0) -> None:
        self.response = response
        self.chunk_bytes = int(chunk_bytes)
        self.chunk_delay_s = float(chunk_delay_s)

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        import asyncio

        pieces = list(_chunk_utf8(self.response, self.chunk_bytes))
        for i, piece in enumerate(pieces):
            if i and self.chunk_delay_s > 0:
                await asyncio.sleep(self.chunk_delay_s)
            yield {"type": "token", "token": piece}
        yield {"type": "done", "response": self.response}


@dataclass
class _GuardedProvider:
    """One call site of the deterministic provider, bounded by the factory."""

    factory: "DeterministicChatProviderFactory"
    ordinal: int

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        if self.ordinal > self.factory.max_invocations:
            raise SmokeInvocationLimitExceeded(
                "deterministic smoke provider invoked %d times; at most %d allowed"
                % (self.ordinal, self.factory.max_invocations)
            )
        import asyncio

        self.factory.record("provider_invoked", ordinal=self.ordinal)
        pieces = list(_chunk_utf8(self.factory.response, self.factory.chunk_bytes))
        for i, piece in enumerate(pieces):
            if i and self.factory.chunk_delay_s > 0:
                await asyncio.sleep(self.factory.chunk_delay_s)
            yield {"type": "token", "token": piece}
        yield {"type": "done", "response": self.factory.response}


@dataclass
class DeterministicChatProviderFactory:
    """Callable matching the `RunWorker(chat_provider=...)` seam.

    Called by the worker as `factory(chat_input, db=..., user_id=...)`; returns a
    provider object. Records the sealed provider/model snapshot it was handed and
    refuses to serve more than `max_invocations` real provider calls."""

    response: str = SMOKE_RESPONSE
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    chunk_delay_s: float = DEFAULT_CHUNK_DELAY_S
    max_invocations: int = 1
    invocations: int = 0
    snapshots: list = field(default_factory=list)
    #: Optional JSONL path. When set, every selection and every REAL provider
    #: invocation is appended here so the launcher (a different process) can read
    #: the count as evidence instead of trusting a claim.
    record_path: Optional[Path] = None

    def record(self, event: str, **fields: Any) -> None:
        if self.record_path is None:
            return
        entry = {"event": event}
        entry.update({k: v for k, v in fields.items()})
        try:
            path = Path(self.record_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            pass

    def __call__(self, chat_input: Any, *, db: Any, user_id: int) -> _GuardedProvider:
        self.invocations += 1
        snapshot = {
            "provider": getattr(chat_input, "provider", None),
            "model": getattr(chat_input, "model", None),
            "message_count": len(getattr(chat_input, "messages", None) or []),
            "user_id": int(user_id),
        }
        self.snapshots.append(snapshot)
        self.record("provider_selected", ordinal=self.invocations, **snapshot)
        return _GuardedProvider(factory=self, ordinal=self.invocations)


# ── outbound-network tripwire (test-process only) ───────────────────

#: Local service ports the smoke must never contact. ChromaDB and Ollama both
#: live on loopback, so an address check alone cannot prove they were unused.
DEFAULT_BLOCKED_PORTS = frozenset({11434, 11435, 8000})


def _is_loopback_ip(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


class NetworkTripwire:
    """Records and BLOCKS outbound connections, allowing only loopback to
    non-blocked ports (so the browser, backend and Vite dev server still work)."""

    def __init__(self, log_path: Optional[Path] = None,
                 blocked_ports=DEFAULT_BLOCKED_PORTS) -> None:
        self.log_path = Path(log_path) if log_path is not None else None
        self.blocked_ports = frozenset(int(x) for x in blocked_ports)
        self.attempts: list[dict] = []
        self._installed = False
        self._real_connect = None
        self._real_connect_ex = None

    def _record(self, address: Any, reason: str) -> None:
        entry = {"reason": reason, "address": _redact_address(address)}
        self.attempts.append(entry)
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, sort_keys=True) + "\n")
            except OSError:
                pass

    def _evaluate(self, address: Any) -> Optional[str]:
        """Return a block reason, or None to allow."""
        if not isinstance(address, tuple) or len(address) < 2:
            return None                      # AF_UNIX / unusual family: allow
        host, port = address[0], address[1]
        if not isinstance(host, str) or not isinstance(port, int):
            return None
        if not _is_loopback_ip(host):
            return "non_loopback"
        if port in self.blocked_ports:
            return "blocked_local_service_port"
        return None

    def install(self) -> "NetworkTripwire":
        if self._installed:
            return self
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex

        def _connect(sock, address):  # noqa: ANN001
            reason = self._evaluate(address)
            if reason is not None:
                self._record(address, reason)
                raise OSError("D6B smoke tripwire blocked outbound connection (%s)" % reason)
            return real_connect(sock, address)

        def _connect_ex(sock, address):  # noqa: ANN001
            reason = self._evaluate(address)
            if reason is not None:
                self._record(address, reason)
                return 111                 # ECONNREFUSED, never a silent success
            return real_connect_ex(sock, address)

        self._real_connect = real_connect
        self._real_connect_ex = real_connect_ex
        socket.socket.connect = _connect
        socket.socket.connect_ex = _connect_ex
        self._installed = True
        return self

    def uninstall(self) -> None:
        """Restore the original socket methods. Used by tests, and by the smoke
        app on shutdown so the tripwire never outlives its process."""
        if not self._installed:
            return
        if self._real_connect is not None:
            socket.socket.connect = self._real_connect
        if self._real_connect_ex is not None:
            socket.socket.connect_ex = self._real_connect_ex
        self._installed = False


def _redact_address(address: Any) -> str:
    """Bounded, non-secret description of a blocked target."""
    if isinstance(address, tuple) and len(address) >= 2:
        return "%s:%s" % (address[0], address[1])
    return type(address).__name__
