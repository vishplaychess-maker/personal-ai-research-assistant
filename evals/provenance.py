"""Provenance + evidence-level vocabulary + redaction for the eval harness.

The point of this module: never let an authored fixture be reported as a
verified product pass. Every result carries where its evidence came from.
"""

from __future__ import annotations

import functools
import hashlib
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Bump when the dataset's task set or assertion semantics change.
DATASET_VERSION = "1a-closure.1"
# Bump when the prompt/template wiring the harness relies on changes.
PROMPT_VERSION = "n/a-phase1a"
# Bump when the runner / adapter / assert / provenance logic changes in a way
# that could move results. Distinct from DATASET_VERSION (the task set).
EVALUATOR_VERSION = "1a-closure.2"

_EVALS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVALS_DIR.parent


class AdapterUnavailable(Exception):
    """An adapter could not produce product evidence for a task (no target
    registered, no reachable backend, no free model). NOT a harness error -
    the runner turns it into an `unverified` result.

    Defined here (not in runner.py) so `python -m evals.runner` running the
    runner as __main__ and `evals.adapters.*` importing `evals.runner` share
    the SAME exception class instead of two lookalikes.
    """

# The evidence a result / a task's intended target can carry. Ordered weakest
# -> strongest for product verification. `graded_quality` overlays any level.
EVIDENCE_LEVELS = (
    "harness",             # the harness validating itself (Gate A machinery)
    "synthetic",           # authored regression fixture, NO product code ran
    "provider_recording",  # a provider API was called directly, no product path
    "product_inprocess",   # a real backend function ran in-process, side effects mocked at the boundary
    "product_backend",     # a running backend HTTP endpoint served the response
    "e2e_live",            # full running product + live model, recorded
    "graded_quality",      # human- or model-graded (may overlay another level)
)

# Levels that count as "the real Thunder AI product path executed".
VERIFIED_PRODUCT_LEVELS = frozenset({"product_inprocess", "product_backend", "e2e_live"})


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except Exception:
        return None


def current_commit_sha() -> str:
    out = _git("rev-parse", "HEAD")
    return (out or "").strip() or "unknown"


def working_tree_dirty() -> bool:
    """True when the repo has staged, unstaged, or untracked changes.

    A result generated from a dirty tree does NOT faithfully represent
    `current_commit_sha()` — callers must record both.
    """
    out = _git("status", "--porcelain", "--untracked-files=normal")
    if out is None:
        return False  # not a git checkout / git unavailable — cannot claim dirty
    return bool(out.strip())


def source_tree_or_diff_digest() -> str | None:
    """A stable digest of what the working tree actually contained when the
    evaluation ran, for the case where `working_tree_dirty()` is True.

    Covers `git diff HEAD` (staged + unstaged, tracked) plus the raw bytes of
    every untracked, non-ignored file. Returns None on a clean tree or when
    git is unavailable, so a clean run carries no misleading digest.
    """
    if not working_tree_dirty():
        return None
    h = hashlib.sha256()
    diff = _git("-c", "core.autocrlf=false", "diff", "HEAD")
    h.update((diff or "").encode("utf-8", "replace"))
    others = _git("ls-files", "--others", "--exclude-standard", "-z")
    for rel in (others or "").split("\0"):
        rel = rel.strip()
        if not rel:
            continue
        h.update(b"\0untracked\0")
        h.update(rel.encode("utf-8", "replace"))
        try:
            h.update((_REPO_ROOT / rel).read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return "sha256:" + h.hexdigest()


@functools.lru_cache(maxsize=1)
def _dirty_and_digest() -> tuple[bool, str | None]:
    """Computed once per process — `source_tree_or_diff_digest()` reads every
    untracked file, so it must not run per-task."""
    dirty = working_tree_dirty()
    return dirty, (source_tree_or_diff_digest() if dirty else None)


def vcs_provenance() -> dict:
    """The commit/tree provenance block stamped onto every report.

    `source_commit_sha` is only a faithful description of the evaluated code
    when `working_tree_dirty` is False; otherwise `source_tree_or_diff_digest`
    identifies the actual tree state.
    """
    import datetime as _dt

    dirty, digest = _dirty_and_digest()
    return {
        "source_commit_sha": current_commit_sha(),
        "working_tree_dirty": dirty,
        "source_tree_or_diff_digest": digest,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "dataset_version": DATASET_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
    }


def provenance_defaults() -> dict:
    """Version + VCS-truthfulness fields shared by every adapter's Provenance."""
    dirty, digest = _dirty_and_digest()
    return {
        "source_commit_sha": current_commit_sha(),
        "runtime": runtime_fingerprint(),
        "dataset_version": DATASET_VERSION,
        "prompt_version": PROMPT_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "working_tree_dirty": dirty,
        "source_tree_or_diff_digest": digest,
    }


def rubric_version(rubric_rel_path: str | None) -> str:
    if not rubric_rel_path:
        return "none"
    p = _EVALS_DIR / rubric_rel_path
    if not p.is_file():
        return "missing"
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def runtime_fingerprint() -> str:
    return f"py{sys.version_info.major}.{sys.version_info.minor} {platform.system().lower()}"


@dataclass
class Provenance:
    """Attached to every fixture and every result. Missing fields are reported."""

    fixture_kind: str = ""           # synthetic_regression | authored_placeholder | inprocess_capture | backend_capture | provider_capture
    evidence_level: str = ""         # one of EVIDENCE_LEVELS
    source_commit_sha: str = ""
    captured_at: str = ""            # ISO 8601 UTC
    capture_mode: str = ""           # offline | inprocess | backend | record
    application_path: str = ""       # e.g. "POST /api/sessions/{id}/messages/stream -> streaming_service.stream_chat_response"
    provider: str | None = None
    model: str | None = None
    model_revision: str | None = None
    runtime: str = ""
    dataset_version: str = ""
    prompt_version: str = ""
    evaluator_version: str = ""
    grader_version: str = "none"
    # VCS truthfulness: source_commit_sha only describes the evaluated code when
    # working_tree_dirty is False. When dirty, source_tree_or_diff_digest names
    # the actual tree state that produced this result.
    working_tree_dirty: bool = False
    source_tree_or_diff_digest: str | None = None
    from_product_code: bool = False  # did real backend/app code produce this response?
    tools_mocked: list[str] = field(default_factory=list)  # boundary mocks in effect
    human_reviewed: bool = False

    REQUIRED = (
        "fixture_kind", "evidence_level", "source_commit_sha", "captured_at",
        "capture_mode", "application_path", "runtime", "dataset_version",
    )

    def missing_fields(self) -> list[str]:
        out = []
        for name in self.REQUIRED:
            if not getattr(self, name):
                out.append(name)
        if self.evidence_level and self.evidence_level not in EVIDENCE_LEVELS:
            out.append(f"evidence_level(invalid:{self.evidence_level})")
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Provenance":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ── Redaction ────────────────────────────────────────────────────────────
#
# Key matching is EXACT (case-insensitive) or anchored-suffix — never a bare
# substring — so legitimate token-usage metadata stays visible:
#   token_count, input_tokens, output_tokens, total_tokens, tokenizer,
#   max_tokens, prompt_tokens, completion_tokens  → NOT redacted
# while genuinely sensitive fields are:
#   api_key, authorization, password, secret, client_secret, access_token,
#   refresh_token, id_token, cookie, set-cookie, …          → redacted
# Value-pattern redaction (bearer creds, Fernet blobs, provider keys, JWTs)
# runs on every string regardless of its key.

# Exact key names (compared as key.strip().lower()).
_SENSITIVE_KEYS_EXACT = frozenset({
    "authorization", "proxy-authorization",
    "cookie", "set-cookie",
    "x-csrf-token", "csrf_token", "csrf",
    "api_key", "apikey", "api-key",
    "secret", "client_secret", "client-secret",
    "password", "passwd", "pwd",
    "access_token", "refresh_token", "id_token",
    "session_token", "auth_token", "bearer_token",
    "private_key", "secret_key", "encryption_key",
    "x-api-key",
})
# Anchored suffixes: "<something>_api_key" etc. `_tokens` (plural) is
# deliberately absent so *_tokens usage counters survive.
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key", "-api-key", "_apikey",
    "_secret", "-secret",
    "_password", "-password",
    "_access_token", "_refresh_token", "_id_token", "_auth_token",
    "_private_key", "_secret_key",
)

_FERNET = re.compile(r"gAAAAA[A-Za-z0-9_\-]{20,}")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}")
_OPENROUTER = re.compile(r"sk-or-v1-[A-Za-z0-9]{8,}")
_PROVIDER_KEY = re.compile(r"sk-[A-Za-z0-9]{20,}")  # OpenAI-style
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")


def _is_sensitive_key(k) -> bool:
    if not isinstance(k, str):
        return False
    kl = k.strip().lower()
    return kl in _SENSITIVE_KEYS_EXACT or kl.endswith(_SENSITIVE_KEY_SUFFIXES)


def _redact_str(s: str) -> str:
    s = _JWT.sub("[REDACTED_JWT]", s)
    s = _FERNET.sub("[REDACTED_FERNET]", s)
    s = _BEARER.sub("Bearer [REDACTED]", s)
    s = _OPENROUTER.sub("sk-or-v1-[REDACTED]", s)
    s = _PROVIDER_KEY.sub("[REDACTED_KEY]", s)
    return s


def redact(obj):
    """Recursively strip sensitive keys/values from a payload or report.

    - A dict key is redacted only on an EXACT (case-insensitive) or
      anchored-suffix match against the sensitive-field lists — never a bare
      substring — so `token_count` / `input_tokens` / `max_tokens` / `tokenizer`
      survive while `api_key` / `authorization` / `access_token` do not.
    - A value that already looks masked (`****`) is kept so masking assertions
      can still run.
    - Every string value is scrubbed for JWTs, Fernet blobs, bearer headers,
      and provider-key formats regardless of its key.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_sensitive_key(k):
                if isinstance(v, str) and "****" in v:
                    out[k] = v  # already masked, keep for the assertion
                else:
                    out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str):
        return _redact_str(obj)
    return obj
