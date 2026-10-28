"""MP-4C — fail-closed STARTUP readiness for the executor service.

Answers exactly one question: *is this service able to accept work right now?*
It is deliberately NOT a second authority for `real_python_execution_available()`
(app/run_engine/sandbox_policy.py). That predicate asks whether model-emitted
Python may execute, and it stays False. Service readiness never feeds it.

ALL of these must hold, or the service is UNREADY:
  1. the Docker daemon is reachable,
  2. the EXACT pinned image is present locally with the pinned digest,
  3. the full adversarial isolation probe passes.
Not one of these pulls an image — never at startup, never on a request.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from executor_service import image as image_mod

DAEMON_UNAVAILABLE = "executor_daemon_unavailable"
IMAGE_NOT_PROVISIONED = "executor_image_not_provisioned"
IMAGE_DIGEST_MISMATCH = "executor_image_digest_mismatch"
PROBE_FAILED = "executor_isolation_probe_failed"

PROBE_REL = os.path.join("tests", "smoke", "mp4c_isolation_probe.py")
PROBE_TIMEOUT_S = 900
DAEMON_TIMEOUT_S = 30


@dataclass(frozen=True)
class ServiceReadiness:
    ready: bool
    reasons: Tuple[str, ...] = ()
    detail: Dict = field(default_factory=dict)

    @property
    def failed_checks(self) -> Tuple[str, ...]:
        """Bounded names of the probe checks that failed (may be empty)."""
        probe = (self.detail or {}).get("probe") or {}
        return tuple(probe.get("failed") or ())


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


def _docker(args, *, timeout=60):
    p = subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def daemon_available(*, docker: Optional[Callable] = None) -> bool:
    """True only when the daemon answers. Never pulls."""
    fn = docker or _docker
    try:
        rc, _out, _err = fn(["version", "--format", "{{.Server.Version}}"],
                            timeout=DAEMON_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — an unreachable daemon fails closed
        return False
    return rc == 0


def run_isolation_probe(*, repo_root: str,
                        probe_runner: Optional[Callable] = None) -> Tuple[bool, Dict]:
    """Run the FULL isolation probe. (ok, BOUNDED diagnostic detail). Never pulls.

    The detail always records: duration, exit code, containerized mode, scope
    counts, the failing check NAMES, and — on a crash — the exception TYPE and a
    bounded message. It never contains model code, a token or child output.
    """
    started = time.monotonic()
    if probe_runner is not None:
        ok, detail = probe_runner()
        out = dict(detail or {})
        out.setdefault("duration_ms", _ms(started))
        return bool(ok), out
    env = dict(os.environ)
    parts = [os.path.join(repo_root, "backend"), repo_root]
    env["PYTHONPATH"] = os.pathsep.join(
        parts + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(repo_root, PROBE_REL)],
            capture_output=True, timeout=PROBE_TIMEOUT_S, cwd=repo_root, env=env)
    except subprocess.TimeoutExpired:
        return False, {"probe": "timeout", "duration_ms": _ms(started),
                       "timeout_s": PROBE_TIMEOUT_S}
    except Exception as exc:  # noqa: BLE001
        return False, {"probe": "error", "exception": type(exc).__name__,
                       "message": str(exc)[:200], "duration_ms": _ms(started)}
    text = (p.stdout or b"").decode("utf-8", "replace")
    duration = _ms(started)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace")
        return False, {"probe": "nonzero_exit", "exit": p.returncode,
                       "duration_ms": duration,
                       "stdout_tail": text[-400:], "stderr_tail": err[-200:]}
    for line in text.splitlines():
        if line.startswith("PROBE_RESULT "):
            try:
                payload = json.loads(line[len("PROBE_RESULT "):])
            except ValueError:
                return False, {"probe": "unparseable", "duration_ms": duration,
                               "stdout_tail": text[-200:]}
            return bool(payload.get("ok")), {
                "probe": "ok" if payload.get("ok") else "failed",
                "checks": payload.get("checks"),
                "failed": list(payload.get("failed") or [])[:8],
                "scopes": payload.get("scopes"),
                "containerized": payload.get("containerized"),
                "duration_ms": duration,
            }
    return False, {"probe": "no_result", "duration_ms": duration,
                   "stdout_tail": text[-400:]}


def cheap_availability(*, docker: Optional[Callable] = None,
                       expected_digest: Optional[str] = None,
                       ref: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """PER-REQUEST check: daemon reachable + the EXACT pinned image present.

    Cheap (two daemon round-trips) and never pulls. This is deliberately NOT the
    full probe, which stays a one-time STARTUP gate: the isolation posture cannot
    change while the process lives, but the daemon or the image CAN disappear.
    """
    if not daemon_available(docker=docker):
        return False, DAEMON_UNAVAILABLE
    kw = {}
    if expected_digest is not None:
        kw["expected_digest"] = expected_digest
    if ref is not None:
        kw["ref"] = ref
    ok, reason = image_mod.readiness(**kw)
    if not ok:
        return False, (IMAGE_DIGEST_MISMATCH if reason == "python_image_digest_mismatch"
                       else IMAGE_NOT_PROVISIONED)
    return True, None


def check_readiness(*, repo_root: str, expected_digest: Optional[str] = None,
                    ref: Optional[str] = None,
                    docker: Optional[Callable] = None,
                    probe_runner: Optional[Callable] = None) -> ServiceReadiness:
    """Fail closed. Returns every failed requirement, in a fixed order."""
    reasons = []
    timings: Dict = {}
    detail: Dict = {"timings_ms": timings}

    t0 = time.monotonic()
    daemon_ok = daemon_available(docker=docker)
    timings["daemon"] = _ms(t0)
    if not daemon_ok:
        reasons.append(DAEMON_UNAVAILABLE)
        detail["daemon"] = "unavailable"
        return ServiceReadiness(False, tuple(reasons), detail)
    detail["daemon"] = "ok"

    kw = {}
    if expected_digest is not None:
        kw["expected_digest"] = expected_digest
    if ref is not None:
        kw["ref"] = ref
    t1 = time.monotonic()
    img_ok, img_reason = image_mod.readiness(**kw)
    timings["image"] = _ms(t1)
    if not img_ok:
        reasons.append(IMAGE_DIGEST_MISMATCH if img_reason == "python_image_digest_mismatch"
                       else IMAGE_NOT_PROVISIONED)
        detail["image"] = img_reason
        return ServiceReadiness(False, tuple(reasons), detail)
    detail["image"] = "ok"

    probe_ok, probe_detail = run_isolation_probe(repo_root=repo_root,
                                                 probe_runner=probe_runner)
    detail["probe"] = probe_detail
    if not probe_ok:
        reasons.append(PROBE_FAILED)
        return ServiceReadiness(False, tuple(reasons), detail)
    return ServiceReadiness(True, (), detail)
