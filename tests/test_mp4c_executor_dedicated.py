"""MP-4C - executor service: hermetic contract, grant, quota, readiness tests.

TEST-ONLY. NOTHING here launches a container or pulls an image. The identity
contract is a SIGNED GRANT: identity is inside the signature, so it cannot be
chosen by a caller, and the static token map is prototype-only (asserted below).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from executor_service import readiness as readiness_mod
from executor_service import image as image_mod
from executor_service import contract, runner as runner_mod
from executor_service.app import ExecutionBody, create_app
from executor_service.identity import IdentityRegistry, registry_from_env
from executor_service.nonce_store import NonceStore
from executor_service.quota import QuotaBroker
from executor_service.runner import RunOutcome
from executor_service.service import (STATUS_BUSY, STATUS_NONCE_UNAVAILABLE,
                                      STATUS_NOT_READY, STATUS_REPLAY,
                                      ExecutionService)
from mp4c_contract import grant as g

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KEY = b"dedicated-grant-key-0123456789"
CODE = "print(1)"
CANARY = "MP4C_DED_CANARY_" + "d" * 12


def _claims(**over):
    now = int(time.time())
    sha, ln = g.code_sha256(CODE)
    raw = {
        "v": g.GRANT_VERSION, "jti": "jti-" + os.urandom(8).hex(), "iat": now,
        "exp": now + 120, "user_id": 7, "tenant_id": None, "run_id": "run-1",
        "step_id": "step-1", "tool_call_id": "tc-1", "approval_id": "appr-1",
        "action_hash": "a" * 64, "code_sha256": sha, "code_len": ln,
    }
    raw.update(over)
    return g.GrantClaims.from_claims(raw)


def _grant(**over):
    return g.issue(_claims(**over), KEY)


class _RecordingRunner:
    def __init__(self, outcome=None):
        self.plans = []
        self.outcome = outcome or RunOutcome(status=runner_mod.STATUS_OK, output="OK\n")

    def run(self, plan):
        self.plans.append(plan)
        return self.outcome


def _reserve(svc, **over):
    return svc.reserve(grant=g.issue(_claims(**over), KEY,
                                     purpose=g.PURPOSE_RESERVE))


def _exec(svc, *, code=CODE, run_id="r1", **over):
    """Reserve the bound action, then execute - the post-MP-4C backend order."""
    _reserve(svc, **over)
    return svc.execute(grant=_grant(**over), code=code, run_id=run_id)


def _service(tmp_path, **kw):
    rec = kw.pop("runner", None) or _RecordingRunner()
    svc = ExecutionService(
        key=kw.pop("key", KEY),
        nonce_store=kw.pop("nonce_store",
                           None) or NonceStore(str(tmp_path / "n.db")),
        runner=rec,
        preflight=kw.pop("preflight", None) or (lambda: (True, None)),
        ready=kw.pop("ready", None) or (lambda: True), **kw)
    return svc, rec


# -- contract --------------------------------------------------------


def test_plan_argv_is_fixed_and_carries_no_source_or_docker_options():
    code = "x = 1  # " + CANARY + "\nprint(x)\n"
    plan = contract.build_plan(code, name="mp4c_exec_abc123", owner="mp4c-executor")
    joined = " ".join(plan.argv)
    assert CANARY not in joined and "x = 1" not in joined
    assert "-c" not in plan.argv
    assert list(plan.argv[-5:]) == ["python", "-I", "-B", "-u", "-"]
    assert contract.DOCKER_STDIN_FLAG in plan.argv
    assert plan.stdin_code == code.encode("utf-8")
    assert plan.argv[plan.argv.index("--network") + 1] == "none"
    assert "--read-only" in plan.argv
    assert plan.argv[plan.argv.index("--cap-drop") + 1] == "ALL"
    assert image_mod.PINNED_REF in plan.argv
    for forbidden in ("-v", "--volume", "--mount", "--privileged", "-e", "--env",
                      "sh", "bash", "docker.sock"):
        assert forbidden not in plan.argv, forbidden


def test_repr_never_reveals_the_code():
    plan = contract.build_plan("secret = 1  # " + CANARY, name="n", owner="o")
    assert CANARY not in repr(plan) and "withheld" in repr(plan)


def test_request_body_has_no_identity_or_docker_option_field():
    assert set(ExecutionBody.model_fields) == {"grant", "code", "run_id"}


def test_extra_body_fields_are_rejected(tmp_path):
    svc, _ = _service(tmp_path)
    client = TestClient(create_app(service=svc))
    res = client.post("/v1/execute", json={"grant": _grant(), "code": CODE,
                                           "run_id": "r1", "command": "sh -c id"})
    assert res.status_code == 422


def test_identity_fields_in_the_body_are_rejected(tmp_path):
    svc, _ = _service(tmp_path)
    client = TestClient(create_app(service=svc))
    for extra in ({"user_id": 8}, {"tenant_id": "t-9"}, {"image": "alpine"},
                  {"argv": ["sh"]}, {"mounts": ["/x:/y"]}, {"network": "host"},
                  {"memory": "9g"}, {"env": {"A": "1"}}):
        body = {"grant": _grant(), "code": CODE, "run_id": "r1", **extra}
        res = client.post("/v1/execute", json=body)
        assert res.status_code == 422, (extra, res.status_code)


def test_validation_error_never_echoes_the_callers_input(tmp_path):
    svc, _ = _service(tmp_path)
    client = TestClient(create_app(service=svc))
    res = client.post("/v1/execute",
                      json={"grant": _grant(), "code": CODE, "run_id": "r1",
                            "image": CANARY})
    assert res.status_code == 422 and CANARY not in res.text


# -- the static token map is prototype-only ---------------------------


def test_registry_from_env_fails_closed():
    assert len(registry_from_env({})) == 0
    assert len(registry_from_env({"EXECUTOR_SERVICE_TOKENS": ""})) == 0
    assert len(registry_from_env({"EXECUTOR_SERVICE_TOKENS": "not json"})) == 0
    assert registry_from_env({}).resolve("anything") is None


def test_the_service_no_longer_accepts_a_static_registry():
    import inspect
    params = inspect.signature(ExecutionService.__init__).parameters
    assert "registry" not in params, "the static map must not be a fallback"
    assert "key" in params and "nonce_store" in params


def test_authentication_uses_the_signed_grant(tmp_path):
    svc, rec = _service(tmp_path)
    out = _exec(svc)
    assert out.ok is True and len(rec.plans) == 1
    claims = g.parse_and_verify(_grant(user_id=8), KEY, code=CODE)
    assert claims.user_id == 8


def test_missing_or_forged_grants_are_refused(tmp_path):
    svc, rec = _service(tmp_path)
    for bad in (None, "", "  ", "not-a-grant"):
        assert svc.execute(grant=bad, code=CODE, run_id="r1").status == g.MALFORMED
    forged = g.issue(_claims(), b"some-other-key-0123456789")
    assert svc.execute(grant=forged, code=CODE, run_id="r1").status == g.BAD_SIGNATURE
    assert rec.plans == []


def test_altered_code_is_refused_before_quota_is_taken(tmp_path):
    svc, rec = _service(tmp_path)
    other = "print('other')"
    sha, ln = g.code_sha256(other)
    out = svc.execute(grant=_grant(code_sha256=sha, code_len=ln),
                      code=CODE, run_id="r1")
    assert out.status == g.CODE_BINDING_MISMATCH
    assert rec.plans == [] and svc.broker.active_total() == 0


def test_rejections_before_quota_do_not_consume_a_slot(tmp_path):
    svc, _ = _service(tmp_path)
    svc.execute(grant=None, code=CODE, run_id="r1")
    svc.execute(grant=_grant(), code="", run_id="r1")
    svc.execute(grant=_grant(), code=CODE, run_id="bad run id!")
    assert svc.broker.active_total() == 0


def test_code_bounds_are_utf8_bytes_and_reject_before_starting(tmp_path):
    svc, rec = _service(tmp_path)
    big = "x = 1  # " + "y" * 9000
    sha, ln = g.code_sha256(big)
    out = svc.execute(grant=_grant(code_sha256=sha, code_len=ln), code=big,
                      run_id="r1")
    assert out.status == "executor_code_rejected"
    assert rec.plans == []


def test_logs_never_contain_the_grant_code_or_raw_output(tmp_path, caplog):
    svc, _ = _service(tmp_path)
    token = _grant()
    raw_child = "TRACEBACK-RAW-CHILD-OUTPUT"
    svc.runner = _RecordingRunner(RunOutcome(status=runner_mod.STATUS_FAILED,
                                             output=""))
    with caplog.at_level(logging.INFO, logger="executor_service"):
        svc.execute(grant=token, code="print(1)  # " + CANARY, run_id="r1")
        svc.execute(grant="bad-grant-" + CANARY, code=CODE, run_id="r2")
    text = caplog.text
    assert "executor_request" in text
    assert token not in text and CANARY not in text and raw_child not in text
    assert "print(1)" not in text


def test_the_service_does_not_hold_a_static_token_map():
    src = open(os.path.join(REPO_ROOT, "executor_service", "service.py"),
               encoding="utf-8").read()
    for forbidden in ("IdentityRegistry", "registry_from_env", "load_registry",
                      "EXECUTOR_SERVICE_TOKENS"):
        assert forbidden not in src, forbidden


# -- quota under contention ------------------------------------------


def test_one_active_execution_per_user_under_contention():
    broker = QuotaBroker()
    wins = []
    start = threading.Barrier(12)

    def worker():
        start.wait()
        if broker.try_acquire(7):
            wins.append(1)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1 and broker.active_total() == 1


def test_two_active_executions_globally_under_contention():
    broker = QuotaBroker()
    wins = []
    lock = threading.Lock()
    start = threading.Barrier(16)

    def worker(uid):
        start.wait()
        if broker.try_acquire(uid):
            with lock:
                wins.append(uid)

    threads = [threading.Thread(target=worker, args=(100 + i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 2 and broker.active_total() == 2


def test_release_restores_capacity_exactly():
    broker = QuotaBroker()
    assert broker.try_acquire(1) and broker.try_acquire(2)
    assert broker.try_acquire(3) is False
    broker.release(1)
    assert broker.try_acquire(3) is True
    broker.release(1)
    assert broker.active_total() == 2


def test_busy_request_is_rejected_without_queueing_or_starting(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def blocker():
        started.set()
        release.wait(10)

    rec = _RecordingRunner()
    rec.blocker = blocker
    orig_run = rec.run

    def slow(plan):
        rec.plans.append(plan)
        blocker()
        return rec.outcome

    rec.run = slow
    svc, _ = _service(tmp_path, runner=rec)
    out = {}

    def first():
        _reserve(svc, user_id=7)
        out["first"] = svc.execute(grant=_grant(user_id=7), code=CODE, run_id="r1")

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(10)
    # THE PRE-DISPATCH POINT: capacity is discovered at RESERVE time, i.e. BEFORE
    # any DispatchRecord.started could be committed for this action.
    busy = _reserve(svc, user_id=7, tool_call_id="tc-other")
    release.set()
    t.join(10)
    assert busy.ok is False and busy.status == STATUS_BUSY
    assert out["first"].ok is True
    assert svc.broker.active_total() == 0


# -- readiness -------------------------------------------------------


def _ready_env(monkeypatch, digests):
    monkeypatch.setattr(readiness_mod, "daemon_available", lambda **kw: True)
    monkeypatch.setattr(image_mod, "local_repo_digests", lambda ref=None: digests)


def test_readiness_fails_closed_when_the_daemon_is_unavailable(monkeypatch):
    called = []
    monkeypatch.setattr(readiness_mod, "daemon_available", lambda **kw: False)
    res = readiness_mod.check_readiness(
        repo_root=REPO_ROOT, probe_runner=lambda: (called.append(1) or (True, {})))
    assert res.ready is False and res.reasons == (readiness_mod.DAEMON_UNAVAILABLE,)
    assert called == []


def test_readiness_fails_closed_when_the_image_is_absent(monkeypatch):
    _ready_env(monkeypatch, None)
    res = readiness_mod.check_readiness(repo_root=REPO_ROOT,
                                        probe_runner=lambda: (True, {}))
    assert res.ready is False
    assert res.reasons == (readiness_mod.IMAGE_NOT_PROVISIONED,)


def test_readiness_fails_closed_on_a_digest_mismatch(monkeypatch):
    _ready_env(monkeypatch, ["python@sha256:" + "a" * 64])
    res = readiness_mod.check_readiness(repo_root=REPO_ROOT,
                                        probe_runner=lambda: (True, {}))
    assert res.ready is False
    assert res.reasons == (readiness_mod.IMAGE_DIGEST_MISMATCH,)


def test_readiness_fails_closed_when_the_isolation_probe_fails(monkeypatch):
    _ready_env(monkeypatch, ["python@" + image_mod.IMAGE_AMD64_DIGEST])
    res = readiness_mod.check_readiness(
        repo_root=REPO_ROOT,
        probe_runner=lambda: (False, {"probe": "failed", "failed": ["x"]}))
    assert res.ready is False and res.reasons == (readiness_mod.PROBE_FAILED,)


def test_readiness_is_true_only_when_all_three_requirements_hold(monkeypatch):
    _ready_env(monkeypatch, ["python@" + image_mod.IMAGE_AMD64_DIGEST])
    res = readiness_mod.check_readiness(repo_root=REPO_ROOT,
                                        probe_runner=lambda: (True, {"probe": "ok"}))
    assert res.ready is True and res.reasons == ()


def test_readiness_never_pulls_an_image(monkeypatch):
    calls = []

    def fake_docker(args, **kw):
        calls.append(list(args))
        if list(args)[:2] == ["image", "inspect"]:
            return 0, json.dumps(["python@" + image_mod.IMAGE_AMD64_DIGEST]), ""
        return 0, "29.8.0", ""

    monkeypatch.setattr(readiness_mod, "daemon_available", lambda **kw: True)
    monkeypatch.setattr(image_mod, "_docker", fake_docker)
    res = readiness_mod.check_readiness(repo_root=REPO_ROOT,
                                        probe_runner=lambda: (True, {}))
    assert res.ready is True
    assert "pull" not in " ".join(" ".join(c) for c in calls)


def test_cheap_availability_reports_daemon_and_image_without_the_probe(monkeypatch):
    _ready_env(monkeypatch, ["python@" + image_mod.IMAGE_AMD64_DIGEST])
    assert readiness_mod.cheap_availability() == (True, None)
    monkeypatch.setattr(readiness_mod, "daemon_available", lambda **kw: False)
    assert readiness_mod.cheap_availability() == (
        False, readiness_mod.DAEMON_UNAVAILABLE)
    monkeypatch.setattr(readiness_mod, "daemon_available", lambda **kw: True)
    monkeypatch.setattr(image_mod, "local_repo_digests", lambda ref=None: None)
    assert readiness_mod.cheap_availability() == (
        False, readiness_mod.IMAGE_NOT_PROVISIONED)


def test_service_refuses_to_run_while_unready(tmp_path):
    rec = _RecordingRunner()
    svc, _ = _service(tmp_path, runner=rec, ready=lambda: False)
    out = svc.execute(grant=_grant(), code=CODE, run_id="r1")
    assert out.ok is False and out.status == STATUS_NOT_READY
    assert rec.plans == []


def test_readiness_does_not_change_the_execution_policy_authority(monkeypatch):
    from app.run_engine import sandbox_policy
    _ready_env(monkeypatch, ["python@" + image_mod.IMAGE_AMD64_DIGEST])
    res = readiness_mod.check_readiness(repo_root=REPO_ROOT,
                                        probe_runner=lambda: (True, {}))
    assert res.ready is True
    assert sandbox_policy.real_python_execution_available() is False
    assert sandbox_policy.legacy_may_execute_python() is False


# -- HTTP surface ----------------------------------------------------


def test_http_surface_accepts_a_valid_grant_and_bounds_the_response(tmp_path):
    svc, _ = _service(tmp_path)
    client = TestClient(create_app(service=svc, availability_fn=lambda: (True, None)))
    assert client.post("/v1/execute",
                       json={"grant": "bad", "code": CODE, "run_id": "r1"}
                       ).status_code == 400
    code_with_canary = CODE + "  # " + CANARY
    sha, ln = g.code_sha256(code_with_canary)
    _reserve(svc, code_sha256=sha, code_len=ln)   # reserve THAT bound action
    res = client.post("/v1/execute",
                      json={"grant": _grant(code_sha256=sha, code_len=ln),
                            "code": code_with_canary, "run_id": "r1"})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"ok", "status", "output", "output_truncated"}
    assert CANARY not in res.text


def test_the_authorization_header_is_ignored_and_only_the_grant_authenticates(tmp_path):
    svc, _ = _service(tmp_path)
    client = TestClient(create_app(service=svc, availability_fn=lambda: (True, None)))
    _reserve(svc)
    ok = client.post("/v1/execute",
                     json={"grant": _grant(), "code": CODE, "run_id": "r1"},
                     headers={"Authorization": "Bearer obviously-not-the-credential"})
    assert ok.status_code == 200
    denied = client.post("/v1/execute",
                         json={"grant": "not-a-grant", "code": CODE, "run_id": "r1"},
                         headers={"Authorization": "Bearer whatever"})
    assert denied.status_code == 400


def test_http_replay_is_409(tmp_path):
    svc, _ = _service(tmp_path)
    client = TestClient(create_app(service=svc, availability_fn=lambda: (True, None)))
    token = _grant()
    _reserve(svc)
    assert client.post("/v1/execute",
                       json={"grant": token, "code": CODE, "run_id": "r1"}
                       ).status_code == 200
    _reserve(svc)   # a FRESH reservation for the SAME bound action
    again = client.post("/v1/execute",
                        json={"grant": token, "code": CODE, "run_id": "r1"})
    assert again.status_code == 409 and again.json()["status"] == STATUS_REPLAY


def test_http_surfaces_nonce_store_failure_as_503(tmp_path):
    class Broken:
        def consume(self, jti, *, expires_at, now):
            from executor_service.nonce_store import NonceStoreError
            raise NonceStoreError("boom")

    svc, _ = _service(tmp_path, nonce_store=Broken())
    _reserve(svc)
    client = TestClient(create_app(service=svc, availability_fn=lambda: (True, None)))
    res = client.post("/v1/execute",
                      json={"grant": _grant(), "code": CODE, "run_id": "r1"})
    assert res.status_code == 503
    assert res.json()["status"] == STATUS_NONCE_UNAVAILABLE


class _Ready:
    ready = True
    reasons = ()
    detail = {}
    failed_checks = ()


class _UnreadyProbe:
    ready = False
    reasons = ("executor_isolation_probe_failed",)
    detail = {"probe": {"probe": "failed",
                        "failed": ["network_tripwire_synchronised_and_blocked"]}}
    failed_checks = ("network_tripwire_synchronised_and_blocked",)


def test_healthz_reports_bounded_failing_probe_checks(tmp_path):
    svc, _ = _service(tmp_path)
    client = TestClient(create_app(service=svc, readiness_fn=lambda: _UnreadyProbe(),
                                   availability_fn=lambda: (True, None)))
    res = client.get("/healthz")
    assert res.status_code == 503
    body = res.json()
    assert body["ready"] is False
    assert body["reasons"] == ["executor_isolation_probe_failed"]
    assert body["failed_checks"] == ["network_tripwire_synchronised_and_blocked"]


def test_healthz_reports_startup_probe_and_live_availability(tmp_path):
    svc, _ = _service(tmp_path)

    class _ReadyProbe:
        ready = True
        reasons = ()
        detail = {"probe": {"probe": "ok", "checks": 33, "duration_ms": 100000},
                  "timings_ms": {"daemon": 3}}
        failed_checks = ()

    ready_client = TestClient(create_app(
        service=svc, readiness_fn=lambda: _ReadyProbe(),
        availability_fn=lambda: (True, None)))
    body = ready_client.get("/healthz").json()
    assert body["ready"] is True and body["reasons"] == []
    assert "failed_checks" not in body
    assert body["startup"]["probe"]["checks"] == 33
    assert body["live"]["daemon_and_image"] == "ok"

    down = TestClient(create_app(
        service=svc, readiness_fn=lambda: _ReadyProbe(),
        availability_fn=lambda: (False, "executor_daemon_unavailable")))
    res = down.get("/healthz")
    assert res.status_code == 503
    assert res.json()["reasons"] == ["executor_daemon_unavailable"]
    assert res.json()["startup"]["ready"] is True


# -- entrypoint ------------------------------------------------------


def _entry_env(monkeypatch, tmp_path, host="127.0.0.1"):
    import executor_service.__main__ as entry
    import executor_service.runner as runner_impl
    import uvicorn

    monkeypatch.setattr(readiness_mod, "check_readiness", lambda **kw: _Ready())
    monkeypatch.setattr(runner_impl, "sweep_owned_orphans", lambda owner: [])
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv(entry.ALLOW_INTERNAL_BIND_ENV, "1")
    monkeypatch.setenv("EXECUTOR_SERVICE_HOST", host)
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", "e" * 32)
    monkeypatch.setenv("EXECUTOR_SERVICE_NONCE_DB", str(tmp_path / "n.db"))
    return entry


def test_entrypoint_refuses_a_non_loopback_bind(monkeypatch, tmp_path):
    entry = _entry_env(monkeypatch, tmp_path, host="0.0.0.0")
    monkeypatch.delenv(entry.ALLOW_INTERNAL_BIND_ENV, raising=False)
    assert entry.main() == 2


def test_entrypoint_refuses_to_start_when_unready(monkeypatch, tmp_path):
    import executor_service.__main__ as entry
    monkeypatch.setattr(readiness_mod, "check_readiness",
                        lambda **kw: _UnreadyProbe())
    assert entry.main() == 2


def test_entrypoint_refuses_when_the_signing_key_is_missing(monkeypatch, tmp_path):
    entry = _entry_env(monkeypatch, tmp_path)
    monkeypatch.delenv("EXECUTOR_SERVICE_GRANT_KEY", raising=False)
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY_FILE",
                       str(tmp_path / "absent-key"))
    assert entry.main() == 2


def test_entrypoint_sweeps_owned_orphans_before_serving(monkeypatch, tmp_path):
    entry = _entry_env(monkeypatch, tmp_path)
    swept = []
    import executor_service.runner as runner_impl
    monkeypatch.setattr(runner_impl, "sweep_owned_orphans",
                        lambda owner: (swept.append(owner) or []))
    monkeypatch.setenv("EXECUTOR_SERVICE_OWNER", "mp4c-owner-test")
    assert entry.main() == 0
    assert swept == ["mp4c-owner-test"]


def test_entrypoint_evaluates_readiness_exactly_once(monkeypatch, tmp_path):
    entry = _entry_env(monkeypatch, tmp_path)
    calls = []

    class _R(_Ready):
        pass

    monkeypatch.setattr(readiness_mod, "check_readiness",
                        lambda **kw: (calls.append(1) or _R()))
    assert entry.main() == 0
    assert calls == [1]


def test_application_never_imports_the_executor_service():
    app_dir = os.path.join(REPO_ROOT, "backend", "app")
    offenders = []
    for root, _dirs, files in os.walk(app_dir):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if "executor_service" in fh.read():
                    offenders.append(os.path.relpath(path, REPO_ROOT))
    assert offenders == [], offenders
