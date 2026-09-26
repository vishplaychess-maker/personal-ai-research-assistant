"""D6C-J2/J3 test-only MCP fixtures (shared by approve and deny journeys).

TEST-ONLY. Nothing in `app/**` imports this module. It reuses the production
seams unchanged: `RunWorker(mcp_transport=...)`, `app.approval.registry` seeding
helpers, the canonical `[MCP_CALL: ...]` marker grammar and `MarkerScanner`.

No fake here creates, approves, denies, consumes or dispatches an approval. The
fake transport only *receives* the real gateway-dispatched call and returns one
deterministic result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from sqlalchemy import text as _sql_text

from app.approval import registry
from app.models.models import TrustedTool

#: Private leak canary. It must never appear in browser-visible details, logs,
#: RunEvents, refs, status reasons or recorded fixture metadata.
CANARY = "CANARY-J2J3-MCP-MUST-NEVER-LEAK-7c41e9"

TOOL_ID = "mcp__smoke__echo"
TOOL_VERSION = "1.0.0"
MCP_SERVER_NAME = "smoke"
EXECUTABLE_IDENTITY = "smoke-echo-v1"
ECHO_SCHEMA: dict = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
ECHO_ARGS: dict = {"text": "ping"}
ECHO_RESULT = "SMOKE_ECHO_OK"

MARKER = '[MCP_CALL: mcp__smoke__echo {"text":"ping"}]'
VISIBLE_PREFIX = "Smoke approval fixture ready.\n"
SECOND_ANSWER = "SMOKE_TOOL_RESULT_ACKNOWLEDGED"


def _safe_args_digest(args: Any) -> str:
    """Short, non-secret digest of normalized args (never the raw value)."""
    try:
        blob = json.dumps(args, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        blob = repr(type(args).__name__)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def seed_trusted_echo_tool(db, *, owner_user_id: int, tenant_id: Optional[str] = None,
                           input_schema: Optional[dict] = None,
                           unlock: bool = True) -> TrustedTool:
    """Seed ONE real `trusted_tools` row using the production registry helpers so
    the schema hash and server fingerprint are computed by production code.

    `locked=False` is applied ONLY to this explicitly seeded identity and only when
    `unlock=True`; a different schema therefore yields a DIFFERENT identity that
    stays locked (fail-closed). Global registry behaviour is untouched."""
    schema = ECHO_SCHEMA if input_schema is None else input_schema
    row = registry.register_or_refresh(
        db, owner_user_id=owner_user_id, tool_id=TOOL_ID, tool_name="echo",
        tenant_id=tenant_id, mcp_server_name=MCP_SERVER_NAME,
        executable_identity=EXECUTABLE_IDENTITY,
        non_secret_launch_config={"server": MCP_SERVER_NAME},
        input_schema=schema, tool_version=TOOL_VERSION,
        risk_metadata={"risk": "approval_required"},
    )
    if unlock:
        row.locked = False
        row.lock_reason = None
        db.commit()
    return row


@dataclass
class MarkerProvider:
    """Stateful deterministic provider following the real continuation contract.

    Invocation 1: visible prefix + exactly ONE `[MCP_CALL: ...]` marker, split
    across several chunks so the scanner must reassemble it.
    Invocation 2 (only if production actually continues after dispatch): one
    deterministic final answer containing no marker.

    It is also usable directly as the `RunWorker(chat_provider=...)` factory."""

    marker_split: int = 7
    visible_prefix: str = VISIBLE_PREFIX
    invocations: int = 0
    selections: int = 0
    calls: list = field(default_factory=list)

    def __call__(self, chat_input: Any, *, db: Any = None, user_id: int = 0):
        self.selections += 1
        return self

    def _marker_chunks(self) -> Iterator[str]:
        step = max(1, int(self.marker_split))
        for i in range(0, len(MARKER), step):
            yield MARKER[i:i + step]

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.invocations += 1
        ordinal = self.invocations
        saw_tool_result = False
        try:
            for m in (messages or []):
                content = m.get("content") if isinstance(m, dict) else None
                if isinstance(content, str) and ECHO_RESULT in content:
                    saw_tool_result = True
        except Exception:  # noqa: BLE001
            saw_tool_result = False
        self.calls.append({"ordinal": ordinal, "saw_tool_result": saw_tool_result,
                           "message_count": len(messages or [])})

        if ordinal == 1:
            for piece in (self.visible_prefix, *self._marker_chunks()):
                if piece:
                    yield {"type": "token", "token": piece}
            yield {"type": "done", "response": self.visible_prefix + MARKER}
        else:
            yield {"type": "token", "token": SECOND_ANSWER}
            yield {"type": "done", "response": SECOND_ANSWER}


class FakeMcpTransport:
    """Callable matching `RunWorker(mcp_transport=...)`:
    `(db, user_id, tool_id, args) -> (text, is_error)`.

    It never creates or mutates approval/dispatch/run state, never calls
    `gateway.dispatch`, and fails loudly if invoked before approval consumption.
    """

    class InvokedBeforeApprovalConsumed(RuntimeError):
        pass

    class UnexpectedCall(AssertionError):
        pass

    def __init__(self, *, expected_tool_id: str = TOOL_ID,
                 expected_args: Optional[dict] = None,
                 require_started_dispatch: bool = True) -> None:
        self.expected_tool_id = expected_tool_id
        self.expected_args = dict(ECHO_ARGS if expected_args is None else expected_args)
        self.require_started_dispatch = bool(require_started_dispatch)
        self.invocations = 0
        self.calls: list = []
        #: Always 0 — evidence that this fake never drives the gateway itself.
        self._gateway_dispatch_calls = 0

    def __call__(self, db, user_id: int, tool_id: str, args: Any):
        if self.require_started_dispatch:
            started = int(db.execute(_sql_text(
                "SELECT COUNT(*) FROM dispatch_records WHERE outcome = 'started'"
            )).scalar() or 0)
            pending = int(db.execute(_sql_text(
                "SELECT COUNT(*) FROM pending_approvals WHERE state = 'requested'"
            )).scalar() or 0)
            if started < 1 or pending > 0:
                raise FakeMcpTransport.InvokedBeforeApprovalConsumed(
                    "fake MCP transport invoked before approval consumption"
                )
        if tool_id != self.expected_tool_id:
            raise FakeMcpTransport.UnexpectedCall("unexpected tool_id")
        if dict(args) != self.expected_args:
            raise FakeMcpTransport.UnexpectedCall("unexpected normalized args")

        self.invocations += 1
        self.calls.append({"ordinal": self.invocations, "tool_id": tool_id,
                           "args_digest": _safe_args_digest(args), "user_id": int(user_id)})
        return (ECHO_RESULT, False)

    def safe_metadata(self) -> dict:
        """Non-secret summary for evidence reporting."""
        return {"invocations": self.invocations, "calls": list(self.calls),
                "gateway_dispatch_calls": self._gateway_dispatch_calls}


__all__ = ["CANARY", "TOOL_ID", "TOOL_VERSION", "ECHO_SCHEMA", "ECHO_ARGS",
           "ECHO_RESULT", "MARKER", "VISIBLE_PREFIX", "SECOND_ANSWER",
           "MarkerProvider", "FakeMcpTransport", "seed_trusted_echo_tool"]
