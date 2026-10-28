"""Phase 1B.2R MP-4C — the ONE authoritative policy for model-emitted Python.

BACKEND-ONLY, pure and side-effect free: this module NEVER executes code, never
touches the database, never opens a subprocess and never logs model text. It
answers exactly three questions:

1. May a LEGACY path auto-execute model-emitted Python? — **Never.** In every
   configuration, including with run_engine_sandbox=True. Execution requires a
   server-owned approval consumed through the durable approval gateway; the
   legacy streaming / LangGraph paths cannot perform durable approval+resume
   (that retirement is MP-5-owned), so they fail closed and return a bounded
   notice instead of running code.
2. Is a captured model response a VALID, single, bounded Python marker? — see
   strict_extract_python_code(), which reuses the repository's ONE canonical
   marker grammar (`app.tools.python_sandbox.PYTHON_CODE_PATTERN`) rather than
   defining a second regex.
3. May REAL model-generated Python execute, and through which route? — see
   real_python_execution_available(): the ONE authority, default OFF. It
   becomes True only for an explicit operator opt-in with a live, verified
   executor, and it governs the BROKER route alone. The in-process host
   subprocess is refused unconditionally in app.tools.python_sandbox and
   cannot be re-opened through this module.

Nothing here is user-visible text derived from model output except fixed,
bounded constants.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from app.config import settings

# Bounded, machine-readable reasons. Every entry is a short slug; none carries
# marker text, code, or model output.
SANDBOX_STATUS_REASONS = frozenset({
    "sandbox_disabled",             # run_engine_sandbox is off
    "sandbox_approval_required",    # execution needs a consumed approval
    "sandbox_marker_malformed",     # an opener with no parseable body
    "sandbox_marker_incomplete",    # an opener that never closed
    "sandbox_marker_multiple",      # more than one marker in one turn
    "sandbox_marker_nested",        # an opener inside the captured body
    "sandbox_marker_mixed",         # combined with another execution marker
    "sandbox_marker_oversized",     # body exceeds run_engine_sandbox_max_code_bytes
})

# The ONLY text a legacy path may surface in place of a real result. Fixed and
# bounded: it contains no code, no marker echo, and no model output.
LEGACY_PYTHON_BLOCK_NOTICE = (
    "[Python execution was not performed: it requires an explicit, "
    "server-owned approval. Re-run this request as a durable run to request "
    "approval.]"
)

# Other reserved EXECUTION openers (mirrors run_engine.chat_stream's canonical
# _EXECUTION_OPENERS). Used only to detect a MIXED marker turn.
_OTHER_EXECUTION_OPENERS = ("[mcp_call", "[proposed_command", "[browser_action")

_PYTHON_OPENER = "[python_code"


def sandbox_enabled() -> bool:
    """True only when the deployment has explicitly enabled the MP-4C
    approval-proposal path. Default is False."""
    return bool(getattr(settings, "run_engine_sandbox", False))


def legacy_may_execute_python() -> bool:
    """Whether a LEGACY path may auto-execute model-emitted Python.

    ALWAYS False, under every flag combination. This is the single authority
    the legacy call sites consult; it exists as a named function (rather than a
    bare False at each site) so the invariant is testable and cannot drift per
    call site. run_engine_sandbox=True authorizes creating a PROPOSAL, never
    execution.
    """
    return False


#: The ONE bounded reason reported when real model-generated Python execution is
#: requested but no isolated runtime is authorized. Short slug; never code.
REAL_EXECUTION_UNAVAILABLE_REASON = "python_host_isolation_unavailable"

# ── The ONE authorization for REAL model-generated Python execution ─────────
# Default OFF. It becomes True only when an operator has EXPLICITLY opted in
# (env-only: never a Settings default) AND a configured executor is verified live
# right now. This is the ONLY place that decision is made; no second predicate
# may bypass it, and no call site may authorize execution without consulting it.
#
# Scope: this predicate governs the BROKER route only. It can never re-enable the
# in-process host subprocess: app.tools.python_sandbox._run_python_async refuses
# unconditionally and does not consult this function.
OPT_IN_ENV = "EXECUTOR_OPERATOR_OPT_IN"
EXECUTOR_URL_ENV = "EXECUTOR_SERVICE_URL"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Installed by the production broker client when it is CONSTRUCTED. Registration
#: performs no I/O; the probe runs only from real_python_execution_available().
_readiness_probe = None


def operator_opt_in_enabled() -> bool:
    """True only for an EXPLICIT operator opt in. Env-only; default OFF."""
    return str(os.environ.get(OPT_IN_ENV) or "").strip().lower() in _TRUTHY


def executor_configured() -> bool:
    """Static configuration only (no I/O): an executor endpoint is set.

    The signing secret is validated separately when the broker client is built,
    and fails closed there. Necessary, NOT sufficient.
    """
    return bool(str(os.environ.get(EXECUTOR_URL_ENV) or "").strip())


def register_executor_readiness_probe(probe) -> None:
    """Install the LIVE readiness probe. Registration is side-effect free."""
    global _readiness_probe
    _readiness_probe = probe


def reset_executor_readiness_probe() -> None:
    """Remove the probe (test hygiene / shutdown)."""
    global _readiness_probe
    _readiness_probe = None


def real_python_execution_available() -> bool:
    """The ONE authority: may REAL model-generated code execute?

    True only when ALL of these hold, and False in every other case:
      1. the operator explicitly opted in (EXECUTOR_OPERATOR_OPT_IN),
      2. an executor endpoint is configured (EXECUTOR_SERVICE_URL),
      3. a readiness probe is registered and reports the pinned image, the
         daemon and the required isolation checks as valid RIGHT NOW.

    A missing probe, a probe that raises, or a probe that reports not-ready is
    NOT ready - the fail-closed answer is always False. The probe can only DENY:
    it is consulted by this function alone, so nothing can use it to authorize
    execution behind this predicate.

    What this does NOT govern: the in-process host subprocess. That path is
    unconditionally refused in app.tools.python_sandbox and is not reachable
    through this function under any flag combination.
    """
    if not operator_opt_in_enabled():
        return False
    if not executor_configured():
        return False
    probe = _readiness_probe
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - an unavailable executor is NOT ready
        return False


def max_code_bytes() -> int:
    return max(1, int(getattr(settings, "run_engine_sandbox_max_code_bytes", 8192)))


def strict_extract_python_code(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Strictly parse ONE canonical [PYTHON_CODE: ...] marker.

    Returns (code, None) on success, or (None, reason) where *reason* is a
    bounded slug from SANDBOX_STATUS_REASONS. (None, None) means "no Python
    marker at all" — a legitimate, non-error outcome.

    Reuses app.tools.python_sandbox.PYTHON_CODE_PATTERN — the repository's ONE
    canonical grammar. No second regex is defined here. Never executes, never
    logs, never returns text derived from the body in the reason.
    """
    # Lazy import: keeps this module import-safe for pure unit tests and avoids
    # a config -> langchain_core import at Settings-construction time.
    from app.tools.python_sandbox import PYTHON_CODE_PATTERN

    if not text or not isinstance(text, str):
        return None, None

    lowered = text.lower()
    openers = lowered.count(_PYTHON_OPENER)
    matches = PYTHON_CODE_PATTERN.findall(text)

    if openers == 0 and not matches:
        return None, None
    if openers == 0:
        # A body matched with no visible opener is not the canonical grammar.
        return None, "sandbox_marker_malformed"
    if len(matches) > 1:
        return None, "sandbox_marker_multiple"
    if not matches:
        # Opener present, nothing parsed: either never closed or unusable body.
        # A closing bracket anywhere after the opener distinguishes the two.
        idx = lowered.find(_PYTHON_OPENER)
        return None, ("sandbox_marker_incomplete" if "]" not in text[idx:]
                      else "sandbox_marker_malformed")

    code = (matches[0] or "").strip()
    if not code:
        return None, "sandbox_marker_malformed"
    if _PYTHON_OPENER in code.lower():
        return None, "sandbox_marker_nested"
    for opener in _OTHER_EXECUTION_OPENERS:
        if opener in lowered:
            return None, "sandbox_marker_mixed"
    if len(code.encode("utf-8")) > max_code_bytes():
        return None, "sandbox_marker_oversized"
    return code, None


def strip_python_marker(text: Optional[str]) -> str:
    """Remove every canonical [PYTHON_CODE: ...] marker from *text* so raw
    marker text can never reach a user-visible stream, a persisted Message, or
    an unencrypted column. Pure; never executes."""
    from app.tools.python_sandbox import PYTHON_CODE_PATTERN

    if not text or not isinstance(text, str):
        return text or ""
    return PYTHON_CODE_PATTERN.sub("", text)


def legacy_python_fail_closed(emitted_text: Optional[str]) -> str:
    """The bounded replacement a LEGACY path uses instead of executing Python.

    Returns "" when the response contains no Python marker (nothing to say), or
    LEGACY_PYTHON_BLOCK_NOTICE when it does. Never returns code and never
    echoes the marker.
    """
    code, _reason = strict_extract_python_code(emitted_text)
    has_marker = code is not None or (
        bool(emitted_text) and _PYTHON_OPENER in (emitted_text or "").lower())
    return LEGACY_PYTHON_BLOCK_NOTICE if has_marker else ""


__all__ = [
    "SANDBOX_STATUS_REASONS",
    "LEGACY_PYTHON_BLOCK_NOTICE",
    "sandbox_enabled",
    "legacy_may_execute_python",
    "REAL_EXECUTION_UNAVAILABLE_REASON",
    "real_python_execution_available",
    "OPT_IN_ENV",
    "EXECUTOR_URL_ENV",
    "operator_opt_in_enabled",
    "executor_configured",
    "register_executor_readiness_probe",
    "reset_executor_readiness_probe",
    "max_code_bytes",
    "strict_extract_python_code",
    "strip_python_marker",
    "legacy_python_fail_closed",
]
