"""MP-4C - the narrowly authenticated INTERNAL executor endpoint (signed grants).

Surface:
  GET  /healthz      -> readiness: the CACHED startup probe result PLUS a LIVE
                        cheap availability check (daemon + pinned image), 200/503
  POST /v1/execute   -> body {grant, code, run_id} ONLY

There is NO field for a user, a tenant, an image, an argv, a mount, a network
mode, an environment variable or a resource limit: identity arrives inside the
SIGNED GRANT, so a browser or a model cannot choose it. extra="forbid" rejects
any attempt, and validation errors never echo the caller's input.

What /healthz guarantees after startup: the isolation posture proven ONCE at
startup is reported from the cached evaluation, and availability that CAN change
(daemon reachable, exact pinned image present) is re-checked cheaply on every
call. The full ~100 s probe is NEVER re-run per request or per health poll.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from executor_service.service import ExecutionService

BODY_LIMIT_BYTES = 64 * 1024


class ExecutionBody(BaseModel):
    """The COMPLETE client-controlled surface. No identity, no docker options."""

    model_config = ConfigDict(extra="forbid")

    grant: str
    code: str
    run_id: str


class ReservationBody(BaseModel):
    """A reserve/release request carries ONLY a signed grant.

    Module scope is REQUIRED: FastAPI resolves route annotations with the MODULE
    namespace, so a class defined inside create_app raises
    "NameError: name 'ReservationBody' is not defined" at route registration and
    the whole app fails to build.
    """

    model_config = ConfigDict(extra="forbid")

    grant: str


def _status_code(status: str) -> int:
    from executor_service.runner import (STATUS_FAILED, STATUS_OOM_KILLED,
                                         STATUS_OUTCOME_UNKNOWN, STATUS_TIMEOUT,
                                         STATUS_UNAVAILABLE)
    from executor_service.service import (STATUS_BUSY, STATUS_CODE_REJECTED,
                                          STATUS_NOT_READY, STATUS_NONCE_UNAVAILABLE,
                                          STATUS_RELEASED, STATUS_REPLAY,
                                          STATUS_REQUEST_REJECTED,
                                          STATUS_RESERVATION_EXISTS,
                                          STATUS_RESERVATION_MISSING, STATUS_RESERVED)
    from mp4c_contract import grant as grant_contract
    return {
        grant_contract.MALFORMED: 400,
        grant_contract.CLAIM_MISSING: 400,
        grant_contract.LIFETIME_INVALID: 400,
        grant_contract.CODE_BINDING_MISMATCH: 400,
        grant_contract.BAD_SIGNATURE: 401,
        grant_contract.EXPIRED: 401,
        STATUS_RESERVATION_MISSING: 409,
        STATUS_RESERVED: 200,
        STATUS_RESERVATION_EXISTS: 409,
        STATUS_RELEASED: 200,
        STATUS_REPLAY: 409,
        STATUS_REQUEST_REJECTED: 400,
        STATUS_CODE_REJECTED: 400,
        STATUS_NONCE_UNAVAILABLE: 503,
        STATUS_NOT_READY: 503,
        STATUS_BUSY: 429,
        STATUS_TIMEOUT: 504,
        STATUS_OOM_KILLED: 502,
        STATUS_OUTCOME_UNKNOWN: 502,
        STATUS_UNAVAILABLE: 503,
        STATUS_FAILED: 502,
    }.get(status, 502)


def _bounded(**kw) -> dict:
    return {k: v for k, v in kw.items()
            if k in ("ok", "status", "output", "output_truncated")}


def create_app(*, service: ExecutionService,
               readiness_fn: Optional[Callable] = None,
               availability_fn: Optional[Callable] = None) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        if readiness_fn is not None:
            loop = asyncio.get_running_loop()
            application.state.readiness = await loop.run_in_executor(None,
                                                                    readiness_fn)
            if not application.state.readiness.ready:
                print("executor_service: NOT READY reasons=%s"
                      % ",".join(application.state.readiness.reasons))
        yield

    app = FastAPI(title="Thunder AI - Python executor (internal)",
                  docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=_lifespan)

    def _current_readiness():
        cached = getattr(app.state, "readiness", None)
        if cached is None:
            cached = readiness_fn() if readiness_fn else None
            app.state.readiness = cached
        return cached

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # Bounded and NON-ECHOING: the caller's input never comes back.
        kinds = sorted({str(e.get("type")) for e in exc.errors()})
        return JSONResponse(status_code=422,
                            content={"ok": False,
                                     "status": "executor_request_rejected",
                                     "detail": kinds[:8]})

    @app.get("/healthz")
    async def healthz():
        startup = _current_readiness()
        live_ok, live_reason = True, None
        if availability_fn is not None:
            loop = asyncio.get_running_loop()
            try:
                live_ok, live_reason = await loop.run_in_executor(None, availability_fn)
            except Exception:  # noqa: BLE001 - a failed live check is NOT ready
                live_ok, live_reason = False, "executor_daemon_unavailable"
        startup_ok = bool(startup.ready) if startup is not None else False
        reasons = list(startup.reasons) if startup is not None else \
            ["executor_readiness_unknown"]
        if not live_ok and live_reason and live_reason not in reasons:
            reasons.append(live_reason)
        probe = (startup.detail or {}).get("probe") or {} if startup is not None else {}
        body = {
            "ready": bool(startup_ok and live_ok),
            "reasons": reasons,
            "startup": {
                "ready": startup_ok,
                "timings_ms": dict((startup.detail or {}).get("timings_ms") or {})
                if startup is not None else {},
                "probe": {k: probe.get(k) for k in
                          ("probe", "checks", "duration_ms", "containerized",
                           "exit", "exception")
                          if probe.get(k) is not None},
            },
            "live": {"daemon_and_image": "ok" if live_ok else (live_reason or "failed")},
        }
        if probe.get("failed"):
            body["failed_checks"] = list(probe["failed"])[:8]
        return JSONResponse(status_code=200 if body["ready"] else 503, content=body)

    @app.post("/v1/reserve")
    async def reserve(body: ReservationBody):
        """PRE-DISPATCH capacity reservation. Reserving NEVER runs code, so a busy
        refusal here lets the backend avoid committing DispatchRecord.started."""
        outcome = service.reserve(grant=body.grant)
        return JSONResponse(status_code=200 if outcome.ok
                            else _status_code(outcome.status),
                            content={"ok": outcome.ok, "status": outcome.status})

    @app.post("/v1/release")
    async def release(body: ReservationBody):
        """Abandon a reservation (approval denied or cancelled). Frees the slot."""
        outcome = service.release(grant=body.grant)
        return JSONResponse(status_code=200 if outcome.ok
                            else _status_code(outcome.status),
                            content={"ok": outcome.ok, "status": outcome.status})

    @app.post("/v1/execute")
    async def execute(body: ExecutionBody,
                      authorization: Optional[str] = Header(default=None)):
        # The Authorization header is NO LONGER the credential: the signed grant
        # in the body IS the credential. The header is ignored entirely.
        del authorization
        outcome = service.execute(grant=body.grant, code=body.code,
                                  run_id=body.run_id)
        return JSONResponse(status_code=200 if outcome.ok
                            else _status_code(outcome.status),
                            content=_bounded(ok=outcome.ok, status=outcome.status,
                                             output=outcome.output,
                                             output_truncated=outcome.truncated))

    return app


def build_default_app(*, readiness=None, key=None, nonce_store=None) -> FastAPI:
    """Factory for uvicorn ...:build_default_app --factory.

    The signing key and the nonce store are constructed HERE when not supplied,
    and BOTH fail closed: a missing/trivial key or an unusable store means the
    service does not serve.
    """
    import os

    from executor_service import readiness as readiness_mod
    from executor_service.contract import DEFAULT_OWNER
    from executor_service.nonce_store import store_from_env
    from mp4c_contract import grant as grant_contract

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readiness_state = {}
    if readiness is not None:
        readiness_state["value"] = readiness

    def _readiness_fn():
        if "value" not in readiness_state:
            readiness_state["value"] = readiness_mod.check_readiness(
                repo_root=repo_root)
        return readiness_state["value"]

    resolved_key = key if key is not None else grant_contract.key_from_env()
    store = nonce_store if nonce_store is not None else store_from_env()
    owner = os.environ.get("EXECUTOR_SERVICE_OWNER", DEFAULT_OWNER)
    service = ExecutionService(
        key=resolved_key, nonce_store=store, owner=owner,
        ready=lambda: _readiness_fn().ready,
        preflight=readiness_mod.cheap_availability)
    return create_app(service=service, readiness_fn=_readiness_fn,
                      availability_fn=readiness_mod.cheap_availability)
