"""MP-4C — DEDICATED executor service: real-container end-to-end tests.

These launch REAL Docker containers against the digest-pinned Python image. They
prove exactly one execution, sanitised failure, stdin-only delivery, absence of
code from command metadata and service logs, bounded timeout/OOM/interruption
handling with cleanup, ownership-scoped restart recovery, and that the REAL
startup readiness gate passes (daemon + pinned image + the full 31-check probe).

Every container this file creates is removed in a finally block.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from executor_service import contract, image as image_mod, runner as runner_mod
from executor_service.app import create_app
from executor_service.nonce_store import NonceStore
from executor_service.readiness import check_readiness
from executor_service.runner import ContainerRunner, RunOutcome, sweep_owned_orphans
from executor_service.service import ExecutionService
from mp4c_contract import grant as g

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = "mp4c-executor-e2e"
KEY = b"e2e-grant-key-0123456789"
OTHER_OWNER = "mp4c-executor-someone-else"

INSPECT_FMT = ("{{json .Config.Cmd}}||{{json .Path}}||{{json .Args}}"
               "||{{.Config.OpenStdin}}")


def _docker(args, *, timeout=120):
    p = subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _exists(name: str) -> bool:
    _rc, out, _e = _docker(["ps", "-a", "--filter", "name=^%s$" % name,
                            "--format", "{{.Names}}"])
    return bool(out.strip())


def _grant_claims(code, **over):
    """The bound claims for EXACTLY this code, for one legitimate user."""
    now = int(time.time())
    sha, ln = g.code_sha256(code)
    raw = {"v": g.GRANT_VERSION, "jti": uuid.uuid4().hex, "iat": now,
           "exp": now + 120, "user_id": 4242, "tenant_id": None,
           "run_id": "e2e-run", "step_id": "e2e-step", "tool_call_id": "e2e-tc",
           "approval_id": None, "action_hash": "a" * 64,
           "code_sha256": sha, "code_len": ln}
    raw.update(over)
    return g.GrantClaims.from_claims(raw)


def _grant(code, **over):
    return g.issue(_grant_claims(code, **over), KEY)


def _run(svc, code, run_id):
    """Reserve the bound action (pre-dispatch), then execute - the real order."""
    svc.reserve(grant=g.issue(_grant_claims(code), KEY,
                              purpose=g.PURPOSE_RESERVE))
    return svc.execute(grant=_grant(code), code=code, run_id=run_id)


def _service(tmp_path, runner=None, **kw):
    return ExecutionService(key=KEY,
                            nonce_store=NonceStore(str(tmp_path / "n.db")),
                            runner=runner or ContainerRunner(),
                            preflight=kw.pop("preflight", None)
                            or (lambda: (True, None)), **kw)


@pytest.fixture(autouse=True)
def _no_stray_containers():
    yield
    names = _owned_names()
    if names:
        _docker(["rm", "-f"] + names)


def _owned_names():
    _rc, out, _e = _docker(["ps", "-a", "--filter",
                            "label=%s=%s" % (contract.OWNER_LABEL, OWNER),
                            "--format", "{{.Names}}"])
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


# ── one real request end to end ─────────────────────────────────────


def test_real_request_executes_once_and_source_never_reaches_metadata_or_logs(caplog,
                                                                              tmp_path):
    marker = "MP4C_E2E_MARKER_" + uuid.uuid4().hex
    plans = []

    class Recorder(ContainerRunner):
        def run(self, plan):
            plans.append(plan)
            return super().run(plan)

    svc = _service(tmp_path, Recorder())
    code = "print('%s')" % marker
    grant = _grant(code)
    svc.reserve(grant=g.issue(_grant_claims(code), KEY,
                              purpose=g.PURPOSE_RESERVE))
    with caplog.at_level(logging.INFO, logger="executor_service"):
        out = svc.execute(grant=grant, code=code, run_id="e2e-1")

    assert out.ok is True and out.status == "ok", out
    assert out.output.count(marker) == 1, out.output          # exactly one execution
    assert out.output.strip() == marker

    plan = plans[0]
    argv_join = " ".join(plan.argv)
    assert marker not in argv_join and "print(" not in argv_join
    assert "-c" not in plan.argv
    assert list(plan.argv[-5:]) == ["python", "-I", "-B", "-u", "-"]
    assert plan.stdin_code.decode("utf-8") == "print('%s')" % marker

    log_text = caplog.text
    assert "executor_request" in log_text
    assert marker not in log_text and grant not in log_text
    assert not _exists(plan.name), "the run container must not survive"
    assert svc.broker.active_total() == 0


def test_real_request_command_metadata_is_clean_in_a_live_container():
    """Identical argv, held open, inspected while running."""
    marker = "MP4C_E2E_INSPECT_" + uuid.uuid4().hex
    name = contract.CONTAINER_PREFIX + uuid.uuid4().hex[:10]
    src = "import time" + chr(10) + "time.sleep(30)" + chr(10) + "# " + marker
    plan = contract.build_plan(src, name=name, owner=OWNER)
    argv = [a for a in plan.argv if a != "--rm"]
    insp_name = name + "i"
    argv[argv.index("--name") + 1] = insp_name
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.stdin.write(plan.stdin_code)
        proc.stdin.flush()          # stdin stays open: the container stays alive
        time.sleep(2.5)
        rc, out, err = _docker(["inspect", insp_name, "--format", INSPECT_FMT])
        parts = out.strip().split("||") if out.strip() else []
        assert rc == 0, err
        assert marker not in out
        assert parts[0] == '["python","-I","-B","-u","-"]', parts
        assert parts[1] == '"python"'
        assert parts[2] == '["-I","-B","-u","-"]'
        assert parts[3].strip() == "true"          # stdin really is forwarded
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
        _docker(["rm", "-f", insp_name])
    assert not _exists(insp_name)


# ── bounded failure behaviour ───────────────────────────────────────


def test_real_sanitised_failure_returns_no_child_output(tmp_path):
    canary = "MP4C_E2E_ERR_" + uuid.uuid4().hex
    svc = _service(tmp_path)
    code = "raise ValueError('%s')" % canary
    out = _run(svc, code, "e2e-fail")
    assert out.ok is False and out.status == runner_mod.STATUS_FAILED
    assert out.output == "", "child output must never be returned on failure"
    assert canary not in out.output and "ValueError" not in out.output
    assert svc.broker.active_total() == 0
    assert _owned_names() == []


class _Recorder:
    """Records the plan it hands to a real runner."""

    def __init__(self, inner):
        self.inner = inner
        self.plans = []

    def run(self, plan):
        self.plans.append(plan)
        return self.inner.run(plan)


def test_real_timeout_is_bounded_and_cleans_up_the_container(tmp_path):
    rec = _Recorder(ContainerRunner(timeout_s=6))
    svc = _service(tmp_path, rec)
    started = time.monotonic()
    code = "import time; time.sleep(60)"
    out = _run(svc, code, "e2e-to")
    elapsed = time.monotonic() - started
    assert out.ok is False and out.status == runner_mod.STATUS_TIMEOUT
    assert out.output == ""
    assert elapsed < 45, elapsed
    assert not _exists(rec.plans[0].name), "a timed-out container must be removed"
    assert svc.broker.active_total() == 0


def test_real_oom_is_classified_and_cleans_up_the_container(tmp_path):
    rec = _Recorder(ContainerRunner())
    svc = _service(tmp_path, rec)
    code = "a=[]\nwhile True:\n    a.append(bytearray(10**7))"
    out = _run(svc, code, "e2e-oom")
    assert out.ok is False
    assert out.status in (runner_mod.STATUS_OOM_KILLED, runner_mod.STATUS_FAILED), out.status
    assert out.output == ""
    assert not _exists(rec.plans[0].name)
    assert svc.broker.active_total() == 0


class _BoomPopen:
    """Starts the REAL container, then simulates a broker interruption."""

    def __init__(self, argv, **kw):
        self.argv = list(argv)
        self.name = self.argv[self.argv.index("--name") + 1]
        self._proc = subprocess.Popen(self.argv, **kw)

    def communicate(self, input=None, timeout=None):
        try:
            self._proc.stdin.write(input or b"")
            self._proc.stdin.flush()
        except Exception:
            pass
        for _ in range(60):
            if _exists(self.name):
                break
            time.sleep(0.25)
        raise RuntimeError("simulated broker interruption")

    def kill(self):
        try:
            self._proc.kill()
        except Exception:
            pass


def test_interruption_removes_the_container_and_releases_the_slot(tmp_path):
    name_holder = {}

    class Capturing(ContainerRunner):
        def run(self, plan):
            name_holder["name"] = plan.name
            return super().run(plan)

    svc = _service(tmp_path, Capturing(popen=_BoomPopen))
    code = "import time; time.sleep(30)"
    out = _run(svc, code, "e2e-int")
    # A raised runner means the container may or may not have started, so the
    # outcome is UNKNOWN and must never be retried.
    assert out.ok is False and out.status == runner_mod.STATUS_OUTCOME_UNKNOWN
    assert out.output == ""
    assert not _exists(name_holder["name"]), "an interrupted run must be cleaned up"
    assert svc.broker.active_total() == 0, "the slot must be released"


# ── restart recovery: owned orphans only ────────────────────────────


def test_restart_removes_owned_orphans_and_spares_unrelated_containers():
    orphan = "mp4c_exec_orphan_" + uuid.uuid4().hex[:8]
    other = "mp4c_exec_other_" + uuid.uuid4().hex[:8]
    plain = "mp4c_exec_plain_" + uuid.uuid4().hex[:8]
    base_argv = [image_mod.PINNED_REF] + list(contract.PYTHON_ARGV)
    try:
        rc, _o, err = _docker(["create", "--name", orphan,
                               "--label", "%s=%s" % (contract.OWNER_LABEL, OWNER)]
                              + base_argv)
        assert rc == 0, err
        rc, _o, err = _docker(["create", "--name", other,
                               "--label", "%s=%s" % (contract.OWNER_LABEL, OTHER_OWNER)]
                              + base_argv)
        assert rc == 0, err
        rc, _o, err = _docker(["create", "--name", plain] + base_argv)
        assert rc == 0, err

        removed = sweep_owned_orphans(OWNER)
        assert removed == [orphan], removed
        assert not _exists(orphan)
        assert _exists(other), "another owner's container must be spared"
        assert _exists(plain), "an unrelated container must be spared"
    finally:
        for n in (orphan, other, plain):
            _docker(["rm", "-f", n])


def test_sweep_is_a_no_op_when_nothing_is_owned():
    assert sweep_owned_orphans("mp4c-executor-nobody-" + uuid.uuid4().hex[:8]) == []


# ── the REAL startup readiness gate ─────────────────────────────────


@pytest.fixture(scope="module")
def probe_run():
    """The FULL probe, executed ONCE for this module (it takes minutes).

    Sharing one real run avoids evaluating the same multi-minute probe twice
    inside a single test session.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(REPO_ROOT, "backend"), REPO_ROOT]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    p = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "tests", "smoke",
                                      "mp4c_isolation_probe.py")],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=1500)
    return p.returncode, p.stdout, p.stderr


def _probe_payload(out):
    for line in out.splitlines():
        if line.startswith("PROBE_RESULT "):
            return json.loads(line[len("PROBE_RESULT "):])
    return {}


def test_real_probe_pairs_the_network_control_on_both_sides(probe_run):
    """Both sides of the network control, from ONE real probe run:
    a networked control REACHES the listener at a target, and the otherwise
    equivalent --network none workload dials the SAME target, completes the
    attempt, and produces ZERO listener hits."""
    rc, out, err = probe_run
    assert rc == 0, (out[-1500:], err[-500:])
    ctrl = [ln for ln in out.splitlines()
            if ln.startswith("PASS network_tripwire_positive_control_reachable")]
    work = [ln for ln in out.splitlines()
            if ln.startswith("PASS network_tripwire_synchronised_and_blocked")]
    assert ctrl, out[-1500:]
    assert work, out[-1500:]
    c = json.loads(ctrl[0].split(":: ", 1)[1])
    w = json.loads(work[0].split(":: ", 1)[1])
    assert c["reached"] is True and c["control_hits"] >= 1, c
    assert w["attempt"] == "done" and w["listener_hits"] == 0, w
    assert c["target"] == w["target"], (c, w)


def test_real_startup_readiness_passes_with_the_pinned_image_and_full_probe(probe_run):
    """Real daemon + real pinned image, plus the module's single real probe run."""
    _rc, out, _err = probe_run
    payload = _probe_payload(out)
    assert payload.get("ok") is True, payload
    result = check_readiness(
        repo_root=REPO_ROOT,
        probe_runner=lambda: (True, {"probe": "ok",
                                     "checks": payload.get("checks"), "failed": [],
                                     "containerized": payload.get("containerized")}))
    assert result.ready is True, (result.reasons, result.detail)
    assert result.detail["daemon"] == "ok"
    assert result.detail["image"] == "ok"
    assert result.detail["probe"]["probe"] == "ok"
    assert result.reasons == ()


def test_real_service_request_through_the_http_surface(tmp_path):
    marker = "MP4C_E2E_HTTP_" + uuid.uuid4().hex
    svc = _service(tmp_path)
    client = TestClient(create_app(service=svc,
                                   availability_fn=lambda: (True, None)))
    code = "print('%s')" % marker
    reserve = client.post("/v1/reserve",
                          json={"grant": g.issue(_grant_claims(code), KEY,
                                                 purpose=g.PURPOSE_RESERVE)})
    assert reserve.status_code == 200, reserve.text
    res = client.post("/v1/execute",
                      json={"grant": _grant(code), "code": code,
                            "run_id": "e2e-http"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["status"] == "ok"
    assert body["output"].count(marker) == 1
    assert marker not in json.dumps({"run_id": "e2e-http"})
    assert _owned_names() == []
