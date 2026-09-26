"""MP-4C — run the dedicated executor service on loopback only.

    EXECUTOR_SERVICE_TOKENS='{"<token>": {"user_id": 1}}' \
    python -m executor_service

Binds 127.0.0.1 and refuses to start unless readiness passes (daemon reachable,
exact pinned image present, full isolation probe green). Readiness NEVER pulls an
image.
"""
from __future__ import annotations

import json
import os
import sys

DEFAULT_PORT = 8091

#: Loopback is the DEFAULT and always allowed. Inside the container deployment the
#: endpoint must be reachable by the backend over a dedicated internal network,
#: which requires a non-loopback bind; that is never implicit.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
ALLOW_INTERNAL_BIND_ENV = "EXECUTOR_SERVICE_ALLOW_INTERNAL_BIND"
_TRUTHY = ("1", "true", "yes", "on")


def bind_is_allowed(host: str, env) -> bool:
    """Loopback always; any other bind needs an explicit, documented opt-in."""
    if host in LOOPBACK_HOSTS:
        return True
    return str(env.get(ALLOW_INTERNAL_BIND_ENV, "")).strip().lower() in _TRUTHY


def main(argv=None) -> int:
    from executor_service.contract import DEFAULT_OWNER
    from executor_service.readiness import check_readiness
    from executor_service.runner import sweep_owned_orphans

    # Logging is configured BEFORE readiness so the bounded diagnostics of a
    # failing startup are actually captured.
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("executor_service")
    log.setLevel(logging.INFO)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = check_readiness(repo_root=repo_root)
    probe = (result.detail or {}).get("probe") or {}
    log.info(
        "executor_readiness ready=%s reasons=%s failed_checks=%s timings_ms=%s "
        "probe_status=%s probe_checks=%s probe_duration_ms=%s containerized=%s "
        "probe_exception=%s",
        result.ready, ",".join(result.reasons) or "-",
        ",".join(result.failed_checks) or "-",
        json.dumps((result.detail or {}).get("timings_ms") or {}),
        probe.get("probe"), probe.get("checks"), probe.get("duration_ms"),
        probe.get("containerized"), probe.get("exception") or "-")
    if not result.ready:
        print("executor_service: refusing to start; NOT READY reasons=%s "
              "failed_checks=%s"
              % (",".join(result.reasons), ",".join(result.failed_checks) or "-"),
              file=sys.stderr)
        return 2

    # Restart recovery BEFORE serving: remove ONLY containers carrying OUR stable
    # owner label (a crashed previous instance's leftovers). Unrelated containers
    # are never touched. Then start with empty in-memory quota counters.
    owner = os.environ.get("EXECUTOR_SERVICE_OWNER", DEFAULT_OWNER)
    removed = sweep_owned_orphans(owner)
    if removed:
        print("executor_service: removed %d owned orphan container(s)" % len(removed))

    # The signing key and the durable replay guard must BOTH be usable, or the
    # service refuses to serve. Neither value is ever logged.
    from executor_service.nonce_store import NonceStoreError, store_from_env
    from mp4c_contract import grant as grant_contract
    try:
        key = grant_contract.key_from_env()
    except grant_contract.GrantError as exc:
        print("executor_service: refusing to start; %s" % exc.code, file=sys.stderr)
        return 2
    try:
        nonce_store = store_from_env()
    except NonceStoreError:
        print("executor_service: refusing to start; nonce store unavailable",
              file=sys.stderr)
        return 2
    log.info("executor_nonce_store path=%s records=%s",
             nonce_store.path, nonce_store.count())

    import uvicorn
    from executor_service.app import build_default_app

    host = os.environ.get("EXECUTOR_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("EXECUTOR_SERVICE_PORT", DEFAULT_PORT))
    if not bind_is_allowed(host, os.environ):
        print("executor_service: refusing a non-loopback bind (%s). Loopback is "
              "the default; set %s=1 ONLY when this endpoint is reachable solely "
              "over a dedicated internal network with no published host port."
              % (host, ALLOW_INTERNAL_BIND_ENV), file=sys.stderr)
        return 2
    # The SAME readiness result is injected: no second probe evaluation.
    uvicorn.run(build_default_app(readiness=result, key=key, nonce_store=nonce_store),
                host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
