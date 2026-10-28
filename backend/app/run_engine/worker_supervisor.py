"""Embedded RunWorker lifecycle supervisor (Phase 1B.2R — MP-3C3A + lifecycle close-out).

The **only** sanctioned way to run the durable :class:`~app.run_engine.worker.RunWorker`
inside the FastAPI process. Created by the app lifespan **after** every boot check
(migrations + schema verification + HMAC-key check) has succeeded — never at
module import — and it owns exactly one worker's lifecycle for one app instance.

Execution model (close-out correction)
--------------------------------------
The sweep loop runs on **one explicitly owned ``threading.Thread(daemon=True)``**,
NOT ``asyncio.to_thread``. Empirically (CPython 3.11): ``asyncio.to_thread``
worker threads are ``daemon=False``, cancelling the awaiting task does NOT stop
the underlying blocking call, and ``loop.shutdown_default_executor()`` (run by
``asyncio.run`` on close) *joins* those threads — so an uncooperative in-flight
call there would keep application shutdown alive past the grace period. An owned
``daemon=True`` thread cannot block ``asyncio.run`` / process exit, gives a real
``is_alive()`` handle, and a bounded non-blocking join. It is never force-killed;
cooperative stop (the worker consults ``attach_stop`` at its guard / idle-tick
seams) is the primary path. The residual limitation — a truly uncooperative
synchronous third-party call inside ``poll_once`` — is documented below and in
the completion report; the only fully-bounded fix for that is a separate worker
process (a future MP-6 production topology, NOT built here).

* **poll loop** — reuses ``RunWorker.poll_once`` unchanged; each sub-call owns
  and closes its own DB session (no Session crosses a thread). An operational
  sweep failure (``Exception`` only) is classified to a **bounded** code, backed
  off (bounded exponential, reset after any clean sweep). ``BaseException`` that
  is not ``Exception`` — ``KeyboardInterrupt`` / ``SystemExit`` / ``GeneratorExit``
  / a cancellation — is NEVER caught here: it ends the loop and the thread with
  no classification and no backoff. Schema/invariant errors or a consecutive
  failure ceiling ⇒ the loop stops instead of spinning.
* **shutdown** — set the stop Event; wait up to ``run_worker_shutdown_grace_s``
  for the thread to finish (non-blocking poll). If it is still alive the status
  reports ``shutdown_deadline_exceeded`` and stays ``stopping`` / ``alive`` —
  it never claims the worker stopped while its thread is running.
* **status** — a minimal non-secret snapshot. No prompts, tool names/args/
  results, provider text, API keys, user/session/run ids, or DB URLs.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import threading
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from app.config import settings
from app.models.models import _utcnow_aware

logger = logging.getLogger(__name__)

_THREAD_NAME = "run-worker"


class SupervisorFatalError(RuntimeError):
    """A schema / configuration / invariant error the supervisor must NOT retry.
    ``code`` is a bounded slug — never raw exception text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class WorkerStatus:
    enabled: bool = False
    started: bool = False
    alive: bool = False
    stopping: bool = False
    last_successful_poll_at: Optional[str] = None      # ISO-8601 UTC or None
    last_bounded_error_code: Optional[str] = None      # bounded slug or None
    consecutive_failure_count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


# bounded, machine-readable supervisor error codes — never raw text
_SWEEP_ERROR = "sweep_error"
_DB_UNAVAILABLE = "db_unavailable"
_SCHEMA_ERROR = "schema_error"              # fatal
_CONFIG_ERROR = "config_error"             # fatal
_MAX_FAILURES = "max_consecutive_failures"  # fatal (retry ceiling hit)
_SHUTDOWN_DEADLINE = "shutdown_deadline_exceeded"
_INTERRUPTED = "worker_interrupted"         # BaseException (cancel / KI / SysExit / GenExit)

_FAILURE_CEILING = 12


def _classify(exc: Exception) -> tuple[str, bool]:
    """Map an OPERATIONAL sweep exception to ``(bounded_code, is_fatal)``.

    Only ``Exception`` instances ever reach here — cancellation /
    ``KeyboardInterrupt`` / ``SystemExit`` / ``GeneratorExit`` are not caught by
    the caller. Message text is inspected only for well-known schema markers and
    is never stored.
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in ("OperationalError", "ProgrammingError") and (
        "no such table" in msg or "no such column" in msg
        or "undefinedtable" in msg or "undefinedcolumn" in msg
        or "does not exist" in msg
    ):
        return (_SCHEMA_ERROR, True)
    if name in ("OperationalError", "InterfaceError", "DBAPIError", "TimeoutError"):
        return (_DB_UNAVAILABLE, False)
    if name in ("SupervisorFatalError",):
        return (getattr(exc, "code", _CONFIG_ERROR), True)
    return (_SWEEP_ERROR, False)


def _detected_web_concurrency() -> Optional[int]:
    """Best-effort read of the real web-process count from the environment
    (gunicorn / uvicorn / common orchestrators). Returns None when unknown."""
    import os
    import re

    for key in ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "UVICORN_WORKERS"):
        raw = (os.getenv(key) or "").strip()
        if raw.isdigit():
            return int(raw)
    m = re.search(r"(?:^|\s)(?:-w|--workers)[=\s]+(\d+)",
                  os.getenv("GUNICORN_CMD_ARGS", "") or "")
    if m:
        return int(m.group(1))
    return None


def assert_embedded_topology_ok() -> None:
    """Fail closed when the embedded worker would run in an unsafe topology.

    **Enforced automatically:** more than one web process against a **SQLite**
    file — startup is HALTED (``SupervisorFatalError``). The effective count is
    ``max(run_worker_web_concurrency, detected env count)`` where the env count
    comes from ``WEB_CONCURRENCY`` / ``GUNICORN_WORKERS`` / ``UVICORN_WORKERS`` /
    ``GUNICORN_CMD_ARGS -w N``.

    **NOT enforced automatically:** multi-process on **PostgreSQL**. The
    lease/``version``-CAS/``dispatch_records`` design is PG-safe by construction
    but the concurrency matrix is the MP-6 gate — no automatic multi-process
    protection is claimed there. ``run_worker_web_concurrency`` MUST be set to
    the real server process count; a detected value that disagrees is logged.
    """
    configured = max(1, int(settings.run_worker_web_concurrency))
    detected = _detected_web_concurrency()
    if detected is not None and detected != configured:
        logger.warning(
            "run_worker_web_concurrency=%d disagrees with a detected web-process "
            "count of %d - set run_worker_web_concurrency to the real count",
            configured, detected,
        )
    count = max(configured, detected or 1)
    if count > 1 and str(settings.database_url).startswith("sqlite"):
        raise SupervisorFatalError(_CONFIG_ERROR)


class RunWorkerSupervisor:
    """One embedded worker's lifecycle for one FastAPI app instance."""

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], object]] = None,
        worker_factory: Optional[Callable[[], object]] = None,
        now_fn: Optional[Callable[[], _dt.datetime]] = None,
    ) -> None:
        self._now = now_fn or _utcnow_aware
        if worker_factory is not None:
            self._worker_factory = worker_factory
        else:
            from app.database import SessionLocal
            from app.run_engine.worker import RunWorker

            sf = session_factory or SessionLocal
            # MP-4C: the executor broker client is built from operator
            # configuration and is INERT. Construction opens no socket and the
            # readiness probe is only REGISTERED here, never called, so an
            # unconfigured or unreachable executor changes nothing at startup:
            # ordinary chat and the worker still start normally.
            from app.run_engine import executor_broker as _broker_mod
            broker = None
            try:
                broker = _broker_mod.wire_from_env()
            except Exception:  # noqa: BLE001 - wiring must never be fatal
                broker = None
            _broker_is_ready = broker.ready if broker is not None else None
            self._worker_factory = lambda: RunWorker(
                sf, now_fn=now_fn, python_broker=broker,
                broker_ready=_broker_is_ready)
        self._stop = threading.Event()          # cooperative — read on the worker thread
        self._thread: Optional[threading.Thread] = None
        self._worker = None
        self._started = False
        self._status = WorkerStatus(enabled=self.enabled)
        self._backoff = float(max(1, int(settings.run_worker_backoff_min_s)))

    # ---- introspection ------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(settings.run_engine_enabled and settings.run_worker_enabled)

    @property
    def alive(self) -> bool:
        """The TRUTH: is the owned worker thread still running?"""
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        self._status.enabled = self.enabled
        self._status.started = self._started
        self._status.alive = self.alive
        return self._status.as_dict()

    def is_durable_path_ready(self) -> bool:
        """Readiness of the durable execution path. With the public durable Run
        API enabled the path is ready ONLY when the worker is alive, not in a
        fatal state, and NOT stopping / stuck-stopping."""
        if not settings.run_public_api_enabled:
            return True
        if not self.enabled:
            return False
        if self._status.stopping or self._stop.is_set():
            return False                       # stopping / stuck-stopping ⇒ not ready
        return self.alive and self._status.last_bounded_error_code not in (
            _SCHEMA_ERROR, _CONFIG_ERROR, _MAX_FAILURES, _SHUTDOWN_DEADLINE,
        )

    # ---- lifecycle --------------------------------------------------

    async def start(self) -> None:
        """Idempotent. Starts the owned worker thread only when enabled and the
        topology is safe. Raises :class:`SupervisorFatalError` (halting the
        lifespan) on an unsafe topology — startup fails closed."""
        self._status.enabled = self.enabled
        if self._started:
            return
        if not self.enabled:
            logger.info("RunWorkerSupervisor idle (run_engine_enabled=%s "
                        "run_worker_enabled=%s)", settings.run_engine_enabled,
                        settings.run_worker_enabled)
            return
        assert_embedded_topology_ok()          # fail closed BEFORE any thread
        self._worker = self._worker_factory()
        self._worker.attach_stop(self._stop.is_set)
        self._stop.clear()
        self._started = True
        self._status.stopping = False
        self._status.last_bounded_error_code = None
        self._status.consecutive_failure_count = 0
        # daemon=True: a stuck worker thread can never block asyncio.run() /
        # process exit. We never RELY on the abrupt daemon kill — cooperative
        # stop + bounded join is the normal path; the kill is only the safety
        # net for a truly uncooperative in-flight third-party call.
        self._thread = threading.Thread(target=self._run_loop, name=_THREAD_NAME,
                                        daemon=True)
        self._thread.start()
        logger.info("RunWorkerSupervisor started")

    def _run_loop(self) -> None:
        """Runs ON the owned daemon thread. Blocking DB work only — no asyncio.

        ``except Exception`` deliberately does NOT catch ``KeyboardInterrupt`` /
        ``SystemExit`` / ``GeneratorExit`` / a cancellation: any of those ends
        the loop (and the thread) immediately with no classification, no
        ``consecutive_failure_count`` bump, and no backoff.
        """
        try:
            while not self._stop.is_set():
                try:
                    self._worker.poll_once()
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit,
                        GeneratorExit) as _interrupt:
                    # NOT an operational failure: never classified as
                    # sweep_error / db_unavailable, never counted, never backed
                    # off. Minimal cleanup, then re-raise so the thread ends.
                    self._status.last_bounded_error_code = _INTERRUPTED
                    logger.warning("RunWorkerSupervisor worker thread interrupted "
                                   "(%s) - ending, not retrying",
                                   type(_interrupt).__name__)
                    raise
                except Exception as exc:  # OPERATIONAL sweep failure ONLY
                    code, fatal = _classify(exc)
                    self._status.consecutive_failure_count += 1
                    self._status.last_bounded_error_code = code
                    if self._status.consecutive_failure_count >= _FAILURE_CEILING:
                        code, fatal = _MAX_FAILURES, True
                        self._status.last_bounded_error_code = code
                    if fatal:
                        logger.critical("RunWorkerSupervisor fatal sweep error "
                                        "(%s) - stopping; NOT retrying", code)
                        break
                    logger.error("RunWorkerSupervisor sweep failed (%s); backoff "
                                 "%.0fs (consecutive=%d)", code, self._backoff,
                                 self._status.consecutive_failure_count)
                    self._stop.wait(self._backoff)          # interruptible sleep
                    self._backoff = min(self._backoff * 2,
                                        float(int(settings.run_worker_backoff_max_s)))
                    continue
                # clean sweep — reset backoff + failure state
                self._status.last_successful_poll_at = self._now().isoformat()
                self._status.consecutive_failure_count = 0
                self._status.last_bounded_error_code = None
                self._backoff = float(max(1, int(settings.run_worker_backoff_min_s)))
                self._stop.wait(float(int(settings.run_worker_poll_interval_s)))
        finally:
            # only reached when the loop actually returns (cooperative stop, a
            # fatal error, or a propagating BaseException) — NOT while a blocking
            # poll_once is still running.
            self._status.alive = False

    async def stop(self, *, grace_s: Optional[float] = None) -> None:
        """Cooperative shutdown. Set the stop Event, then wait up to
        ``run_worker_shutdown_grace_s`` for the owned thread to finish (a
        non-blocking poll). NEVER force-kills the thread. If the grace expires
        while the thread is still alive the status stays ``stopping`` /
        ``alive`` and records ``shutdown_deadline_exceeded`` — it does not claim
        the worker stopped."""
        if not self._started:
            self._status.started = False
            return
        self._status.stopping = True
        self._stop.set()                       # no new sweeps; provider guard aborts
        grace = float(grace_s if grace_s is not None
                      else int(settings.run_worker_shutdown_grace_s))
        t = self._thread
        if t is not None:
            deadline = self._now() + _dt.timedelta(seconds=max(0.0, grace))
            while t.is_alive() and self._now() < deadline:
                await asyncio.sleep(0.05)
            if t.is_alive():
                # HONEST: the worker thread is STILL RUNNING (an uncooperative
                # in-flight call). Do not kill it, do not claim it stopped.
                self._status.last_bounded_error_code = _SHUTDOWN_DEADLINE
                self._status.stopping = True    # stays stopping; alive stays True
                logger.error(
                    "RunWorkerSupervisor shutdown grace (%.0fs) expired; the "
                    "worker thread is still alive (daemon - it will not block "
                    "process exit and will be abandoned at interpreter exit)",
                    grace)
                return
        # the thread actually finished
        self._started = False
        self._status.started = False
        self._status.alive = False
        self._status.stopping = False
        logger.info("RunWorkerSupervisor stopped")


__all__ = [
    "RunWorkerSupervisor", "WorkerStatus", "SupervisorFatalError",
    "assert_embedded_topology_ok",
]
