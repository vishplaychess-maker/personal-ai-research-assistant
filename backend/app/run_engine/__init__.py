"""Durable run engine (Phase 1B.2R).

MP-0 scope: the durable run ledger SCHEMA (``Run`` / ``RunStep`` / ``RunEvent``
/ ``RunSignal`` / ``RunPayload`` in ``app.models.models``), the pure run/step
STATE MACHINE, the encrypted PAYLOAD store, the allowlisted SIGNAL schema, and
the fail-closed session-binding predicate. No worker, no run loop, no routes, no
streaming adapter, no approval resume, no tool invocation — those are MP-1 and
later.

RunEngine is the canonical outer run ledger; a future LangGraph subflow
(``run_steps.engine == "langgraph"``) stays subordinate to this ledger's
identity / approval / dispatch / audit state.
"""

from app.run_engine.errors import (
    CancellationInProgress,
    ControlEventContention,
    ControlEventFieldError,
    ControlEventTooLarge,
    DisallowedControlEventType,
    IdempotencyKeyConflict,
    IdempotencyKeyTooLong,
    InvalidRunTransition,
    RunAlreadyTerminal,
    RunEngineError,
    RunNotFound,
    RunPayloadAccessError,
    RunPayloadActionHashMismatch,
    RunPayloadContextMismatch,
    RunPayloadEncodingError,
    RunPayloadEncryptionUnavailable,
    RunPayloadEnvelopeError,
    RunPayloadEnvelopeVersionError,
    RunPayloadError,
    RunPayloadExpired,
    RunPayloadIntegrityError,
    RunPayloadSchemeError,
    RunLeaseLost,
    RunPayloadTooLarge,
    RunStateConflict,
    RunSubmitContention,
    SessionBindingMismatch,
    SessionBindingRequired,
)
from app.run_engine.payloads import (
    ENC_SCHEME,
    ENVELOPE_VERSION,
    SUPPORTED_SCHEMES,
    open_payload,
    purge_payload,
    seal_payload,
)
from app.run_engine.context import (
    AuthContext,
    build_auth_context,
    get_auth_context,
)
from app.run_engine.engine import (
    CancellationResult,
    get_run,
    request_cancellation,
    submit_run,
    transition_run,
)
from app.run_engine.events import (
    CONTROL_EVENT_SCHEMAS,
    CONTROL_EVENT_TYPES,
    append_control_event,
    read_control_events,
    validate_event_data,
)
from app.run_engine.session_binding import (
    session_ref_is_active,
    validate_session_binding,
)
from app.run_engine.signals import (
    SIGNAL_KINDS,
    SignalPayloadError,
    build_signal_payload,
    parse_signal_payload,
)
from app.run_engine.mcp_flow import (
    MCP_RUN_KIND,
    MCP_STEP_KIND,
    create_mcp_run,
    mirror_decision_signal,
)
from app.run_engine.submission import (
    CONTRACT_VERSION,
    ChatRunSubmission,
    RunFeatureDisabled,
    RunFeatureMisconfigured,
    build_chat_submission,
    create_chat_run,
    require_run_public_api,
    semantic_identity,
)
from app.run_engine.chat_stream import (
    CHAT_RUN_KIND,
    CHAT_STATUS_REASONS,
    build_chat_input,
)
from app.run_engine.text_events import TextEventBuffer, append_text_event
from app.run_engine.sse import parse_last_event_id, stream_run_events
from app.run_engine.events import read_run_events
from app.run_engine.worker import (
    ClaimedRun,
    HandlerContext,
    HandlerResult,
    HandlerSpec,
    RunOutcome,
    RunWorker,
    clear_handlers,
    register_handler,
    resolve_handler,
    unregister_handler,
)
from app.run_engine.worker_supervisor import (
    RunWorkerSupervisor,
    SupervisorFatalError,
    WorkerStatus,
    assert_embedded_topology_ok,
)
from app.run_engine.notifier import (
    RunNotifier,
    install_notifier_hooks,
    run_notifier,
)
from app.run_engine.state_machine import (
    RUN_STATES,
    RUN_TERMINAL,
    RUN_TRANSITIONS,
    STEP_STATES,
    STEP_TERMINAL,
    STEP_TRANSITIONS,
    assert_run_transition,
    assert_step_transition,
    idempotent_decision_state,
    is_run_terminal,
    is_step_terminal,
    race_winner,
    run_can_transition,
    step_can_transition,
)

__all__ = [
    "RunEngineError", "InvalidRunTransition",
    "RunPayloadError", "RunPayloadEncryptionUnavailable", "RunPayloadTooLarge",
    "RunPayloadAccessError", "RunPayloadExpired", "RunPayloadSchemeError",
    "RunPayloadActionHashMismatch", "RunPayloadIntegrityError",
    "RunPayloadEnvelopeError", "RunPayloadEnvelopeVersionError",
    "RunPayloadEncodingError", "RunPayloadContextMismatch",
    "seal_payload", "open_payload", "purge_payload",
    "ENC_SCHEME", "SUPPORTED_SCHEMES", "ENVELOPE_VERSION",
    "session_ref_is_active", "validate_session_binding",
    # MP-1A — auth context
    "AuthContext", "build_auth_context", "get_auth_context",
    # MP-1A — core services
    "submit_run", "get_run", "transition_run", "request_cancellation",
    "CancellationResult",
    "append_control_event", "read_control_events",
    "CONTROL_EVENT_TYPES", "CONTROL_EVENT_SCHEMAS", "validate_event_data",
    # MP-1A — typed errors
    "SessionBindingRequired", "SessionBindingMismatch", "RunNotFound",
    "IdempotencyKeyConflict", "IdempotencyKeyTooLong", "RunSubmitContention",
    "RunStateConflict", "RunAlreadyTerminal", "CancellationInProgress",
    "DisallowedControlEventType", "ControlEventTooLarge", "ControlEventContention",
    "ControlEventFieldError",
    # MP-1B — worker / claim / lease / recovery
    "RunWorker", "ClaimedRun", "RunOutcome", "RunLeaseLost",
    "HandlerContext", "HandlerResult", "HandlerSpec",
    "register_handler", "unregister_handler", "clear_handlers", "resolve_handler",
    # MP-3C3A — embedded worker lifespan supervisor
    "RunWorkerSupervisor", "WorkerStatus", "SupervisorFatalError",
    "assert_embedded_topology_ok",
    # MP-3C3B — DB-authoritative in-process run-event notifier
    "RunNotifier", "run_notifier", "install_notifier_hooks",
    # MP-2 — durable one-time MCP approval resume
    "create_mcp_run", "mirror_decision_signal", "MCP_RUN_KIND", "MCP_STEP_KIND",
    # MP-3B — worker-backed durable chat generation
    "CHAT_RUN_KIND", "CHAT_STATUS_REASONS", "build_chat_input",
    # MP-3A — durable Run public API contract + text events + SSE
    "ChatRunSubmission", "CONTRACT_VERSION", "build_chat_submission",
    "semantic_identity", "create_chat_run", "require_run_public_api",
    "RunFeatureDisabled", "RunFeatureMisconfigured",
    "TextEventBuffer", "append_text_event", "read_run_events",
    "stream_run_events", "parse_last_event_id",
    "SIGNAL_KINDS", "SignalPayloadError", "build_signal_payload", "parse_signal_payload",
    "RUN_STATES", "RUN_TERMINAL", "RUN_TRANSITIONS",
    "STEP_STATES", "STEP_TERMINAL", "STEP_TRANSITIONS",
    "assert_run_transition", "assert_step_transition",
    "run_can_transition", "step_can_transition",
    "is_run_terminal", "is_step_terminal", "race_winner",
    "idempotent_decision_state",
]
