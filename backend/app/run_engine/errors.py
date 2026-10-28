"""Typed errors for the durable run engine (Phase 1B.2R — MP-0).

All inherit from ``RunEngineError`` so a call site can catch the family. None
carry secret material — messages name the *reason*, never a raw payload value.
"""

from __future__ import annotations


class RunEngineError(Exception):
    """Base class for every run-engine failure."""


class InvalidRunTransition(RunEngineError):
    """A run/step state transition was refused — the source state does not
    permit the requested target (see ``app.run_engine.state_machine``)."""

    def __init__(self, frm: str, to: str, *, scope: str = "run"):
        self.frm = frm
        self.to = to
        self.scope = scope
        super().__init__(f"illegal {scope} transition: {frm!r} -> {to!r}")


class RunPayloadError(RunEngineError):
    """Base for encrypted run-payload failures. Fails CLOSED — a payload that
    cannot be sealed, opened, or verified must never fall back to plaintext or
    to empty/partial data."""


class RunPayloadEncryptionUnavailable(RunPayloadError):
    """ENCRYPTION_KEY is not configured, so a run payload cannot be sealed.
    The run must not proceed to a step that needs a persisted payload."""


class RunPayloadTooLarge(RunPayloadError):
    """The plaintext exceeds ``settings.run_payload_max_bytes``."""

    def __init__(self, byte_len: int, limit: int):
        self.byte_len = byte_len
        self.limit = limit
        super().__init__(f"run payload is {byte_len} bytes; limit is {limit}")


class RunPayloadAccessError(RunPayloadError):
    """The caller's (tenant, user, run, step) does not own this payload, or it
    is soft-deleted. Non-disclosing."""


class RunPayloadExpired(RunPayloadError):
    """The payload's ``expires_at`` has passed — it is no longer readable."""


class RunPayloadSchemeError(RunPayloadError):
    """The stored ``enc_scheme`` is not one this build knows how to open."""


class RunPayloadActionHashMismatch(RunPayloadError):
    """The decrypted envelope's ``action_hash`` does not equal the trusted
    ``expected_action_hash`` (both ``None`` is a match; any other difference is
    not). Fail closed — the sensitive bytes are not released for a different
    approved action."""


class RunPayloadIntegrityError(RunPayloadError):
    """The Fernet ciphertext failed its authentication tag (tampered / wrong
    key / truncated / malformed encoding). The bytes are not trustworthy —
    refuse them."""


class RunPayloadEnvelopeError(RunPayloadError):
    """The authenticated-but-decrypted bytes are not a well-formed run-payload
    envelope: not valid JSON, not a JSON object, a missing or extra field, or a
    field of the wrong type."""


class RunPayloadEnvelopeVersionError(RunPayloadEnvelopeError):
    """The decrypted envelope's ``version`` is not one this build understands."""


class RunPayloadEncodingError(RunPayloadError):
    """The envelope declares a ``payload_encoding`` other than ``utf-8``, or the
    payload / decrypted bytes are not valid UTF-8."""


class RunPayloadContextMismatch(RunPayloadError):
    """A decrypted envelope context field (``tenant_id`` / ``user_id`` /
    ``run_id`` / ``step_id`` / ``payload_type``) does not equal the trusted
    server-supplied expected value. The ciphertext belongs to a different
    run/step/user/tenant context — refuse it. Carries only the mismatched
    field name (non-secret)."""

    def __init__(self, field: str):
        self.field = field
        super().__init__(f"run payload envelope context mismatch on {field!r}")


# ── MP-1A: core transactional-service errors ─────────────────────────
#
# Raised by ``app.run_engine.engine`` / ``app.run_engine.events``. None carry
# secret material — a message names the reason, never a prompt / arg value.


class SessionBindingRequired(RunEngineError):
    """The caller's access token has no verified refresh-family binding
    (``sid`` claim missing, or its ``refresh_sessions`` family is revoked /
    expired). Every RunEngine *mutation* fails closed with this — the caller
    must re-authenticate to obtain a fresh, bound token."""

    def __init__(self) -> None:
        super().__init__("fresh authentication / session binding required")


class SessionBindingMismatch(RunEngineError):
    """A run is bound to one refresh family and the caller presents a token
    bound to a different one. Cross-session control of a durable run is
    refused."""

    def __init__(self) -> None:
        super().__init__("run is bound to a different authenticated session")


class RunNotFound(RunEngineError):
    """No run with this id is visible to the caller (absent, or owned by a
    different user / tenant). Deliberately non-disclosing — a cross-owner
    lookup is indistinguishable from a missing row."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id!r} not found")


class IdempotencyKeyConflict(RunEngineError):
    """The submission scope already holds a run created from the SAME
    idempotency key but a DIFFERENT normalized request identity. The key was
    reused for a different request — deterministic conflict, not a replay."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__("idempotency key reused for a different request identity")


class IdempotencyKeyTooLong(RunEngineError):
    """The idempotency key exceeds ``settings.run_idempotency_key_max_bytes``."""

    def __init__(self, byte_len: int, limit: int) -> None:
        self.byte_len = byte_len
        self.limit = limit
        super().__init__(f"idempotency key is {byte_len} bytes; limit is {limit}")


class RunSubmitContention(RunEngineError):
    """A bounded retry budget was exhausted resolving a concurrent submission
    race for the same idempotency scope. The caller may retry."""


class RunStateConflict(RunEngineError):
    """An optimistic ``runs.version`` compare-and-swap lost — another writer
    advanced the run first. The caller must re-read and decide."""

    def __init__(self, expected_version: int, actual_version: int | None) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"stale run write: expected version {expected_version}, "
            f"found {actual_version!r}"
        )


class RunAlreadyTerminal(RunEngineError):
    """The run is already in a terminal state; the requested transition is
    refused (a no-op re-assertion of the same terminal state is allowed and
    does not raise)."""

    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__(f"run is terminal ({state!r})")


class CancellationInProgress(RunEngineError):
    """The requested non-cancel transition lost a race to a cancellation:
    the run is now ``cancelling`` / ``cancelled``. Cancellation wins (D4)."""

    def __init__(self) -> None:
        super().__init__("run cancellation is in progress")


class DisallowedControlEventType(RunEngineError):
    """The control-event ``type`` is not in the closed allowlist
    (``app.run_engine.events.CONTROL_EVENT_TYPES``)."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(f"control-event type {event_type!r} is not allowlisted")


class ControlEventFieldError(RunEngineError):
    """The control-event ``data`` violated the closed per-type schema — an
    unknown / extra key, a missing required key, a nested value, a wrong type,
    a value outside the allowed enum, or an over-length string. Carries only
    the field name and a reason slug (never the offending value)."""

    def __init__(self, event_type: str, field: str, reason: str) -> None:
        self.event_type = event_type
        self.field = field
        self.reason = reason
        super().__init__(
            f"control-event {event_type!r} field {field!r}: {reason}"
        )


class ControlEventTooLarge(RunEngineError):
    """The serialized control-event ``data`` exceeds
    ``settings.run_control_event_max_bytes``."""

    def __init__(self, byte_len: int, limit: int) -> None:
        self.byte_len = byte_len
        self.limit = limit
        super().__init__(f"control-event data is {byte_len} bytes; limit is {limit}")


class ControlEventContention(RunEngineError):
    """A bounded retry budget was exhausted allocating the next ``run_seq``
    under concurrent appenders. The caller may retry."""


# ── MP-1B: worker lease ─────────────────────────────────────────────


class RunLeaseLost(RunEngineError):
    """A worker mutation's ``lease_owner`` + ``version`` compare-and-swap matched
    no row — the lease expired, was reclaimed by another worker, or the run
    advanced underneath this worker. The worker abandons the run for the current
    sweep; the next sweep re-evaluates it. Carries only the run id."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id!r} lease/version CAS lost")
