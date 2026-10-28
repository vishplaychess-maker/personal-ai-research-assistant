"""Focused tests for the D6B smoke orchestration helpers and network tripwire.

Run:  PYTHONPATH=backend python -m pytest tests/smoke/test_helpers.py -q
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest


def test_free_loopback_port_is_distinct_bindable_and_loopback():
    from tests.smoke.helpers import free_loopback_port

    a = free_loopback_port()
    b = free_loopback_port()
    assert a != b
    for port in (a, b):
        assert 1024 <= port <= 65535
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))


def test_wait_for_returns_once_the_predicate_holds():
    from tests.smoke.helpers import wait_for

    calls = {"n": 0}

    def _predicate():
        calls["n"] += 1
        return calls["n"] >= 3

    wait_for(_predicate, timeout_s=5.0, interval_s=0.01, description="third try")
    assert calls["n"] == 3, "must stop polling as soon as the condition holds"


def test_wait_for_raises_a_bounded_timeout_without_busy_spinning():
    from tests.smoke.helpers import SmokeTimeout, wait_for

    calls = {"n": 0}

    def _never():
        calls["n"] += 1
        return False

    start = time.monotonic()
    with pytest.raises(SmokeTimeout) as exc:
        wait_for(_never, timeout_s=0.3, interval_s=0.05, description="never true")
    elapsed = time.monotonic() - start
    assert "never true" in str(exc.value)
    assert elapsed >= 0.3
    assert calls["n"] < 50, "must not busy-spin"


def test_assert_loopback_url_accepts_only_bare_loopback_origins():
    from tests.smoke.helpers import SmokeError, assert_loopback_url

    assert_loopback_url("http://127.0.0.1:8080")
    assert_loopback_url("http://localhost:5173/api")
    assert_loopback_url("http://[::1]:9000")

    for bad in (
        "http://example.com:80",
        "https://10.0.0.5:443",
        "http://user:pw@127.0.0.1:8080",
        "http://127.0.0.1:8080/?q=1",
        "http://127.0.0.1:8080/#frag",
        "file:///etc/passwd",
        "ftp://127.0.0.1:21",
        "not-a-url",
    ):
        with pytest.raises(SmokeError):
            assert_loopback_url(bad)


def test_atomic_json_round_trip_and_no_partial_file_left_behind(tmp_path):
    from tests.smoke.helpers import atomic_write_json, read_json

    target = tmp_path / "nested" / "manifest.json"
    atomic_write_json(target, {"run_id": "abc", "counts": {"runs": 1}})
    assert read_json(target) == {"run_id": "abc", "counts": {"runs": 1}}
    assert json.loads(target.read_text(encoding="utf-8"))["run_id"] == "abc"
    leftovers = [q.name for q in target.parent.iterdir() if q.name != "manifest.json"]
    assert leftovers == [], "atomic write must not leave a temp sibling"


def test_terminate_process_tree_stops_a_live_child():
    from tests.smoke.helpers import terminate_process_tree

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    assert proc.poll() is None
    terminate_process_tree(proc, grace_s=10.0)
    assert proc.poll() is not None, "child must be dead after terminate_process_tree"


def test_terminate_process_tree_is_safe_on_an_already_dead_process():
    from tests.smoke.helpers import terminate_process_tree

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    terminate_process_tree(proc, grace_s=1.0)      # must not raise
    assert proc.poll() is not None


def test_tripwire_allows_loopback_and_blocks_everything_else():
    from tests.smoke.fakes import NetworkTripwire

    trip = NetworkTripwire(blocked_ports={11434, 8000})
    assert trip._evaluate(("127.0.0.1", 8080)) is None
    assert trip._evaluate(("::1", 5173)) is None
    assert trip._evaluate(("127.0.0.1", 11434)) == "blocked_local_service_port"
    assert trip._evaluate(("127.0.0.1", 8000)) == "blocked_local_service_port"
    assert trip._evaluate(("93.184.216.34", 443)) == "non_loopback"
    assert trip._evaluate(("api.openrouter.ai", 443)) == "non_loopback"
    assert trip._evaluate("/tmp/some.sock") is None      # AF_UNIX passthrough


def test_tripwire_install_blocks_a_real_outbound_attempt_then_uninstalls(tmp_path):
    from tests.smoke.fakes import NetworkTripwire

    real_connect = socket.socket.connect
    log = tmp_path / "tripwire.jsonl"
    trip = NetworkTripwire(log_path=log, blocked_ports={11434}).install()
    try:
        assert socket.socket.connect is not real_connect
        with pytest.raises(OSError):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("93.184.216.34", 80))     # TEST-NET address, never routed
        with pytest.raises(OSError):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", 11434))       # blocked local service port
    finally:
        trip.uninstall()

    assert socket.socket.connect is real_connect
    reasons = [e["reason"] for e in trip.attempts]
    assert reasons == ["non_loopback", "blocked_local_service_port"]
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [x["reason"] for x in lines] == reasons
    assert all("address" in x for x in lines)


def _pid_alive(pid: int) -> bool:
    """Windows-only liveness probe via tasklist."""
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                         capture_output=True, text=True)
    return str(pid) in (out.stdout or "")


@pytest.mark.skipif(os.name != "nt", reason="tree kill is a Windows behaviour")
def test_terminate_process_tree_kills_descendants_not_just_the_parent(tmp_path):
    """Regression: `terminate()` on an `npm.cmd` shim kills only the cmd wrapper
    and ORPHANS its node/vite children, which keep their log handles open. The
    helper must tree-kill. This actually reproduced during D6B: four orphaned
    `npm run dev` / `vite.js` processes survived two smoke runs."""
    from tests.smoke.helpers import terminate_process_tree

    pidfile = tmp_path / "grandchild.pid"
    grandchild_code = (
        "import os, pathlib, time;"
        "pathlib.Path(r'%s').write_text(str(os.getpid()));"
        "time.sleep(120)"
    ) % pidfile
    parent_code = (
        "import subprocess, sys, time;"
        "subprocess.Popen([sys.executable, '-c', %r]);"
        "time.sleep(120)"
    ) % grandchild_code

    proc = subprocess.Popen([sys.executable, "-c", parent_code])
    deadline = time.monotonic() + 30
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pidfile.exists(), "the grandchild never started"
    grandchild_pid = int(pidfile.read_text(encoding="utf-8").strip())
    assert _pid_alive(grandchild_pid)

    terminate_process_tree(proc, grace_s=20.0)

    deadline = time.monotonic() + 20
    while _pid_alive(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(grandchild_pid), (
        "a descendant process survived terminate_process_tree"
    )
    assert proc.poll() is not None


def test_tripwire_records_nothing_when_only_loopback_is_used(tmp_path):
    from tests.smoke.fakes import NetworkTripwire

    trip = NetworkTripwire(log_path=tmp_path / "t.log", blocked_ports={11434}).install()
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
            c.connect(("127.0.0.1", port))      # allowed
        srv.close()
    finally:
        trip.uninstall()

    assert trip.attempts == []
    assert not (tmp_path / "t.log").exists()
