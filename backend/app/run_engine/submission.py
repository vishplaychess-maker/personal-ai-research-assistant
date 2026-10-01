"""Versioned canonical chat-run submission + dedicated semantic-idempotency HMAC
+ atomic submission persistence (Phase 1B.2R MP-3A).

* **Semantic identity** — a server-computed HMAC-SHA256 over the *canonical
  validated submission* (every behaviour-changing field, including
  server-resolved model / mode / system-prompt), keyed by
  ``RUN_IDEMPOTENCY_HMAC_KEY`` and domain-separated by
  ``thunder-ai/run-semantic-identity/v1``. Same scoped ``Idempotency-Key`` +
  same semantic submission ⇒ the same run; a changed prompt / model / mode /
  option ⇒ a deterministic ``IdempotencyKeyConflict``.
* The key is **env-only** (never a Settings field), has **no default**, is
  required only when ``run_public_api_enabled``, fails closed when missing, and
  is never returned or logged. Client fingerprints are never trusted; no raw
  prompt and no *unkeyed* prompt digest is placed in identity metadata.
* Actual submission content is persisted only via the existing ``Message``
  privacy model **and** a context-bound encrypted :class:`RunPayload` — never a
  third plaintext copy, never in ``run_steps.input_ref`` (ids only).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.approval.canonical import canonicalize
from app.config import settings
from app.models.models import Message, MessageRole, Run, RunStep, User, _utcnow_aware
from app.run_engine.context import AuthContext
from app.run_engine import save_markers as _save_markers
from app.run_engine.engine import _normalize_key, _select_by_idem_scope, submit_run, transition_run
from app.run_engine.errors import RunEngineError
from app.run_engine.events import _try_append_event, validate_event_data
from app.run_engine.multi_agent_orchestration import MULTI_AGENT_RUN_KIND
from app.run_engine.payloads import seal_payload
from app.run_engine.session_binding import validate_session_binding
from app.run_engine.state_machine import STEP_PENDING, STEP_SKIPPED
from app.run_engine.state_machine import RUN_TERMINAL

CONTRACT_VERSION = "chat-run/v2"  # v2 (MP-3C1): +server-resolved provider in the identity
_SEMANTIC_DOMAIN = b"thunder-ai/run-semantic-identity/v1"
_SYSPROMPT_DOMAIN = b"thunder-ai/run-system-prompt/v1"
_ALLOWED_MODES = frozenset({"chat", "multi_agent", "deep_research"})
_MESSAGE_MAX = 10_000


class RunFeatureDisabled(RunEngineError):
    """The durable-run public API is disabled (``run_public_api_enabled`` off).
    Routes surface this as a non-disclosing 404."""


class RunFeatureMisconfigured(RunEngineError):
    """The durable-run public API is enabled but ``RUN_IDEMPOTENCY_HMAC_KEY`` is
    not configured. Fail closed — routes surface this as 503. The message never
    contains the key."""


class RunFeatureNotExecutable(RunEngineError):
    """A ``chat_stream`` submission was requested but the durable chat driver
    cannot run it (`run_engine_enabled` / `run_chat_stream_enabled` not both on).
    Fail closed — the submit route surfaces this as 503 and NO run is created, so
    an unusable queued run is never persisted (MP-3C1)."""


class MultiAgentActiveRunLimitExceeded(RunEngineError):
    """Creating a NEW ``multi_agent`` run for this user (+tenant) would exceed
    ``settings.multi_agent_max_active_runs_per_user``. Fail closed BEFORE any
    persistence — routes surface this as a bounded 429 with no counts,
    prompts, or run ids in the response."""


# Server-owned Run.kind values by resolved mode (Phase 1B.2R MP-3C3D4C /
# MP-3C3D5C). A client never supplies this — it is derived entirely from the
# ALREADY strictly-validated `submission.resolved_mode`.
_DEEP_RESEARCH_RUN_KIND = "deep_research"


def _run_kind_for_mode(resolved_mode: str) -> str:
    if resolved_mode == "deep_research":
        return _DEEP_RESEARCH_RUN_KIND
    if resolved_mode == "multi_agent":
        return MULTI_AGENT_RUN_KIND
    return "chat_stream"


# ── HMAC key (env only; no default; never logged) ───────────────────


def _hmac_key() -> bytes:
    raw = (os.getenv("RUN_IDEMPOTENCY_HMAC_KEY") or "").strip()
    if len(raw) < 16:  # a present-but-trivial key is treated as unconfigured
        raise RunFeatureMisconfigured(
            "RUN_IDEMPOTENCY_HMAC_KEY is not configured (env-only, min 16 chars)"
        )
    return raw.encode("utf-8")


def require_run_public_api() -> None:
    """Route gate. Raises :class:`RunFeatureDisabled` (→404) when the flag is
    off, or :class:`RunFeatureMisconfigured` (→503) when it is on without the
    HMAC key."""
    if not settings.run_public_api_enabled:
        raise RunFeatureDisabled("durable run API is disabled")
    _hmac_key()


def assert_hmac_key_configured() -> None:
    """Startup check — call from lifespan when ``run_public_api_enabled``."""
    _hmac_key()


def assert_chat_stream_executable() -> None:
    """Fail closed when a ``chat_stream`` submission could never be executed —
    the durable chat driver needs BOTH ``run_engine_enabled`` and
    ``run_chat_stream_enabled``. Prevents unusable queued runs (MP-3C1)."""
    if not (settings.run_engine_enabled and settings.run_chat_stream_enabled):
        raise RunFeatureNotExecutable(
            "durable chat execution requires run_engine_enabled and run_chat_stream_enabled"
        )



# Phase 1B.2R MP-3C3D4C closeout — an earlier revision of this module
# defined a standalone ``assert_deep_research_executable()`` mirroring
# ``assert_chat_stream_executable()`` above, but it was never called from
# any route: unlike chat, deep-research readiness cannot be checked before
# the request's `mode` is known (both `/api/runs` and `/messages/stream`
# resolve `mode` from the request body itself), so there is no early,
# mode-agnostic point to call it from without duplicating exactly what
# `messages_stream.unsupported_mode_reason("deep_research")` already does
# (both delegate to the SAME `deep_research_readiness.deep_research_executable()`
# predicate). Removed rather than left as unused, unreachable code — the
# ONE authoritative, ACTUALLY-CALLED gate for both public routes is
# `messages_stream.unsupported_mode_reason`, verified reachable before any
# persistence in `routes/runs.py` (line ~135, before `create_chat_run`) and
# `routes/messages.py` (`unsupported_mode_code`, before `resolve_and_submit`).


# ── the versioned canonical submission ─────────────────────────────


@dataclass(frozen=True)
class ChatRunSubmission:
    contract_version: str
    session_id: int
    message: str
    image_url: Optional[str]
    resolved_provider: Optional[str]   # server-resolved provider NAME (never a key)
    resolved_model: Optional[str]
    resolved_mode: str
    system_prompt_hmac: Optional[str]  # keyed hash — never the raw system prompt (identity)
    resolved_system_prompt: Optional[str]  # raw — sealed in the encrypted snapshot ONLY
    options: dict                      # sorted scalar-only behaviour knobs


def validate_request_mode(raw: object) -> Optional[str]:
    """Phase 1B.2R MP-3C3D3A/closeout — the ONE authoritative PUBLIC WIRE mode
    validator. Every mode-aware public route enforces this SAME strict
    contract: ``/messages/stream`` (legacy + durable branches, directly) and
    ``/api/runs`` (via :func:`resolve_mode` below, which delegates here —
    there is no second, looser public validation path).

    STRICT, exact-match only — no case-folding, no whitespace-stripping, no
    alias resolution. (Evidence check, closeout correction: an earlier
    revision of :func:`resolve_mode` normalized case/whitespace directly and
    was called by ``/api/runs`` without going through this function at all —
    an undocumented, untested asymmetry between the two public endpoints.
    Nothing in the repository — no test, no docstring, this feature remains
    entirely uncommitted with the durable flag OFF — established that
    leniency as a real public contract, so it was removed rather than
    preserved as precedent.)

    Returns ``None`` when ``raw`` is ``None`` (omitted — allowed only where
    the calling endpoint's own contract allows omission; this function
    itself is agnostic to that). Returns ``raw`` UNCHANGED when it is
    exactly one of the canonical mode strings. Raises :class:`ValueError` —
    with a bounded, GENERIC message that never echoes ``raw`` — for any
    non-string (including bool/int/float/list/dict), empty, wrong-case,
    whitespace-padded, oversized, or unrecognized value. Reuses
    ``_ALLOWED_MODES`` — the ONE authoritative allowed-mode set; no second,
    drifting definition anywhere in the codebase."""
    if raw is None:
        return None
    # bool is a subclass of int in Python; isinstance(True, str) is already
    # False so this is redundant for bool but kept explicit for clarity.
    if not isinstance(raw, str):
        raise ValueError("mode must be a string")
    if len(raw) > 32 or raw not in _ALLOWED_MODES:
        raise ValueError("unrecognized mode")
    return raw


def resolve_mode(raw: Optional[str]) -> str:
    """Resolve the mode for INTERNAL/durable use (the execution snapshot +
    semantic identity): an omitted value defaults to ``"chat"`` — every
    durable submission needs a concrete, non-``None`` resolved mode.
    Well-formedness is delegated entirely to :func:`validate_request_mode`
    (the same strict public-wire contract ``/messages/stream`` enforces) —
    this function adds ONLY the internal omitted-to-chat default on top of
    it, never a second, looser validation path. Raises :class:`ValueError`
    (bounded, generic, never echoing ``raw``) for anything
    :func:`validate_request_mode` would reject."""
    return validate_request_mode(raw) or "chat"


def _system_prompt_hmac(system_prompt: Optional[str]) -> Optional[str]:
    if not system_prompt:
        return None
    return hmac.new(_hmac_key(), _SYSPROMPT_DOMAIN + b"\x00" + system_prompt.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def build_chat_submission(
    *,
    session_id: int,
    message: str,
    image_url: Optional[str],
    model: Optional[str],
    mode: Optional[str],
    system_prompt: Optional[str],
    provider: Optional[str] = None,
    options: Optional[dict] = None,
) -> ChatRunSubmission:
    """Assemble the canonical submission. ``provider`` / ``model`` / ``mode`` /
    ``system_prompt`` are the SERVER-resolved values (caller passes what it
    resolved from the session + user settings + request). ``options`` must be a
    flat dict of scalars. Every behaviour-changing field feeds the semantic
    identity HMAC (`provider` added in `chat-run/v2`)."""
    if not isinstance(message, str) or not (1 <= len(message) <= _MESSAGE_MAX):
        raise ValueError("message must be 1..10000 chars")
    opts = {}
    for k, v in (options or {}).items():
        if not isinstance(k, str) or isinstance(v, (dict, list, tuple, set)):
            raise ValueError("options must be a flat dict of scalars")
        opts[k] = v
    resolved_mode = resolve_mode(mode)
    # MP-4C decision 2 follow-up (Task 8): the durable session system prompt
    # carries the memory-marker grammar ONLY while the server-owned memory-write
    # flag is on, and only for the normal chat mode (deep-research / multi-agent
    # submissions keep their own prompts). Flag OFF -> nothing is injected.
    effective_system_prompt = system_prompt
    if resolved_mode == "chat":
        effective_system_prompt = _save_markers.compose_durable_system_prompt(system_prompt)
    return ChatRunSubmission(
        contract_version=CONTRACT_VERSION,
        session_id=int(session_id),
        message=message,
        image_url=(image_url or None),
        resolved_provider=((provider or "").strip() or None),
        resolved_model=((model or "").strip() or None),
        resolved_mode=resolved_mode,
        system_prompt_hmac=_system_prompt_hmac(effective_system_prompt),
        resolved_system_prompt=(effective_system_prompt if effective_system_prompt else None),
        options=dict(sorted(opts.items())),
    )


def _canonical_bytes(sub: ChatRunSubmission) -> bytes:
    return canonicalize({
        "v": sub.contract_version,
        "session_id": sub.session_id,
        "message": sub.message,
        "image_url": sub.image_url,
        "provider": sub.resolved_provider,
        "model": sub.resolved_model,
        "mode": sub.resolved_mode,
        "system_prompt_hmac": sub.system_prompt_hmac,
        "options": sub.options,
    })


def semantic_identity(sub: ChatRunSubmission) -> str:
    """Domain-separated HMAC-SHA256 over the canonical validated submission.
    64-char hex. The raw prompt is an HMAC *input* (keyed ⇒ not an offline
    dictionary target) and is NEVER returned or stored as identity metadata."""
    return hmac.new(_hmac_key(), _SEMANTIC_DOMAIN + b"\x00" + _canonical_bytes(sub),
                    hashlib.sha256).hexdigest()


# ── atomic submission persistence ─────────────────────────────────


def create_chat_run(
    db: Session,
    ctx: AuthContext,
    *,
    submission: ChatRunSubmission,
    idempotency_key: str,
    context: Optional[dict] = None,
) -> tuple[Run, bool]:
    """Atomically create — or idempotently return — the durable chat run for
    this scoped ``Idempotency-Key`` + semantic submission.

    On a fresh submission ONE transaction persists: the user ``Message``, the
    ``Run`` (server-generated id, ``created→queued``), the initial ``RunStep``
    (non-secret ``input_ref`` — ids only), the encrypted input ``RunPayload``,
    and the ``run.created`` + ``run.status`` control events. A replay adds
    nothing (retrieval — see ``context`` below — never re-runs). Returns
    ``(run, created)``.

    ``context`` (Phase 1B.2R MP-3C3D2, optional): a pre-computed
    context-enrichment snapshot — ``{"rag_context_text", "citations",
    "sources_used", "memory_context_text", "memories_used"}`` — sealed
    alongside the execution snapshot in the SAME encrypted payload.
    **The caller MUST compute this BEFORE calling `create_chat_run`, using a
    session that has nothing else pending** — `memory_service.
    retrieve_relevant_memories` commits internally (access-metric
    bookkeeping), and calling it from inside this function's own atomic
    staging area would force-commit the not-yet-CAS-confirmed `Run`/`Message`
    rows early (this was tried and reverted after it broke
    `test_submission_commit_failure_no_partial`'s all-or-nothing guarantee —
    see the MP-3C3D2 completion report). Not part of the semantic identity
    (server-DERIVED data, not user-supplied request identity) — a replay
    reusing the same Idempotency-Key returns early below and never seals a
    second (possibly different) context, even though the caller may have
    computed one again wastefully.

    Raises :class:`IdempotencyKeyConflict` (same key, different semantic
    submission), the session-binding errors, or ``ValueError``.
    """
    validate_session_binding(db, ctx)
    sem = semantic_identity(submission)
    if submission.resolved_mode == MULTI_AGENT_RUN_KIND:
        # Serialize admission through commit across both public routes. SQLite
        # takes its writer lock; row-locking databases lock this user's row.
        db.execute(update(User).where(User.id == ctx.user_id).values(id=User.id))
        enforce_multi_agent_active_run_cap(
            db, ctx, resolved_mode=submission.resolved_mode,
            idempotency_key=idempotency_key, session_id=submission.session_id,
        )

    # submit_run: replay-returns an identity-matching run, raises on a mismatch,
    # else creates Run(created) + run.created event WITHOUT committing.
    run = submit_run(
        db, ctx,
        idempotency_key=idempotency_key,
        session_id=submission.session_id,
        kind=_run_kind_for_mode(submission.resolved_mode),
        identity_hash=sem,
        commit=False,
    )

    if db.query(RunStep).filter(RunStep.run_id == run.id).first() is not None:
        # replay of a fully-persisted run — nothing new, nothing to commit.
        run_id = run.id
        db.rollback()
        return db.query(Run).filter(Run.id == run_id).first(), False

    msg = Message(
        session_id=submission.session_id, role=MessageRole.user,
        content=submission.message, image_url=submission.image_url,
        created_at=_utcnow_aware(),
    )
    db.add(msg)
    db.flush()

    # Versioned encrypted execution snapshot (server-resolved provider / model /
    # mode / resolved system prompt / options) sealed alongside the message —
    # NEVER credentials. The worker uses this snapshot, never live settings.
    from app.run_engine.chat_stream import (
        CONTEXT_SNAPSHOT_VERSION, build_context_snapshot, build_exec_snapshot,
    )

    # MP-3C3D2 — every fresh submission seals a VERSIONED context object,
    # including the "nothing retrieved" case — never a bare None, regardless
    # of caller (this function has TWO callers: messages_stream's durable
    # /messages/stream, which always computes a real snapshot, and the
    # direct Run API — routes/runs.py — which passes no context at all). A
    # caller-supplied dict is trusted only if it already carries the correct
    # version marker (it was built via build_context_snapshot()); anything
    # else — None, or an unversioned/malformed dict from any future caller —
    # falls back to the canonical empty versioned snapshot, never a raw None.
    sealed_context = (
        context if isinstance(context, dict)
        and context.get("context_snapshot_version") == CONTEXT_SNAPSHOT_VERSION
        else build_context_snapshot()
    )

    step_id = str(uuid.uuid4())
    payload = seal_payload(
        db,
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, run_id=run.id, step_id=step_id,
        payload_type="other",
        plaintext=canonicalize({
            "message": submission.message,
            "image_url": submission.image_url,
            "exec": build_exec_snapshot(
                provider=submission.resolved_provider,
                model=submission.resolved_model,
                mode=submission.resolved_mode,
                system_prompt=submission.resolved_system_prompt,
                options=submission.options,
            ),
            # MP-3C3D2 — sealed once, here, on a fresh submission only; a
            # replay returns above before this line ever runs, so context is
            # never re-selected (or re-committed) for an existing run.
            "context": sealed_context,
        }).decode("utf-8"),
        commit=False,
    )

    # D6C-J9 - seq-1 `load_context` OWNERSHIP (authorized lifecycle contract).
    #
    # Plain chat: this row IS the initial generation segment (chat_stream.CHAT_STEP_KIND
    # doubles as the generate step), so the chat worker owns it and advances it
    # pending -> running -> succeeded. It is created `pending` on purpose.
    #
    # deep_research / multi_agent: the mode-specific workers own and execute their OWN
    # dedicated steps (research_fetch_extract / deep_research_synthesis and
    # agent_research / agent_solution / agent_review) and never advance this
    # compatibility/submission placeholder. Creating it `pending` advertised
    # unfinished, apparently reclaimable work that no component owned, so it is created
    # in the documented TERMINAL state STEP_SKIPPED instead (state_machine.STEP_TERMINAL
    # includes STEP_SKIPPED). This creates no provider work whatever: the sealed context
    # snapshot and payload above are produced exactly as before.
    _chat_owns_load_context = submission.resolved_mode == "chat"
    step = RunStep(
        id=step_id, run_id=run.id, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
        session_id=submission.session_id, seq=1, kind="load_context", engine="native",
        status=(STEP_PENDING if _chat_owns_load_context else STEP_SKIPPED),
        finished_at=(None if _chat_owns_load_context else _utcnow_aware()),
        input_payload_id=payload.id,
        input_ref=json.dumps({
            "message_id": msg.id,
            "model": submission.resolved_model,
            "mode": submission.resolved_mode,
            "contract_version": submission.contract_version,
        }, sort_keys=True),
    )
    db.add(step)
    db.flush()

    # `run.queued` control event (seq after `run.created`) — in the same
    # uncommitted transaction, so it lands with the transition or not at all.
    _try_append_event(
        db, run_id=run.id, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
        event_type="run.queued", data_text=validate_event_data("run.queued", None),
    )

    # commits Message + payload + step + run.queued + the created->queued transition
    transition_run(db, ctx, run.id, to="queued", expected_version=0)
    return db.query(Run).filter(Run.id == run.id).first(), True


# ── MP-3C3D5C — multi-agent active-run cap (foundation-only setting,
#    enforced here at submission time) ──────────────────────────────


def enforce_multi_agent_active_run_cap(
    db: Session, ctx: AuthContext, *, resolved_mode: str,
    idempotency_key: str, session_id: int,
) -> None:
    """Fail closed (:class:`MultiAgentActiveRunLimitExceeded`) BEFORE any
    persistence if creating a genuinely NEW ``multi_agent`` run for this
    user (+tenant, when present) would exceed
    ``settings.multi_agent_max_active_runs_per_user``. A no-op for any other
    mode.

    Idempotent REPLAY is detected FIRST via the exact same scoped
    ``uq_runs_idem_scope`` lookup ``submit_run`` itself uses (no second,
    drifting identity check) — a matching existing run (same key, whether
    its semantic identity matches or conflicts) is NEVER counted against or
    blocked by this cap; ``create_chat_run``/``submit_run`` handle
    replay-vs-conflict entirely on their own, downstream of this call.

    Active states are the canonical non-terminal ``Run`` states
    (``state_machine.RUN_TERMINAL``'s complement) — never a second, drifting
    list of "which statuses count".

    create_chat_run holds the database user-row write lock through admission
    and submission commit. Callers must not use this check alone for admission.
    """
    if resolved_mode != MULTI_AGENT_RUN_KIND:
        return
    existing = _select_by_idem_scope(
        db, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
        session_id=session_id, idempotency_key=_normalize_key(idempotency_key),
    )
    if existing is not None:
        return  # replay (or a conflicting key create_chat_run will itself reject) — never blocked
    limit = int(settings.multi_agent_max_active_runs_per_user)
    active = (
        db.query(Run)
        .filter(
            Run.user_id == ctx.user_id,
            func.coalesce(Run.tenant_id, "") == (ctx.tenant_id or ""),
            Run.kind == MULTI_AGENT_RUN_KIND,
            Run.status.notin_(tuple(RUN_TERMINAL)),
        )
        .count()
    )
    if active >= limit:
        raise MultiAgentActiveRunLimitExceeded()
