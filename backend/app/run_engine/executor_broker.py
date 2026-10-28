"""MP-4C - the production BROKER CLIENT for the isolated executor service.

This is the ONLY transport that may execute real model-generated Python. It is
deliberately INERT: constructing it performs no I/O, opens no socket and reads
nothing but the environment, so wiring it into the worker supervisor adds NO
startup dependency and a broker outage can never stop ordinary chat or the
worker from starting.

Contract with the executor deployment's app module:
  GET  /healthz      -> 200 {ready: true} when the pinned image, the Docker
                        daemon and the startup isolation probe are ALL valid
  POST /v1/reserve   -> {grant}
  POST /v1/release   -> {grant}
  POST /v1/execute   -> {grant, code, run_id}

Every response is reduced to a BOUNDED result - an ``ok`` flag, a short status
slug and (for execute) the output text. A grant, the code, the signing secret and
the raw response body are never logged and never placed in an exception message.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

#: Bounded, machine-readable status slugs. Anything unrecognised degrades to one
#: of these rather than leaking a body fragment into a Run reason.
STATUS_OK = "executor_ok"
STATUS_UNAVAILABLE = "executor_unavailable"
STATUS_REQUEST_REJECTED = "executor_request_rejected"
STATUS_ERROR = "executor_error"

#: No response body is read beyond this many bytes.
BODY_LIMIT_BYTES = 256 * 1024

_SLUG_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_SLUG_MAX = 48


class ExecutorBrokerError(RuntimeError):
    """Bounded transport failure. The message is a fixed slug, never a body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BrokerResult:
    """The COMPLETE client-visible outcome. No identity, no raw body."""

    ok: bool
    status: str
    output: str = ""
    output_truncated: bool = False


def _slug(value, fallback: str) -> str:
    """A short, lowercase, machine-readable slug or *fallback*."""
    text = str(value or "").strip().lower()
    if not text or len(text) > _SLUG_MAX:
        return fallback
    if any(ch not in _SLUG_OK for ch in text):
        return fallback
    return text


def _normalize_base(base_url: str) -> str:
    text = str(base_url or "").strip().rstrip("/")
    if not text.startswith(("http://", "https://")):
        raise ExecutorBrokerError(STATUS_REQUEST_REJECTED)
    return text


def _http_opener(method: str, url: str, data: Optional[bytes],
                 timeout: float) -> Tuple[int, bytes]:
    """The DEFAULT transport. Returns (status, bounded body) and never raises
    for an HTTP error status - the executor reports refusals as JSON bodies."""
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read(
                BODY_LIMIT_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(BODY_LIMIT_BYTES)
        except Exception:  # noqa: BLE001 - a broken error body is just "no body"
            body = b""
        return int(getattr(exc, "code", 0) or 0), body


class ExecutorBrokerClient:
    """PREDISPATCH_RESERVE + EXECUTE client. Construction is side-effect free."""

    def __init__(self, *, base_url: str, request_timeout_s: float = 120.0,
                 health_timeout_s: float = 10.0,
                 opener: Optional[Callable] = None) -> None:
        self._base = _normalize_base(base_url)
        self._request_timeout = max(1.0, float(request_timeout_s))
        self._health_timeout = max(1.0, float(health_timeout_s))
        self._opener = opener or _http_opener

    # ---- readiness ----------------------------------------------------

    def ready(self) -> bool:
        """LIVE readiness. Any failure - transport, status or body - is NOT ready.

        This is the only probe the single authority consults, so it must be
        strictly fail-closed: an unparseable 200 is not readiness.
        """
        try:
            status, body = self._call("GET", "/healthz", None,
                                      self._health_timeout)
        except Exception:  # noqa: BLE001
            return False
        if status != 200:
            return False
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return False
        return isinstance(payload, dict) and payload.get("ready") is True

    # ---- the four operations ------------------------------------------

    def reserve(self, *, grant: str) -> BrokerResult:
        return self._post("/v1/reserve", {"grant": grant})

    def release(self, *, grant: str) -> BrokerResult:
        return self._post("/v1/release", {"grant": grant})

    def execute(self, *, grant: str, code: str, run_id: str) -> BrokerResult:
        return self._post("/v1/execute",
                          {"grant": grant, "code": code, "run_id": run_id})

    # ---- internals ----------------------------------------------------

    def _post(self, path: str, payload: dict) -> BrokerResult:
        try:
            status, body = self._call("POST", path, payload,
                                      self._request_timeout)
        except Exception:  # noqa: BLE001 - a transport failure is a refusal
            return BrokerResult(False, STATUS_UNAVAILABLE)
        try:
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except Exception:  # noqa: BLE001
            return BrokerResult(False, self._status_without_body(status))
        ok = data.get("ok") is True
        fallback = STATUS_ERROR if ok else STATUS_UNAVAILABLE
        output = data.get("output")
        return BrokerResult(
            ok=ok,
            status=_slug(data.get("status"), fallback),
            output=output if isinstance(output, str) else "",
            output_truncated=bool(data.get("output_truncated")),
        )

    @staticmethod
    def _status_without_body(http_status: int) -> str:
        if http_status in (400, 401, 403, 409, 422):
            return STATUS_REQUEST_REJECTED
        if http_status == 429:
            return "executor_quota_exceeded"
        if http_status in (500, 502, 503, 504):
            return STATUS_UNAVAILABLE
        return STATUS_ERROR

    def _call(self, method: str, path: str, payload: Optional[dict],
              timeout: float) -> Tuple[int, bytes]:
        url = self._base + path
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")
        return self._opener(method, url, data, timeout)


def _float_env(env: Mapping, name: str, default: float) -> float:
    try:
        return float(str(env.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def build_from_env(env: Optional[Mapping] = None) -> Optional[ExecutorBrokerClient]:
    """Build the client from operator configuration, or None (fail closed).

    Returns None - so nothing is wired and the single authority stays False -
    when the endpoint is unset, or when the shared signing secret is absent or
    trivial (mp4c_contract.grant.key_from_env raises). No I/O is performed.
    """
    e = env if env is not None else os.environ
    from app.run_engine import sandbox_policy
    from mp4c_contract import grant as grant_contract

    base = str(e.get(sandbox_policy.EXECUTOR_URL_ENV) or "").strip()
    if not base:
        return None
    try:
        grant_contract.key_from_env(e)          # validated, then discarded
    except Exception:  # noqa: BLE001 - no signing secret means no broker
        return None
    try:
        return ExecutorBrokerClient(
            base_url=base,
            request_timeout_s=_float_env(e, "EXECUTOR_SERVICE_TIMEOUT_S", 120.0),
            health_timeout_s=_float_env(e, "EXECUTOR_SERVICE_HEALTH_TIMEOUT_S",
                                        10.0),
        )
    except ExecutorBrokerError:
        return None


def install_readiness_probe(client: ExecutorBrokerClient) -> None:
    """Register ``client.ready`` as the ONE authority's live probe.

    Registration is side-effect free: the probe runs later, and only inside
    ``real_python_execution_available()``. It can therefore only ever DENY.
    """
    from app.run_engine import sandbox_policy
    sandbox_policy.register_executor_readiness_probe(client.ready)


def wire_from_env(env: Optional[Mapping] = None) -> Optional[ExecutorBrokerClient]:
    """build_from_env + install_readiness_probe. Returns None when unconfigured."""
    client = build_from_env(env)
    if client is None:
        return None
    install_readiness_probe(client)
    return client


__all__ = [
    "BrokerResult", "ExecutorBrokerClient", "ExecutorBrokerError",
    "build_from_env", "install_readiness_probe", "wire_from_env",
    "STATUS_OK", "STATUS_UNAVAILABLE", "STATUS_REQUEST_REJECTED", "STATUS_ERROR",
    "BODY_LIMIT_BYTES",
]