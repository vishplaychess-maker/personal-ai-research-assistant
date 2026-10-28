"""Standalone launcher for Thunder AI - the PyInstaller entry point.

Responsibilities:

* start uvicorn programmatically on a free port (default 8080, auto-increment);
* open the default browser once the server accepts connections;
* set local single-user env defaults;
* persist a stable JWT secret under the portable data dir (%APPDATA%/ThunderAI
  in a frozen build) so sessions survive restarts;
* keep stdout/stderr usable in a windowed (console=False) frozen build.

Run from source:
    python backend/launcher.py                  # http://127.0.0.1:8080
    python backend/launcher.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

DEFAULT_HOST = "127.0.0.1"
REMOTE_BIND_HOST = "0.0.0.0"
LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_PORT = 8080
PORT_SCAN_LIMIT = 100
READY_TIMEOUT_S = 30.0
JWT_SECRET_FILE = "jwt_secret.key"
ENCRYPTION_KEY_FILE = "encryption.key"
LOG_FILE = "thunder.log"

logger = logging.getLogger("thunder.launcher")


def _ensure_backend_on_path() -> None:
    """Allow "python backend/launcher.py" (and the frozen bundle) to import app."""
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parent
    entry = str(backend_dir)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _open_log_stream():
    """Append-mode log file under the portable data dir, or devnull."""
    try:
        from app.paths import get_data_dir

        path = get_data_dir() / LOG_FILE
        return open(path, "a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001 - logging setup must never crash the app
        try:
            return open(os.devnull, "w", encoding="utf-8")
        except Exception:  # noqa: BLE001
            return None


def _configure_stdio() -> None:
    """Make stdout/stderr usable and UTF-8 in a frozen windowed build.

    PyInstaller windowed builds (console=False) set sys.stdout/sys.stderr to
    None, which breaks every logging handler; and a non-UTF-8 Windows console
    code page raises UnicodeEncodeError on a non-ASCII line. Both must never
    crash the launcher, so missing streams are backed by a log file (or devnull)
    and both streams are reconfigured to UTF-8 with replacement.
    """
    _ensure_backend_on_path()
    log_stream = None
    if getattr(sys, "stdout", None) is None or getattr(sys, "stderr", None) is None:
        log_stream = _open_log_stream()
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            stream = log_stream
            if stream is None:
                try:
                    stream = open(os.devnull, "w", encoding="utf-8")
                except Exception:  # noqa: BLE001
                    continue
            setattr(sys, stream_name, stream)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - stream may not support reconfigure
            pass


def _generate_jwt_secret() -> str:
    """A fresh high-entropy secret (a valid Fernet key)."""
    from app.services.secret_store import generate_fernet_key

    return generate_fernet_key()


def load_or_create_jwt_secret() -> str:
    """Read the persisted JWT secret, creating it on first run. Never logged.

    Returns "" when the file cannot be read or written (fail-closed).
    """
    from app.services.secret_store import load_or_create_secret

    return load_or_create_secret(JWT_SECRET_FILE)


def load_or_create_encryption_key() -> str:
    """Read the persisted API-key encryption key, creating it on first run.

    Same portable data dir as the JWT secret; the value is never logged.
    """
    from app.services.secret_store import load_or_create_secret

    return load_or_create_secret(ENCRYPTION_KEY_FILE)


def ensure_jwt_secret() -> None:
    """Set JWT_SECRET from a persisted file unless the operator set one.

    Must run BEFORE app.config is imported (pydantic-settings reads the
    environment at import time). The secret value is never logged.
    """
    if (os.environ.get("JWT_SECRET") or "").strip():
        return
    secret = load_or_create_jwt_secret()
    if secret:
        os.environ["JWT_SECRET"] = secret
    else:
        logger.warning(
            "JWT_SECRET is unset and no secret file is available; using the",
        )
        logger.warning("insecure application default (acceptable for local dev only)")


def ensure_encryption_key() -> None:
    """Set ENCRYPTION_KEY from a persisted file unless the operator set one.

    Provider API keys are stored Fernet-encrypted; without this key the Add
    Provider save fails closed with HTTP 500. Must run BEFORE app.config is
    imported (pydantic-settings reads the environment at import time). The
    secret value is never logged.
    """
    if (os.environ.get("ENCRYPTION_KEY") or "").strip():
        return
    key = load_or_create_encryption_key()
    if key:
        os.environ["ENCRYPTION_KEY"] = key
    else:
        logger.warning(
            "ENCRYPTION_KEY is unset and no key file is available; provider "
        )
        logger.warning("API keys cannot be saved until one is configured")


def apply_local_env_defaults(
    port: int = DEFAULT_PORT,
    *,
    remote_access_enabled: bool = False,
    remote_https: bool = False,
) -> None:
    """Set env defaults for local single-user use without overriding the user.

    Only fills variables that are unset, so an operator always wins. Called
    BEFORE app.config is imported.

    Cookie matrix: remote access over HTTPS upgrades the refresh/CSRF cookies
    to Secure + SameSite=None; every other combination keeps the current
    local values (Secure=false, SameSite=lax).
    """
    os.environ.setdefault("PRODUCTION_MODE", "false")
    if remote_access_enabled and remote_https:
        os.environ.setdefault("COOKIE_SECURE", "true")
        os.environ.setdefault("COOKIE_SAMESITE", "none")
    else:
        os.environ.setdefault("COOKIE_SECURE", "false")
        os.environ.setdefault("COOKIE_SAMESITE", "lax")
    os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:" + str(port))


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var without raising on a malformed value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_bind_host(cli_host: str | None, remote_access_enabled: bool) -> str:
    """Pick the bind interface: explicit CLI > remote opt-in > loopback.

    An explicit ``--host`` always wins. With remote access enabled the default
    becomes 0.0.0.0; otherwise the app stays loopback-only.
    """
    if cli_host:
        return cli_host
    return REMOTE_BIND_HOST if remote_access_enabled else DEFAULT_HOST


def _lan_ipv4_addresses() -> list[str]:
    """Best-effort private IPv4 addresses of this machine (never raises).

    Uses the hostname resolver plus a UDP-connect probe (which sends no
    packets) and filters to private, non-loopback, non-link-local addresses.
    """
    candidates: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.add(info[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            candidates.add(probe.getsockname()[0])
    except OSError:
        pass

    result: list[str] = []
    for raw in candidates:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if addr.version != 4 or not addr.is_private:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            continue
        result.append(raw)
    return sorted(set(result))


def format_remote_access_notice(port: int, lan_ips: list[str]) -> list[str]:
    """ASCII-only banner shown when remote access is enabled."""
    lines = [
        "=" * 64,
        "REMOTE ACCESS ENABLED - every API request requires login.",
        "Local URL: http://localhost:%d" % port,
    ]
    if lan_ips:
        lines.append("LAN URLs:")
        lines.extend("  http://%s:%d" % (ip, port) for ip in lan_ips)
    else:
        lines.append(
            "LAN URL: no private IPv4 found; use this machine's network address."
        )
    lines.append("Anyone who can reach this port can attempt a login.")
    lines.append("=" * 64)
    return lines


def find_free_port(
    host: str = DEFAULT_HOST,
    start: int = DEFAULT_PORT,
    limit: int = PORT_SCAN_LIMIT,
) -> int:
    """Return the first bindable port in [start, start + limit).

    Raises RuntimeError when the whole range is busy (bounded, actionable).
    """
    if limit < 1:
        raise RuntimeError("port scan limit must be >= 1")
    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # No SO_REUSEADDR: on Windows it would let the probe bind a port that
            # another process is already listening on, defeating the scan.
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        "no free port in %d..%d (all busy)" % (start, start + limit - 1)
    )


def _wait_and_open_browser(
    url: str,
    host: str,
    port: int,
    timeout: float = READY_TIMEOUT_S,
) -> None:
    """Poll until the server accepts connections, then open the browser."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((host, port))
            except OSError:
                time.sleep(0.2)
                continue
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - browser is best-effort
            logger.warning("could not open browser (%s)", type(exc).__name__)
        return
    logger.warning("server not ready within %.0fs; browser not opened", timeout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="thunder-ai", description="Thunder AI launcher")
    parser.add_argument(
        "--host",
        default=None,
        help="bind host (default 127.0.0.1; 0.0.0.0 with --remote-access)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="preferred port (default 8080)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--remote-access",
        action="store_true",
        help="bind 0.0.0.0 so other devices can connect (login required)",
    )
    parser.add_argument(
        "--remote-https",
        action="store_true",
        help="remote access terminates TLS (Secure + SameSite=None cookies)",
    )
    return parser


def _probe_host(bind_host: str) -> str:
    """Address the browser/readiness probe should use for a wildcard bind."""
    return "127.0.0.1" if bind_host in ("0.0.0.0", "::", "[::]") else bind_host


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _ensure_backend_on_path()
    _configure_stdio()

    if args.remote_access:
        os.environ["REMOTE_ACCESS_ENABLED"] = "true"
    os.environ.setdefault("REMOTE_ACCESS_ENABLED", "false")
    if args.remote_https:
        os.environ["REMOTE_HTTPS"] = "true"
    os.environ.setdefault("REMOTE_HTTPS", "false")

    remote_enabled = _env_bool("REMOTE_ACCESS_ENABLED")
    remote_https = _env_bool("REMOTE_HTTPS")
    if remote_https and not remote_enabled:
        print(
            "[thunder] --remote-https requires --remote-access "
            "(or REMOTE_ACCESS_ENABLED=true)",
            file=sys.stderr,
        )
        return 1

    requested_host = args.host or (os.environ.get("BIND_HOST") or "").strip() or None
    bind_host = resolve_bind_host(requested_host, remote_enabled)
    if bind_host.strip().lower() not in LOOPBACK_BIND_HOSTS and not remote_enabled:
        print(
            "[thunder] binding a non-loopback address requires --remote-access "
            "(login is then required for remote callers)",
            file=sys.stderr,
        )
        return 1
    os.environ["BIND_HOST"] = bind_host

    apply_local_env_defaults(
        args.port,
        remote_access_enabled=remote_enabled,
        remote_https=remote_https,
    )
    ensure_jwt_secret()
    ensure_encryption_key()

    try:
        port = find_free_port(bind_host, args.port)
    except RuntimeError as exc:
        print("[thunder] " + str(exc), file=sys.stderr)
        return 1

    url = "http://%s:%d" % (_probe_host(bind_host), port)

    logging.basicConfig(level=logging.INFO)
    if remote_enabled:
        for line in format_remote_access_notice(port, _lan_ipv4_addresses()):
            print("[thunder] " + line)

    if not args.no_browser:
        threading.Thread(
            target=_wait_and_open_browser,
            args=(url, _probe_host(bind_host), port),
            name="thunder-browser",
            daemon=True,
        ).start()
    print("[thunder] Thunder AI starting at " + url)

    import uvicorn

    from app.main import app

    uvicorn.run(app, host=bind_host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
