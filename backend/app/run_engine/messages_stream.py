"""Durable `/messages/stream` backend cutover adapter (Phase 1B.2R — MP-3C3B).

Backend-only, feature-flagged (`run_messages_stream_durable_enabled`, OFF by
default). When on, `POST /api/sessions/{id}/messages/stream`:

* fails closed (bounded 503) BEFORE any persistence unless every durable
  dependency is satisfied and the embedded `RunWorkerSupervisor` is live/ready;
* refuses (bounded 422) request modes the durable engine cannot faithfully
  serve — images, deep research, multi-agent — BEFORE any Run/Message is created;
* submits (or idempotently reuses) an MP-3A `chat_stream` run — resolving
  provider / model / mode / system prompt server-side via the approved
  `build_chat_submission` + `create_chat_run` path; the route calls **no**
  executor (LLM / MCP / shell / …);
* streams the run's **persisted `run_events`** (the DB is the sole source of
  truth), adapting each event into the *exact legacy SSE wire format*
  (`event: <name>\ndata: <json>\n\n` via `streaming_service.format_sse`), plus a
  standard, parser-ignored `id: <run_seq>` line for `Last-Event-ID` replay and an
  `X-Run-Id` response header for the server-owned run identity.

The legacy path is untouched when the flag is off.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Message, Run, RunEvent, RunStep, ResearchSession
from app.run_engine import chat_stream, submission as _sub
from app.run_engine.context import AuthContext
from app.run_engine.engine import (  # bounded key check + read-only idem lookup
    _normalize_key, _select_by_idem_scope,
)
from app.run_engine.errors import (
    IdempotencyKeyConflict, IdempotencyKeyTooLong, RunNotFound,
    SessionBindingMismatch, SessionBindingRequired,
)
from app.run_engine.events import read_run_events
from app.run_engine.notifier import run_notifier
from app.run_engine.session_binding import validate_session_binding
from app.run_engine.state_machine import RUN_TERMINAL, WAITING_FOR_APPROVAL
from app.run_engine.sse import format_sse  # MP-4B: canonical neutral serializer

logger = logging.getLogger(__name__)


class DurableStreamError(Exception):
    """Bounded, user-safe route failure. ``detail`` never carries operational
    internals / secrets / provider text."""

    def __init__(self, http_status: int, detail: str) -> None:
        super().__init__(detail)
        self.http_status = http_status
        self.detail = detail


# ── dependency + mode gates (fail closed BEFORE persistence) ──────────


def assert_durable_stream_ready(supervisor) -> None:
    """Raise :class:`DurableStreamError` (bounded 503) unless every durable
    dependency is on AND the embedded worker is live/ready. Leaks no
    operational detail."""
    if not (settings.run_engine_enabled and settings.run_worker_enabled
            and settings.run_chat_stream_enabled):
        raise DurableStreamError(503, "Durable streaming is not available.")
    if not settings.run_public_api_enabled:
        raise DurableStreamError(503, "Durable streaming is not available.")
    try:
        _sub.assert_hmac_key_configured()
        _sub.assert_chat_stream_executable()
    except (_sub.RunFeatureMisconfigured, _sub.RunFeatureNotExecutable):
        raise DurableStreamError(503, "Durable streaming is not available.")
    if supervisor is None or not supervisor.is_durable_path_ready():
        raise DurableStreamError(503, "Durable streaming is temporarily unavailable.")


# ── server-owned run mode for this route (MP-3C3D1) ────────────────
#
# `/messages/stream`'s request body (`ChatRequest`) has no field a client can
# use to select a run mode — every request this route accepts executes as
# plain chat. `DURABLE_STREAM_MODE` is therefore the single server-owned
# source of truth for the mode this route submits, shared by the
# unsupported-mode gate below AND `resolve_and_submit`'s submission, so the
# two can never drift. `submission.py`'s `_ALLOWED_MODES` is the durable
# engine's full future vocabulary (chat / multi_agent / deep_research); this
# route's OWN business rule — which of those it can actually serve today — is
# the narrower `_SUPPORTED_DURABLE_MODES` below. Routing must never consult
# `settings.enable_deep_research` / `settings.enable_multi_agent`: those
# global flags describe what the LEGACY pipeline is configured to do, not
# what THIS durable request asked for — gating on them rejects plain-chat
# traffic for a reason unrelated to the request (MP-3C3D0 finding).
DURABLE_STREAM_MODE = "chat"
_SUPPORTED_DURABLE_MODES = frozenset({DURABLE_STREAM_MODE})


def unsupported_mode_reason(mode: str) -> Optional[str]:
    """Mode-only check against the durable engine's executable-mode set
    (``DURABLE_STREAM_MODE`` / ``_SUPPORTED_DURABLE_MODES``) — the canonical
    definition every durable submission route reuses (``/messages/stream``
    via :func:`unsupported_mode_code` below, and the direct Run public API,
    ``routes/runs.py``, which has its own separate image-handling posture and
    does not go through the ``has_image`` check). ``mode`` has already been
    validated/normalized by ``submission.validate_request_mode`` (or defaults
    to ``DURABLE_STREAM_MODE``) — this only decides EXECUTABILITY, not
    well-formedness. Returns a bounded, sanitized slug (never the raw value
    verbatim, even though ``mode`` is already known-safe by this point —
    stays defensive) or ``None`` when supported.

    Phase 1B.2R MP-3C3D4C — ``"deep_research"`` is a SPECIAL case, not a
    static member of ``_SUPPORTED_DURABLE_MODES`` (that frozenset stays a
    fixed, always-true set — "chat" is always executable regardless of
    config; deep_research's executability is config-dependent, computed
    fresh on every call via the ONE authoritative
    ``deep_research_readiness.deep_research_executable()`` predicate, never
    a second/duplicated readiness check)."""
    normalized = (mode or DURABLE_STREAM_MODE).strip().lower()
    if normalized == "deep_research":
        from app.run_engine.deep_research_readiness import deep_research_unavailable_reason

        return deep_research_unavailable_reason()
    # Phase 1B.2R MP-3C3D5C — same shape as deep_research immediately above:
    # multi_agent's executability is config-AND-implementation-dependent,
    # computed fresh via the ONE authoritative
    # multi_agent_readiness.multi_agent_executable() predicate, never a
    # second/duplicated readiness check.
    if normalized == "multi_agent":
        from app.run_engine.multi_agent_readiness import multi_agent_unavailable_reason

        return multi_agent_unavailable_reason()
    if normalized not in _SUPPORTED_DURABLE_MODES:
        # bounded, alnum/underscore-only slug — defensive even though `mode`
        # is already validated/canonical by the time this runs.
        safe = "".join(c for c in normalized if c.isalnum() or c == "_")[:32] or "unknown"
        return f"mode_unsupported_{safe}"
    return None


def unsupported_mode_code(*, has_image: bool, mode: str = DURABLE_STREAM_MODE) -> Optional[str]:
    """A bounded slug for a ``/messages/stream`` request the durable engine
    cannot faithfully serve, or ``None``. Checked BEFORE any run is created.
    ``mode`` is the request's own VALIDATED canonical mode (MP-3C3D3A —
    ``routes/messages.py`` resolves it via ``submission.validate_request_mode``
    before calling this; an omitted request mode resolves to
    ``DURABLE_STREAM_MODE``). The user-facing message is generic; the
    specific reason is logged, not returned."""
    if has_image:
        return "mode_unsupported_image"
    return unsupported_mode_reason(mode)


_UNSUPPORTED_DETAIL = ("Durable streaming currently supports plain chat and "
                       "tool-approval conversations only.")


def valid_idempotency_key(raw: Optional[str]) -> bool:
    if not raw:
        return False
    try:
        _normalize_key(raw)            # reuses MP-1A charset/length rules
        return True
    except Exception:  # noqa: BLE001
        return False


# ── MP-3C3D2 — durable context enrichment (RAG + memory) ─────────────
#
# Reuses the exact same plain-chat retrieval services the legacy path already
# calls (rag_service, memory_service) — no hybrid RAG, no new vector store, no
# new LLM call (rag_service embeds the query via the same local embedding
# model legacy already uses; nothing here calls a chat/completion model).
# Bounded and non-fatal on any failure, matching legacy's own "retrieval
# failure never breaks the chat" posture. Retrieved text is untrusted DATA —
# it is never appended to the system prompt (unlike legacy); it reaches the
# provider only via chat_stream.render_context_envelope's delimited untrusted
# user-role message, matching the MP-3C2 tool-result convention.


def _select_cited_chunks(chunks: list) -> tuple:
    """The single source of the passage/citation one-to-one guarantee: build
    a citation for the (already-bounded, ``rag_service.TOP_K``-capped)
    retrieved-chunk list, keep ONLY the chunks whose citation actually
    validates against the canonical Citation schema, and dedupe by
    ``(document_id, chunk_id)`` — all BEFORE any prompt text is rendered.
    Returns ``(kept_chunks, )`` — the caller re-renders ``rag_context_text``
    and the final citation list from this SAME filtered list (via
    ``rag_service.format_rag_context``/``build_citation_list``), so a chunk
    can never reach the provider without a corresponding citation, and a
    citation can never be dropped later while its passage text survives."""
    from app.services.rag_service import build_citation_list

    raw_citations = build_citation_list(chunks)
    kept: list = []
    seen: set = set()
    for chunk, cite in zip(chunks, raw_citations):
        validated = chat_stream._validate_one_citation(cite)
        if validated is None:
            continue
        key = (validated["document_id"], validated["chunk_id"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(chunk)
    return (kept,)


def _build_context_snapshot(db: Session, *, user_id: int, session_id: int,
                            message: str) -> dict:
    """Bounded, session/user-scoped retrieval snapshot. The caller
    (``resolve_and_submit``) invokes this ONLY for a genuinely fresh
    submission — never on a replay/reconnect — and ONLY before opening
    ``submission.create_chat_run``'s atomic staging area, because
    ``memory_service.retrieve_relevant_memories`` commits internally
    (access-metric bookkeeping) and calling it once any Run/Message row is
    already staged-but-uncommitted would force-commit that partial state.
    Returns a versioned snapshot (``chat_stream.build_context_snapshot``);
    every field stays honestly empty/false on retrieval failure or when
    nothing is retrievable. Logs a bounded exception CLASS NAME only — never
    the raw exception text, which could echo query/document content."""
    rag_context_text: Optional[str] = None
    citations: list = []
    sources_used = False
    memory_context_text: Optional[str] = None
    memories_used = False
    transcript_context_text: Optional[str] = None
    transcript_status: Optional[str] = None

    try:
        from app.services.rag_service import build_citation_list, format_rag_context, retrieve_chunks

        # session-scoped by construction: retrieve_chunks only ever considers
        # this session's own documents — no cross-session/cross-user reach.
        # Already bounded to rag_service.TOP_K by retrieve_chunks itself.
        chunks = retrieve_chunks(session_id, message, db)
        if chunks:
            (kept_chunks,) = _select_cited_chunks(chunks)
            if kept_chunks:
                # re-render BOTH from the SAME filtered set — markers stay
                # contiguous [1..len(kept_chunks)] and correspond exactly.
                rag_context_text = format_rag_context(kept_chunks)
                citations = build_citation_list(kept_chunks)
                sources_used = True
    except Exception as exc:  # noqa: BLE001 — retrieval must never block submission
        logger.warning("durable context: RAG retrieval failed (non-fatal): %s",
                       type(exc).__name__)

    try:
        from app.services.memory_service import (
            format_memories_for_prompt, get_memory_enabled, retrieve_relevant_memories,
        )
        if get_memory_enabled(db):
            # user-scoped by construction: retrieve_relevant_memories filters
            # on Memory.user_id == user_id — no cross-user reach.
            memories = retrieve_relevant_memories(db, user_id=user_id)
            if memories:
                memory_context_text = format_memories_for_prompt(memories)
                memories_used = True
    except Exception as exc:  # noqa: BLE001 — retrieval must never block submission
        logger.warning("durable context: memory retrieval failed (non-fatal): %s",
                       type(exc).__name__)

    # MP-YouTube: a supported YouTube URL pasted INTO the message is resolved
    # into a bounded, timestamped transcript block that travels the SAME
    # untrusted-context channel as retrieval (see
    # chat_stream.render_context_envelope) - never the system prompt, never
    # instructions. This runs at the same already-established point in the
    # lifecycle that retrieval does (fresh submission only, before
    # create_chat_run opens its atomic staging area), because it performs
    # bounded NETWORK I/O. It reuses app.tools.youtube_transcript_context
    # wholesale: one URL parser, one fetch implementation, no second download
    # path. Zero writes of any kind - no memory, no knowledge graph, no
    # Message row - and non-fatal by design. The exception CLASS NAME only is
    # logged, exactly like the two retrievals above.
    try:
        from app.tools.youtube_transcript_context import resolve_message_transcript

        resolved = resolve_message_transcript(message)
        if resolved is not None:
            transcript_context_text = resolved.text
            transcript_status = resolved.status
    except Exception as exc:  # noqa: BLE001 - a transcript must never block chat
        logger.warning("durable context: YouTube transcript resolution failed (non-fatal): %s",
                       type(exc).__name__)

    # YouTube Knowledge Ingestion (Phase 1). Runs at the SAME already-
    # established pre-staging point as retrieval and transcript resolution
    # above: a fresh submission only, BEFORE create_chat_run opens its atomic
    # staging area, because it performs bounded network I/O (transcript +
    # oEmbed) and a model call. It is the ONLY writer here.
    #
    # CONSENT: a pasted link is an explicit user-initiated request, so the
    # ingest writes DIRECTLY (the same basis the pre-existing explicit
    # memory_service.save_memory path uses). It NEVER creates, decides or
    # bypasses an approval, and it does not touch the model-initiated
    # [SAVE_MEMORY]/[SAVE_DIRECTIVE] path, which stays approval-gated in
    # app.run_engine.worker. Flag OFF -> ingest_message_youtube returns None
    # before any fetch, so behaviour is byte-for-byte the legacy behaviour.
    #
    # Fail-closed: the service never raises - every failure is a bounded status
    # + notice, and chat continues normally. The exception CLASS NAME only is
    # logged, never a message that could quote transcript text.
    youtube_ingest_notice = None
    try:
        from app.services.youtube_ingest_service import (
            context_line, ingest_message_youtube,
        )

        ingest = ingest_message_youtube(
            db, user_id=user_id, session_id=session_id, message=message)
        if ingest is not None:
            youtube_ingest_notice = context_line(ingest)
    except Exception as exc:  # noqa: BLE001 - ingest must never block chat
        logger.warning("durable context: YouTube ingest failed (non-fatal): %s",
                       type(exc).__name__)

    return chat_stream.build_context_snapshot(
        rag_context_text=rag_context_text, citations=citations, sources_used=sources_used,
        memory_context_text=memory_context_text, memories_used=memories_used,
        transcript_context_text=transcript_context_text,
        transcript_status=transcript_status,
        youtube_ingest_notice=youtube_ingest_notice,
    )


# ── idempotent server-resolved submission (NO executor) ──────────────


def resolve_and_submit(db: Session, ctx: AuthContext, *, session_id: int,
                       message: str, idempotency_key: str,
                       mode: Optional[str] = None) -> tuple[Run, bool]:
    """Server-resolve provider / model / mode / system prompt and create — or
    idempotently reuse — the durable run (``chat_stream`` or, MP-3C3D4C,
    ``deep_research``). Never accepts a client run_id / turn_id / hash /
    dedupe key / provider identity. Raises :class:`DurableStreamError`.

    ``mode`` (MP-3C3D4C): the route's own already-validated resolved mode —
    ``None``/omitted preserves the EXACT original behavior (defaults to
    ``DURABLE_STREAM_MODE``, i.e. "chat"). The caller (``routes/messages.py``)
    has ALREADY rejected an unsupported mode via ``unsupported_mode_code``
    before this function is ever called."""
    sess = (db.query(ResearchSession)
            .filter(ResearchSession.id == session_id,
                    ResearchSession.user_id == ctx.user_id).first())
    if sess is None:
        raise DurableStreamError(404, "Session not found.")

    try:
        from app.services.settings_service import get_user_llm_config
        resolved_provider = (get_user_llm_config(db, ctx.user_id) or {}).get("provider")
    except Exception:  # noqa: BLE001
        resolved_provider = None
    resolved_provider = resolved_provider or settings.llm_provider

    try:
        submission = _sub.build_chat_submission(
            session_id=session_id, message=message, image_url=None,
            provider=resolved_provider, model=sess.model,
            mode=(mode or DURABLE_STREAM_MODE),
            system_prompt=sess.system_prompt, options={},
        )
    except ValueError as exc:
        raise DurableStreamError(422, str(exc)[:200])

    # MP-3C3D2 — authoritative replay/conflict pre-check, BEFORE retrieval and
    # BEFORE create_chat_run stages anything. Reuses the EXACT same scoped
    # lookup submit_run() itself uses (no second, drifting identity
    # implementation) purely to decide whether this is a genuinely fresh
    # submission. A reconnect/replay (existing row found — matching OR
    # conflicting identity, doesn't matter here) skips retrieval entirely:
    # create_chat_run will return the existing run or raise the deterministic
    # 409 on its own. Only a fresh submission computes context, and it does so
    # BEFORE create_chat_run opens its atomic staging area — retrieval (via
    # memory_service, which commits internally) must never run once a
    # Message/Run row is staged-but-uncommitted in this session, or a
    # downstream failure could leave a partially-committed submission (see
    # the MP-3C3D2 completion report for the exact regression this caused and
    # how `test_submission_commit_failure_no_partial` caught it).
    #
    # The pre-check itself failing (a real DB error, not merely "nothing
    # found") is NOT treated as "fresh" — this is an authoritative decision
    # this function is responsible for, not a best-effort optimization: fail
    # closed BEFORE any retrieval and BEFORE any row is staged. Never expose
    # the raw DB/exception text; log only a bounded exception class name.
    try:
        existing = _select_by_idem_scope(
            db, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
            session_id=session_id, idempotency_key=_normalize_key(idempotency_key),
        )
    except Exception as exc:  # noqa: BLE001 — fail closed, never guess "fresh"
        logger.warning("durable submission: idempotency pre-check failed: %s",
                       type(exc).__name__)
        raise DurableStreamError(503, "Durable streaming is temporarily unavailable.")

    context: Optional[dict] = None
    if existing is None:
        context = _build_context_snapshot(
            db, user_id=ctx.user_id, session_id=session_id, message=message)

    try:
        return _sub.create_chat_run(
            db, ctx, submission=submission, idempotency_key=idempotency_key,
            context=context,
        )
    except _sub.MultiAgentActiveRunLimitExceeded:
        raise DurableStreamError(429, "Too many active multi-agent runs.")
    except IdempotencyKeyConflict:
        raise DurableStreamError(409, "Idempotency-Key reused for a different request.")
    except IdempotencyKeyTooLong:
        raise DurableStreamError(422, "Idempotency-Key is too long.")
    except SessionBindingRequired:
        raise DurableStreamError(401, "Fresh authentication is required.")
    except (SessionBindingMismatch, RunNotFound):
        raise DurableStreamError(404, "Not found.")


# ── per-user concurrent-stream + submit-rate guards (in-process) ─────


_slots_lock = threading.Lock()
_active_slots: dict[int, int] = {}


def acquire_stream_slot(user_id: int) -> bool:
    cap = max(1, int(settings.run_messages_stream_max_concurrent_per_user))
    with _slots_lock:
        n = _active_slots.get(user_id, 0)
        if n >= cap:
            return False
        _active_slots[user_id] = n + 1
        return True


def release_stream_slot(user_id: int) -> None:
    with _slots_lock:
        n = _active_slots.get(user_id, 0)
        if n <= 1:
            _active_slots.pop(user_id, None)
        else:
            _active_slots[user_id] = n - 1


def submit_rate_limited(user_id: int) -> bool:
    try:
        from app.services.rate_limiter import get_rate_limiter
        per_min = max(1, int(settings.run_messages_stream_submits_per_min))
        return get_rate_limiter().is_rate_limited(
            f"durable_msg_stream:{int(user_id)}", per_min, 60)
    except Exception:  # noqa: BLE001 — a limiter failure must not open the gate wide,
        return False   # but also must not hard-block; conservative: allow, logged upstream


def parse_cursor(last_event_id: Optional[str], after_seq: Optional[str]) -> int:
    for raw in (after_seq, last_event_id):
        if raw is None:
            continue
        try:
            v = int(str(raw).strip())
            if v >= 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0


# ── durable events → legacy SSE wire adapter ────────────────────────


class _sess_cm:
    def __init__(self, factory: Callable[[], Session]):
        self._f, self._db = factory, None

    def __enter__(self) -> Session:
        self._db = self._f()
        return self._db

    def __exit__(self, *exc) -> None:
        if self._db is not None:
            self._db.close()


def _final_completion_fields(db: Session, run_id: str) -> dict:
    """The committed finalization step's non-secret ``output_ref`` fields —
    ``message_id`` plus (MP-3C3D2) the ``sources_used``/``memories_used``
    booleans the worker wrote atomically with the ``Message``. Never
    re-derived: this reads exactly what finalization committed, so reconnect/
    replay returns identical completion metadata every time."""
    fields = {"message_id": 0, "sources_used": False, "memories_used": False}
    for s in (db.query(RunStep).filter(RunStep.run_id == run_id)
              .order_by(RunStep.seq.asc()).all()):
        try:
            ref = json.loads(s.output_ref or "{}")
        except (ValueError, TypeError):
            continue
        mid = ref.get("message_id")
        if isinstance(mid, int):
            fields["message_id"] = mid
            fields["sources_used"] = bool(ref.get("sources_used"))
            fields["memories_used"] = bool(ref.get("memories_used"))
    return fields


def _final_citations(db: Session, message_id: int) -> list:
    """The committed ``Message.citations`` for the finalized turn — the SAME
    validated citations sealed atomically with the Message, never
    re-computed. Empty list on any missing/unreadable row (honest, not a
    fabricated success)."""
    if not message_id:
        return []
    msg = db.query(Message).filter(Message.id == message_id).first()
    if msg is None or not msg.citations:
        return []
    try:
        parsed = json.loads(msg.citations)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _approval_view(db: Session, run: Optional[Run]) -> dict:
    """Minimal, redacted approval hint — NO tool args, server env, marker text,
    action hash, or session-family id. The full object comes from
    ``GET /api/approvals/{id}``."""
    out: dict = {}
    if run is not None and run.pending_approval_id:
        out["approval_id"] = run.pending_approval_id
        step = (db.query(RunStep)
                .filter(RunStep.run_id == run.id, RunStep.kind == "tool_dispatch",
                        RunStep.approval_id == run.pending_approval_id).first())
        if step is not None:
            try:
                tid = json.loads(step.input_ref or "{}").get("tool_id")
            except (ValueError, TypeError):
                tid = None
            if isinstance(tid, str):
                out["tool_id"] = tid          # server-owned non-secret id (as in MP-2 refs)
    return out


def _latest_park_seq(db: Session, run_id: str) -> Optional[int]:
    """`run_seq` of the MOST RECENT `run.status -> waiting_for_approval` event.

    A Run can park more than once (approve #1 -> execute -> the model proposes
    again -> park #2). `Run.pending_approval_id` always names the CURRENT bound
    approval, so on a replay from `after_seq=0` only the LATEST park transition
    describes the approval that is actionable now; every earlier park would emit a
    duplicate `tool_approval_required` for that same approval.
    """
    rows = (db.query(RunEvent.run_seq, RunEvent.data)
            .filter(RunEvent.run_id == run_id, RunEvent.category == "control",
                    RunEvent.type == "run.status")
            .order_by(RunEvent.run_seq.desc()).all())
    for seq, data in rows:
        try:
            if json.loads(data or "{}").get("to_status") == "waiting_for_approval":
                return int(seq)
        except (ValueError, TypeError):
            continue
    return None


_FAIL_DETAIL = {
    "provider_error": "The model provider returned an error.",
    "provider_rate_limited": ("The model provider is rate limiting requests. "
                              "Please try again in a moment."),
    "provider_timeout": "The response timed out.",
    "empty_generation": "The model returned no text.",
    "chat_input_error": "The request could not be prepared.",
    "tool_resume_required": "This conversation requires a tool the durable path cannot run.",
    "truncated_tool_marker": "The response ended unexpectedly.",
    "tool_marker_malformed": "The response ended unexpectedly.",
    "tool_marker_multiple": "The response ended unexpectedly.",
    "tool_marker_oversized": "The response ended unexpectedly.",
    "tool_marker_incomplete": "The response ended unexpectedly.",
    "tool_cycle_limit": "The conversation reached its tool-use limit.",
    "output_persist_failed": "The response could not be saved.",
    "session_revoked": "Your session is no longer valid.",
    "nonexec_marker_deferred": ("This conversation requires a feature the durable "
                                "path does not support yet."),
    "context_snapshot_unreadable": "The response could not be completed.",
    # MP-4C decision 2 - bounded save outcomes. Never carries the content.
    "save_marker_malformed": "The response ended unexpectedly.",
    "save_blocked_sensitive": ("That content was not saved because it looks "
                               "sensitive."),
    "save_content_too_large": "That content was not saved because it is too long.",
    # MP-4C decision 2 follow-up - duplicate-marker suppression notices. Exact
    # fixed templates; they never echo the content or the prior proposal.
    "save_duplicate_approved": ("A proposal with this exact content was already "
                                "approved and saved. No new action taken."),
    "save_duplicate_denied": ("A proposal with this exact content was denied. "
                              "Unchanged content will not be re-proposed."),
    "save_duplicate_expired": ("A proposal with this exact content expired. "
                               "Unchanged content will not be re-proposed."),
    "save_duplicate_unresolved": ("A proposal for this content is unresolved or "
                                  "previously failed. No new proposal created."),
    "approval_expired": ("The approval request expired before it was decided; "
                         "nothing was executed or saved."),
}


def _bound_approval_is_save(db: Session, run: Run) -> bool:
    """True iff the RUN-BOUND approval names one of the save tools. Bounded,
    non-disclosing: reads the server-owned tool id only, never the content."""
    if run is None or not run.pending_approval_id:
        return False
    try:
        from app.models.models import PendingApproval
        from app.run_engine.save_approval_identity import SAVE_TOOL_IDS
        row = (db.query(PendingApproval)
               .filter(PendingApproval.id == run.pending_approval_id).first())
        return row is not None and row.tool_id in SAVE_TOOL_IDS
    except Exception:  # noqa: BLE001 - a lookup failure never changes the outcome
        return False


def _terminal_frame(db: Session, run: Run) -> Optional[str]:
    st = run.status
    if st == "succeeded":
        fields = _final_completion_fields(db, run.id)
        return format_sse("complete", {
            "message_id": fields["message_id"],
            "citations": _final_citations(db, fields["message_id"]),
            "sources_used": fields["sources_used"],
            "memories_used": fields["memories_used"],
        })
    if st == "denied":
        detail = ("The save was denied. Nothing was written."
                  if _bound_approval_is_save(db, run)
                  else "The requested tool call was denied.")
        return format_sse("error", {"code": "approval_denied", "detail": detail})
    if st in ("cancelled", "cancelling"):
        return format_sse("cancelled", {"detail": "The run was cancelled."})
    if st == "timed_out":
        return format_sse("error", {"code": "timed_out",
                                    "detail": "The response timed out."})
    if st == "outcome_unknown":
        return format_sse("error", {"code": "outcome_unknown",
                                    "detail": "The run ended in an indeterminate state."})
    if st == "failed":
        code = (run.status_reason or "run_failed")[:48]
        return format_sse("error", {"code": code,
                                    "detail": _FAIL_DETAIL.get(code,
                                                               "The response could not be completed.")})
    return None


def _map_event(db: Session, run: Optional[Run], ev) -> tuple[Optional[str], bool]:
    """(legacy-wire frame or None, is_terminal). Never emits marker / arg /
    result / provider text."""
    if ev.category == "text":
        try:
            txt = json.loads(ev.data or "{}").get("text") or ""
        except (ValueError, TypeError):
            txt = ""
        return (format_sse("token", {"token": txt}) if txt else None, False)
    if ev.category == "control" and ev.type == "run.status":
        try:
            to = json.loads(ev.data or "{}").get("to_status")
        except (ValueError, TypeError):
            to = None
        if to == "waiting_for_approval":
            # A REPLAY (`GET /api/runs/{run_id}/events` from `after_seq=0`) re-walks
            # this HISTORICAL transition. Only a run that is genuinely waiting RIGHT
            # NOW may surface an approval: once the run has moved on (approved,
            # resuming, running, denied, failed, succeeded) the retained
            # `Run.pending_approval_id` is the run BINDING IDENTITY, not a live
            # pending state, so re-emitting it would re-open the dialog for an
            # already-decided (possibly consumed) approval.
            if run is None or run.status != WAITING_FOR_APPROVAL:
                return (None, False)
            # ...and only the park that is CURRENT right now may present it. A
            # superseded earlier park would emit a DUPLICATE frame for the same
            # (current) approval; the actionable approval is presented exactly once.
            if getattr(ev, "run_seq", None) != _latest_park_seq(db, run.id):
                return (None, False)
            return (format_sse("tool_approval_required", _approval_view(db, run)), False)
        if to in RUN_TERMINAL and run is not None:
            return (_terminal_frame(db, run), True)
        if to in ("running", "resuming", "approved", "queued", "cancelling"):
            return (format_sse("run_status", {"status": to}), False)
    # Phase 1B.2R MP-3C3D4C — bounded deep-research progress. `code` is
    # already schema-validated (events.CONTROL_EVENT_SCHEMAS) to the closed
    # set before this event was ever persisted; forwarded as-is, no raw
    # query/URL/title/content ever reaches this event's `data` in the first
    # place. An older/unrecognized frontend build simply ignores an unknown
    # SSE event name (existing `useStreaming.ts` `default:` behavior) — this
    # is additive-safe with zero required frontend change.
    if ev.category == "control" and ev.type == "research.progress":
        try:
            code = json.loads(ev.data or "{}").get("code")
        except (ValueError, TypeError):
            code = None
        if isinstance(code, str):
            return (format_sse("research_progress", {"code": code}), False)
    # Phase 1B.2R MP-3C3D5C — bounded multi-agent progress, same shape as
    # research.progress immediately above: `code` is already schema-validated
    # (events.CONTROL_EVENT_SCHEMAS["agent.progress"]) to the closed
    # multi_agent_orchestration.AGENT_PROGRESS_CODES set before persistence;
    # forwarded as-is. No role/content/citation/exception ever reaches this
    # event's `data`. An older/unrecognized frontend build simply ignores an
    # unknown SSE event name (existing `useStreaming.ts` `default:` behavior).
    if ev.category == "control" and ev.type == "agent.progress":
        try:
            code = json.loads(ev.data or "{}").get("code")
        except (ValueError, TypeError):
            code = None
        if isinstance(code, str):
            return (format_sse("agent_progress", {"code": code}), False)
    return (None, False)


async def stream_legacy_wire(
    request, session_factory: Callable[[], Session], ctx: AuthContext, run_id: str,
    *, after_seq: int = 0, now_fn: Optional[Callable[[], datetime]] = None,
) -> AsyncIterator[str]:
    """Yield legacy-wire SSE frames for ``run_id`` from its persisted events,
    strictly after ``after_seq``. DB is authoritative; the notifier only shortens
    the wait. Disconnect closes the connection ONLY — it never cancels the run."""
    import asyncio

    now = now_fn or (lambda: datetime.now(timezone.utc))
    cursor = max(0, int(after_seq or 0))
    page = int(settings.run_event_read_max_page)
    poll = max(1, int(settings.run_sse_poll_interval_s))
    beat_every = max(0, int(settings.run_sse_heartbeat_s))
    max_secs = int(settings.run_sse_max_seconds)
    started = now()
    last_beat = started
    ev_wait = run_notifier.subscribe(run_id)
    try:
        if cursor == 0:
            yield format_sse("start", {"run_id": run_id})
        while True:
            if await request.is_disconnected():
                return
            if (now() - started).total_seconds() > max_secs:
                yield ": stream-max-duration\n\n"
                return

            emitted_terminal = False
            with _sess_cm(session_factory) as db:
                run = db.query(Run).filter(Run.id == run_id).first()
                try:
                    validate_session_binding(db, ctx, run=run)
                except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                    yield format_sse("error", {"code": "stream_closed",
                                               "detail": "The stream was closed."})
                    return
                rows = read_run_events(db, ctx, run_id, after_seq=cursor, limit=page)
                for r in rows:
                    frame, is_terminal = _map_event(db, run, r)
                    cursor = r.run_seq
                    if frame:
                        yield f"id: {r.run_seq}\n" + frame
                    if is_terminal:
                        emitted_terminal = True
                        break
                caught_up = len(rows) < page
                # reconnect past a terminal run.status event: synthesize the
                # terminal frame so the client stops cleanly.
                if (not emitted_terminal and caught_up and run is not None
                        and run.status in RUN_TERMINAL):
                    tf = _terminal_frame(db, run)
                    if tf:
                        yield tf
                    emitted_terminal = True

            if emitted_terminal:
                return
            if not rows:
                if beat_every == 0 or (now() - last_beat).total_seconds() >= beat_every:
                    yield ": heartbeat\n\n"
                    last_beat = now()
                try:
                    await asyncio.wait_for(ev_wait.wait(), timeout=poll)
                except asyncio.TimeoutError:
                    pass
                ev_wait.clear()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — redact
        logger.error("durable messages stream error: %s", "internal")
        try:
            yield format_sse("error", {"code": "internal_error",
                                       "detail": "An unexpected error occurred."})
        except Exception:  # pragma: no cover
            pass
    finally:
        run_notifier.unsubscribe(run_id, ev_wait)


__all__ = [
    "DurableStreamError", "assert_durable_stream_ready", "unsupported_mode_code",
    "unsupported_mode_reason",
    "DURABLE_STREAM_MODE", "valid_idempotency_key", "resolve_and_submit",
    "acquire_stream_slot", "release_stream_slot", "submit_rate_limited", "parse_cursor",
    "stream_legacy_wire",
]
