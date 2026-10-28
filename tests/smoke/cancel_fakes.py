"""D6C-J4 test-only cancellable provider fixture.

A deterministic provider that yields a fixed visible prefix and then stays OPEN,
producing nothing, for a long bounded window. That shape is what makes a real
browser cancellation observable end to end without an arbitrary race:

chat_stream._adrive drives the provider through
asyncio.wait({pending}, timeout=poll_interval). When the provider produces
nothing the loop calls on_idle(), which runs the lease / session-family /
cancellation checks and may raise StreamAborted. The finally block then cancels
the pending __anext__ task and calls agen.aclose(). So the stall is observed
through the REAL production guard path.

Every observable is appended to a JSONL file inside THUNDER_SMOKE_ROOT, so the
launcher (a different process) reads counts as evidence instead of trusting a
claim. Nothing here is importable by app/** and there is no production switch:
the fixture is installed in-process by tests/smoke/durable_smoke_app only.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from tests.smoke.fakes import SmokeInvocationLimitExceeded

#: Alphanumeric and markdown-neutral, so a rendered-text search cannot be
#: confused by punctuation or markdown escaping.
CANCEL_SENTINEL = "CANCELSMOKEPARTIAL"
#: Never rendered in an acceptance run. Reaching it proves the cancel failed to
#: stop the provider.
CANCEL_COMPLETION_SENTINEL = "CANCELSMOKECOMPLETED"

#: Deliberately SMALLER than settings.run_event_text_flush_bytes (4096), so the size
#: flush can never write it: only the D6C-J4 time-based idle flush can make this text
#: durable and visible while the provider is stalled. That is what makes the browser
#: journey a genuine before/after for the idle-flush correction (pre-fix, this exact
#: shape produced 3 control RunEvents, ZERO text RunEvents and no rendered text).
CANCEL_PREFIX = (
    CANCEL_SENTINEL + " " + ("durable cancellation partial text " * 24).strip() + " END"
)

DEFAULT_CANCEL_CHUNK_CHARS = 64
#: Long enough that no test can race it, short enough to bound a stuck run.
DEFAULT_CANCEL_STALL_SECONDS = 600.0


@dataclass
class _CancellableProvider:
    """One call site of the cancellable provider, bounded by its factory."""

    factory: "CancellableProviderFactory"
    ordinal: int

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        f = self.factory
        if self.ordinal > f.max_invocations:
            raise SmokeInvocationLimitExceeded(
                "cancellable smoke provider invoked %d times; at most %d allowed"
                % (self.ordinal, f.max_invocations)
            )
        chunks = 0
        f.record("cancel_provider_invoked", ordinal=self.ordinal)
        try:
            for piece in f.prefix_chunks():
                chunks += 1
                f.record("cancel_provider_chunk", ordinal=self.ordinal,
                         chunk_index=chunks, chars=len(piece))
                yield {"type": "token", "token": piece}
            f.record("cancel_provider_stalled", ordinal=self.ordinal, chunks=chunks)
            await asyncio.sleep(f.stall_seconds)
            # Unreachable in an acceptance run: reaching here means the provider
            # was allowed to complete, i.e. the cancellation did NOT stop it.
            f.record("cancel_provider_completed", ordinal=self.ordinal, chunks=chunks)
            yield {
                "type": "done",
                "response": CANCEL_PREFIX + " " + CANCEL_COMPLETION_SENTINEL,
            }
        except asyncio.CancelledError:
            f.record("cancel_provider_cancelled", ordinal=self.ordinal, chunks=chunks)
            raise
        finally:
            f.record("cancel_provider_closed", ordinal=self.ordinal, chunks=chunks)


@dataclass
class CancellableProviderFactory:
    """Callable matching the RunWorker(chat_provider=...) seam.

    Called by the worker as factory(chat_input, db=..., user_id=...) and returns
    a provider object, exactly like the production default factory.
    """

    response_prefix: str = CANCEL_PREFIX
    chunk_chars: int = DEFAULT_CANCEL_CHUNK_CHARS
    stall_seconds: float = DEFAULT_CANCEL_STALL_SECONDS
    max_invocations: int = 1
    invocations: int = 0
    snapshots: list = field(default_factory=list)
    #: JSONL path inside THUNDER_SMOKE_ROOT. Each selection, invocation, chunk,
    #: stall, cancellation, completion attempt and close is appended here.
    record_path: Optional[Path] = None

    def record(self, event: str, **fields: Any) -> None:
        if self.record_path is None:
            return
        entry: dict = {"event": event, "ts": time.time()}
        entry.update(fields)
        try:
            path = Path(self.record_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            pass

    def prefix_chunks(self) -> Iterator[str]:
        text = self.response_prefix
        size = max(1, int(self.chunk_chars))
        for start in range(0, len(text), size):
            yield text[start:start + size]

    def __call__(self, chat_input: Any, *, db: Any, user_id: int) -> _CancellableProvider:
        self.invocations += 1
        snapshot = {
            "provider": getattr(chat_input, "provider", None),
            "model": getattr(chat_input, "model", None),
            "message_count": len(getattr(chat_input, "messages", None) or []),
            "user_id": int(user_id),
        }
        self.snapshots.append(snapshot)
        self.record("cancel_provider_selected", ordinal=self.invocations, **snapshot)
        return _CancellableProvider(factory=self, ordinal=self.invocations)
