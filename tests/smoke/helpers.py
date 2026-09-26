"""Bounded, testable orchestration helpers for the MP-3C3D6B smoke.

TEST-ONLY. No `app/**` module imports this file.

Everything here is deliberately deterministic: bounded polling with explicit
deadlines instead of fixed sleeps, strict loopback-only URL handling, atomic
manifest writes, and a process-tree terminator that always runs in a finally.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: Ports already handed out by this process — the OS may otherwise re-offer the
#: same ephemeral port immediately after the probe socket closes.
_ALLOCATED_PORTS: set[int] = set()


class SmokeError(RuntimeError):
    """A D6B smoke precondition or invariant that is not a timeout."""


class SmokeTimeout(SmokeError):
    """A bounded wait whose deadline expired."""


def free_loopback_port() -> int:
    """Return a currently-bindable loopback TCP port, never repeating one already
    handed out in this process."""
    for _ in range(64):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        if port not in _ALLOCATED_PORTS:
            _ALLOCATED_PORTS.add(port)
            return port
    raise SmokeError("could not allocate a distinct free loopback port")


def wait_for(predicate: Callable[[], bool], *, timeout_s: float, interval_s: float,
             description: str) -> None:
    """Poll `predicate` until it is truthy or `timeout_s` elapses. Never sleeps a
    fixed amount as proof of readiness, and never busy-spins."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(0.01, float(interval_s))
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise SmokeTimeout("timed out after %.1fs waiting for: %s"
                               % (float(timeout_s), description))
        time.sleep(interval)


def assert_loopback_url(url: str) -> str:
    """Accept only a bare loopback http/https origin (optionally with a path).
    Rejects credentials, query strings, fragments, non-http(s) schemes and any
    non-loopback host."""
    if not isinstance(url, str) or not url:
        raise SmokeError("URL must be a non-empty string")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SmokeError("only http/https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise SmokeError("credentials are not allowed in a smoke URL")
    if parts.query or parts.fragment:
        raise SmokeError("query strings and fragments are not allowed")
    host = parts.hostname
    if host is None or not (host in _LOOPBACK_HOSTS or host.startswith("127.")):
        raise SmokeError("only loopback hosts are allowed")
    return url


def atomic_write_json(path: Path, payload: Any) -> Path:
    """Write JSON durably: same-directory temp file, then an atomic replace, so a
    reader never observes a partial manifest."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".partial")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def terminate_process_tree(proc: Optional[subprocess.Popen], *, grace_s: float = 15.0) -> None:
    """Stop `proc` and its children. Graceful terminate first, bounded wait, then a
    forced tree kill. Safe on an already-dead process and on None."""
    if proc is None:
        return
    if os.name == "nt":
        # Windows: terminate the WHOLE tree FIRST. `terminate()` on an `npm.cmd`
        # shim kills only the cmd.exe wrapper and orphans its node/vite children,
        # which then keep the log file handles open. taskkill /T is the only
        # reliable tree kill, so it runs before any graceful attempt.
        killed = False
        if proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=30, check=False)
                killed = True
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=max(1.0, float(grace_s)))
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is not None:
            return
        if killed:
            pass

    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass

    deadline = time.monotonic() + max(0.0, float(grace_s))
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is not None:
        return

    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=20)
    except Exception:  # noqa: BLE001
        pass
