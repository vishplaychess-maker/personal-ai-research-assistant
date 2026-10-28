"""MP-4C test-only Python approval fixtures (real-browser approval journey).

TEST-ONLY. Nothing in `app/**` imports this module. It reuses the production
seams unchanged: `RunWorker(chat_provider=..., python_executor=...)`, the
canonical `[PYTHON_CODE: ...]` grammar and MarkerScanner (`capture_python`).

No fake here creates, approves, denies, consumes or dispatches an approval. The
counting executor only *receives* the code the gateway reopened from the sealed
`tool_args` payload and returns a typed `SandboxOutcome`; it NEVER starts a
subprocess. This journey therefore proves the approval/durable wiring, NOT real
subprocess, host-filesystem or network isolation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text as _sql_text

#: The exact proposal body. It must never appear in a public UI/event field, a
#: log line, or another user's view: it lives ONLY inside the sealed encrypted
#: tool_args payload and is handed back to the executor.
PYTHON_CODE = "print('MP4C_SMOKE_PY_OK')"
MARKER = "[PYTHON_CODE: " + PYTHON_CODE + "]"
VISIBLE_PREFIX = "Smoke python approval fixture ready.\n"
SECOND_ANSWER = "MP4C_PYTHON_TOOL_RESULT_ACKNOWLEDGED"
EXECUTOR_RESULT = "MP4C_FAKE_EXECUTOR_OUTPUT_NOT_A_SUBPROCESS"
#: Private leak canary. It must never appear in browser-visible text, RunEvents,
#: refs, status reasons, the backend log, or recorded fixture metadata.
CANARY = "CANARY-MP4C-PY-CODE-MUST-NEVER-LEAK-2b9f"


def _append_jsonl(path: Optional[Path], rec: dict) -> None:
    if path is None:
        return
    try:
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass


@dataclass
class PythonMarkerProvider:
    """Stateful deterministic provider following the real continuation contract.

    Invocation 1: a visible prefix + exactly ONE `[PYTHON_CODE: ...]` marker,
    split across several chunks so the scanner must reassemble it.
    Invocation 2 (only if production really continues after dispatch): one
    deterministic final answer containing no marker.

    Usable directly as the `RunWorker(chat_provider=...)` factory.
    """

    marker_split: int = 5
    visible_prefix: str = VISIBLE_PREFIX
    record_path: Optional[Path] = None
    invocations: int = 0
    selections: int = 0
    calls: list = field(default_factory=list)

    def __call__(self, chat_input: Any, *, db: Any = None, user_id: int = 0):
        self.selections += 1
        _append_jsonl(self.record_path,
                      {"event": "provider_selected", "ordinal": self.selections})
        return self

    async def generate_stream_async(self, messages, system_prompt=None,
                                    model_name=None):
        self.invocations += 1
        ordinal = self.invocations
        self.calls.append({"ordinal": ordinal, "message_count": len(messages or [])})
        _append_jsonl(self.record_path,
                      {"event": "provider_invoked", "ordinal": ordinal,
                       "message_count": len(messages or [])})
        if ordinal == 1:
            step = max(1, int(self.marker_split))
            pieces = [self.visible_prefix]
            pieces += [MARKER[i:i + step] for i in range(0, len(MARKER), step)]
            for piece in pieces:
                if piece:
                    yield {"type": "token", "token": piece}
            yield {"type": "done", "response": self.visible_prefix + MARKER}
        else:
            yield {"type": "token", "token": SECOND_ANSWER}
            yield {"type": "done", "response": SECOND_ANSWER}


class CountingPythonExecutor:
    """Callable matching `RunWorker(python_executor=...)`:
    `code -> SandboxOutcome`. Never starts a subprocess.

    At call time it opens its OWN session and counts already-committed
    `dispatch_records` with `outcome = 'started'`, which is exactly the ordering
    invariant the gateway must satisfy BEFORE invoking the callback. It also
    records whether the reopened code equals the sealed proposal.
    """

    def __init__(self, *, record_path: Optional[Path] = None) -> None:
        self.record_path = record_path
        self.invocations = 0
        self.codes: list = []
        self.started_at_call: list = []
        self.gateway_dispatch_calls = 0  # always 0: this fake never drives it

    @staticmethod
    def _started_count() -> int:
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            return int(db.execute(_sql_text(
                "SELECT COUNT(*) FROM dispatch_records WHERE outcome = 'started'"
            )).scalar() or 0)
        finally:
            db.close()

    def __call__(self, code: str):
        from app.tools.python_sandbox import SandboxOutcome

        started = self._started_count()
        self.invocations += 1
        self.codes.append(code)
        self.started_at_call.append(started)
        _append_jsonl(self.record_path, {
            "event": "executor_invoked",
            "ordinal": self.invocations,
            "started_dispatch_at_call": started,
            "code_matches_proposal": bool(code == PYTHON_CODE),
            "code_len": len(code or ""),
        })
        return SandboxOutcome(EXECUTOR_RESULT, False)

    def safe_metadata(self) -> dict:
        """Non-secret summary for evidence reporting."""
        return {
            "invocations": self.invocations,
            "started_dispatch_at_call": list(self.started_at_call),
            "code_matches_proposal": [c == PYTHON_CODE for c in self.codes],
            "gateway_dispatch_calls": self.gateway_dispatch_calls,
        }


class CountingMcpTransport:
    """Callable matching `RunWorker(mcp_transport=...)`.

    It must NEVER be called on the Python path: `_mcp_dispatch` routes
    `tool_id == 'python_sandbox'` to `python_executor` and never to the
    transport. Any call is recorded AND raised, so a non-zero count is a
    loud defect rather than a silent one."""

    def __init__(self, *, record_path: Optional[Path] = None) -> None:
        self.record_path = record_path
        self.invocations = 0

    def __call__(self, db, user_id, tool_id, args):
        self.invocations += 1
        _append_jsonl(self.record_path, {"event": "mcp_transport_invoked",
                                         "ordinal": self.invocations,
                                         "tool_id": str(tool_id)})
        raise AssertionError(
            "MCP transport must never be invoked on the Python path")


__all__ = ["PYTHON_CODE", "MARKER", "VISIBLE_PREFIX", "SECOND_ANSWER",
           "EXECUTOR_RESULT", "CANARY", "PythonMarkerProvider",
           "CountingPythonExecutor", "CountingMcpTransport"]
