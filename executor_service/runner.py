"""MP-4C — one container per request: bounded, timed out, ALWAYS cleaned up.

The runner is the only place a container is launched. It writes the plan's code to
the container's stdin, enforces a wall-clock timeout, maps the exit status to a
BOUNDED slug, and removes the container it owns on every path.

It never logs, and never returns, the child's raw failure output: a failed run
yields a slug plus an exit code only. Successful stdout is returned because that
is the program's result (bounded by `max_output_bytes`).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

from executor_service.contract import OWNER_LABEL, ExecutionPlan

MAX_OUTPUT_BYTES = 8192
DEFAULT_TIMEOUT_S = 20.0

STATUS_OK = "ok"
STATUS_TIMEOUT = "executor_timeout"
STATUS_OOM_KILLED = "executor_oom_killed"
STATUS_UNAVAILABLE = "executor_runtime_unavailable"
STATUS_FAILED = "executor_failed"
STATUS_INTERRUPTED = "executor_interrupted"
#: Raised mid-run: the container may or may not have started, so the exact
#: outcome is UNKNOWN. It is never retried (the nonce was already consumed).
STATUS_OUTCOME_UNKNOWN = "executor_outcome_unknown"


@dataclass(frozen=True)
class RunOutcome:
    """Bounded result. `output` is empty for every non-ok status."""

    status: str
    output: str = ""
    truncated: bool = False
    exit_code: Optional[int] = None


def _docker(args, *, timeout=60):
    p = subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


class ContainerRunner:
    """Runs ONE plan against the fixed contract. Injectable seams for tests."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_output_bytes: int = MAX_OUTPUT_BYTES,
                 popen: Optional[Callable] = None,
                 docker: Optional[Callable] = None) -> None:
        self.timeout_s = float(timeout_s)
        self.max_output_bytes = int(max_output_bytes)
        self._popen = popen or subprocess.Popen
        self._docker = docker or _docker

    def force_remove(self, name: str) -> None:
        try:
            self._docker(["rm", "-f", name])
        except Exception:  # noqa: BLE001 — cleanup must never mask the real outcome
            pass

    def run(self, plan: ExecutionPlan) -> RunOutcome:
        proc = None
        try:
            proc = self._popen(list(plan.argv), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                out, _err = proc.communicate(input=plan.stdin_code,
                                             timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                # Remove the container first (that unblocks the CLI), then reap.
                self.force_remove(plan.name)
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    proc.communicate(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
                return RunOutcome(status=STATUS_TIMEOUT)
            rc = proc.returncode
            raw = out or b""
            if rc == 0:
                return RunOutcome(status=STATUS_OK,
                                  output=raw[: self.max_output_bytes].decode("utf-8", "replace"),
                                  truncated=len(raw) > self.max_output_bytes,
                                  exit_code=0)
            if rc == 137:                       # cgroup OOM kill
                return RunOutcome(status=STATUS_OOM_KILLED, exit_code=137)
            if rc in (125, 126, 127):           # daemon/engine refused to run it
                return RunOutcome(status=STATUS_UNAVAILABLE, exit_code=rc)
            return RunOutcome(status=STATUS_FAILED, exit_code=rc)
        finally:
            # Belt and braces: remove OUR container on every path, including a
            # raised exception ("broker interruption"). Never touches others.
            self.force_remove(plan.name)


def owned_container_names(owner: str, *, docker: Optional[Callable] = None) -> list:
    """Names of containers carrying OUR ownership label (never a name guess)."""
    fn = docker or _docker
    rc, out, _ = fn(["ps", "-a",
                     "--filter", "label=%s=%s" % (OWNER_LABEL, owner),
                     "--format", "{{.Names}}"])
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def sweep_owned_orphans(owner: str, *, docker: Optional[Callable] = None) -> list:
    """Remove ONLY containers labelled as owned by `owner`. Returns removed names.

    Unrelated containers (no label, or a different owner) are never touched.
    """
    fn = docker or _docker
    removed = []
    for name in owned_container_names(owner, docker=fn):
        rc, _, _ = fn(["rm", "-f", name])
        if rc == 0:
            removed.append(name)
    return removed
