"""MP-4C - DISPOSABLE end-to-end journey through the REAL stack.

Journey: RunWorker -> real approval gateway -> REAL broker client over HTTP ->
real executor service (separate process, full isolation probe) -> REAL Python
container. Nothing is faked on the execution path: the only deterministic part
is the chat provider that emits the [PYTHON_CODE] marker (the model is replaced,
NOT the executor).

Run it yourself:
    $env:PYTHONPATH="<repo>\backend;<repo>"
    .venv\Scripts\python.exe tests\smoke\mp4c_runner_e2e.py

It starts its OWN executor on 127.0.0.1:8099 with a TEMP nonce database and a
unique owner label, then tears both down. It never touches any other container,
never enables the opt-in in the user's stack, and modifies no repository data.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ARTIFACT = REPO / "tests" / "smoke" / "mp4c_runner_e2e.json"
PORT = 8099
BASE = "http://127.0.0.1:%d" % PORT
KEY = "mp4c-runner-e2e-grant-key-0123456789"
CODE_MARKER = "MP4C-RUNNER-E2E-" + uuid.uuid4().hex[:8]
CODE = "print('%s')" % CODE_MARKER

CHECKS = []
RESULT = {"ok": False}


def ck(name, ok, seen=None):
    CHECKS.append({"check": name, "pass": bool(ok), "seen": None if seen is None
                   else str(seen)[:400], "seen_missing": seen is None})
    return bool(ok)


def _docker(args, timeout=120):
    return subprocess.run(["docker"] + list(args), capture_output=True,
                          text=True, timeout=timeout)


def _nonce_rows(path):
    try:
        con = sqlite3.connect(path)
        try:
            return int(con.execute("select count(*) from nonces").fetchone()[0])
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return None


def _wait_ready(proc, log_path, deadline_s=900):
    import urllib.request
    deadline = time.time() + deadline_s
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, "executor exited rc=%s" % proc.returncode
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=5) as r:
                body = r.read(2000).decode("utf-8", "replace")
                return True, body
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
            time.sleep(2)
    tail = ""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
    except Exception:  # noqa: BLE001
        pass
    return False, "not ready (%s) tail=%s" % (last, tail)


def _sweep(owner):
    ids = _docker(["ps", "-a", "-q", "--filter", "label=mp4c.owner=" + owner]).stdout.split()
    if ids:
        _docker(["rm", "-f"] + ids)
    return ids


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mp4c_runner_e2e_")
    tmp_path = Path(tmp)
    nonce_db = tmp_path / "nonces.db"
    log_path = tmp_path / "executor.log"
    owner = "mp4c-runner-" + uuid.uuid4().hex[:8]
    proc = None
    log_fh = None
    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO / "backend"), str(REPO), env.get("PYTHONPATH", "")])
        env["EXECUTOR_SERVICE_GRANT_KEY"] = KEY
        env["EXECUTOR_SERVICE_NONCE_DB"] = str(nonce_db)
        env["EXECUTOR_SERVICE_OWNER"] = owner
        env["EXECUTOR_SERVICE_HOST"] = "127.0.0.1"
        env["EXECUTOR_SERVICE_PORT"] = str(PORT)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        log_fh = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-m", "executor_service"],
                                cwd=str(REPO), env=env, stdout=log_fh,
                                stderr=subprocess.STDOUT)
        ready, detail = _wait_ready(proc, log_path)
        ck("executor_ready", ready, detail[:200])
        if not ready:
            return 1

        os.environ["EXECUTOR_OPERATOR_OPT_IN"] = "1"
        os.environ["EXECUTOR_SERVICE_URL"] = BASE
        os.environ["EXECUTOR_SERVICE_GRANT_KEY"] = KEY
        from cryptography.fernet import Fernet
        os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        os.environ["RUN_IDEMPOTENCY_HMAC_KEY"] = "mp4c-runner-e2e-hmac-key"
        os.environ["DATABASE_URL"] = "sqlite://"

        from app.config import settings
        settings.run_public_api_enabled = True
        settings.run_engine_enabled = True
        settings.run_chat_stream_enabled = True
        settings.run_engine_sandbox = True
        settings.run_worker_lease_ttl_s = 30
        settings.run_worker_handler_timeout_s = 300
        settings.run_chat_max_tool_cycles = 3
        settings.run_chat_tool_marker_max_bytes = 4096

        import pytest
        from app.models.models import DispatchRecord, Message, RunPayload, RunStep
        from app.run_engine import executor_broker as eb
        from app.run_engine import sandbox_policy
        from app.run_engine.worker import RunWorker
        from tests import test_mp4c_r2a3_python_proposal_worker as r2a3
        from tests.run_engine_helpers import mk_engine, mk_factory
        from tests.test_run_engine_mp3c2 import _decide_run
        from tests.test_mp4c_r2b2c2_python_routing import _park_python, _q

        sandbox_policy.reset_executor_readiness_probe()
        client = eb.wire_from_env()
        ck("broker_client_wired", client is not None)
        ck("authority_true_with_live_executor",
           sandbox_policy.real_python_execution_available() is True)

        calls = {"execute": [], "reserve": 0, "release": 0}

        class CountingBroker:
            def ready(self):
                return client.ready()
            def reserve(self, *, grant):
                calls["reserve"] += 1
                return client.reserve(grant=grant)
            def execute(self, *, grant, code, run_id):
                calls["execute"].append({"code": code, "run_id": run_id})
                return client.execute(grant=grant, code=code, run_id=run_id)
            def release(self, *, grant):
                calls["release"] += 1
                return client.release(grant=grant)

        broker = CountingBroker()
        mp = pytest.MonkeyPatch()
        eng = mk_engine()
        try:
            rid, prov, tr = _park_python(eng, mp, code=CODE)
            prov.scripts.append(["RUNNER-CONTINUATION-OK"])
            parked = r2a3._run(eng, rid)
            ck("proposal_parked_for_approval",
               parked.status == "waiting_for_approval" and parked.pending_approval_id,
               parked.status)
            _decide_run(eng, parked.pending_approval_id, approve=True)

            before = _nonce_rows(nonce_db)
            worker = RunWorker(mk_factory(eng), worker_id="runner-e2e",
                               chat_provider=(lambda ci, **_k: prov),
                               python_broker=broker, broker_ready=broker.ready)
            for _ in range(8):
                worker.advance_chat_runs()
                if r2a3._run(eng, rid).status in ("succeeded", "failed",
                                                  "outcome_unknown", "denied"):
                    break

            run = r2a3._run(eng, rid)
            after = _nonce_rows(nonce_db)
            ledger = _q(eng, lambda db: [(r.outcome,) for r in
                                         db.query(DispatchRecord).all()])
            succeeded = sum(1 for row in ledger if row[0] == "succeeded")
            outputs = _q(eng, lambda db: db.query(RunPayload)
                         .filter(RunPayload.payload_type == "tool_output").count())
            assistants = _q(eng, lambda db: [m for m in db.query(Message).all()
                                             if str(m.role).endswith("assistant")])

            ck("run_succeeded", run.status == "succeeded",
               (run.status, run.status_reason))
            ck("exactly_one_executor_send", len(calls["execute"]) == 1,
               len(calls["execute"]))
            ck("exact_approved_code_was_sent",
               bool(calls["execute"]) and calls["execute"][0]["code"] == CODE,
               calls["execute"][0]["code"] if calls["execute"] else None)
            ck("nonce_store_gained_exactly_one_row",
               isinstance(after, int) and isinstance(before, int)
               and after == before + 1, (before, after))
            ck("exactly_one_completed_dispatch_ledger_row",
               len(ledger) == 1 and succeeded == 1, ledger)
            ck("one_sealed_tool_output", outputs == 1, outputs)
            ck("one_same_run_continuation", len(assistants) == 1
               and "RUNNER-CONTINUATION-OK" in assistants[0].content,
               len(assistants))

            reloaded = RunWorker(mk_factory(eng), worker_id="runner-e2e-2",
                                 chat_provider=(lambda ci, **_k: prov),
                                 python_broker=broker, broker_ready=broker.ready)
            for _ in range(3):
                reloaded.advance_chat_runs()
            after2 = _nonce_rows(nonce_db)
            ledger2 = _q(eng, lambda db: db.query(DispatchRecord).count())
            outputs2 = _q(eng, lambda db: db.query(RunPayload)
                          .filter(RunPayload.payload_type == "tool_output").count())
            ck("no_duplicate_execution_after_reload",
               isinstance(after2, int) and after2 == after
               and ledger2 == 1 and outputs2 == 1
               and len(calls["execute"]) == 1,
               (before, after, after2, ledger2, outputs2,
                len(calls["execute"])))
            ck("transport_never_used", tr.calls == [], tr.calls)
        finally:
            eng.dispose()
            mp.undo()
        # fall through to the shared teardown + verdict
    except Exception as exc:  # noqa: BLE001 - never lose the verdict
        RESULT["crashed"] = "%s: %s" % (type(exc).__name__,
                                         str(exc)[:180])
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                proc.kill()
        leftover = _sweep(owner)
        if log_fh is not None:
            log_fh.close()
        ok = (all(c["pass"] for c in CHECKS) and bool(CHECKS)
              and RESULT.get("crashed") is None)
        RESULT["ok"] = ok
        payload = {
            "aggregate_ok": ok,
            "artifact": "mp4c_runner_e2e.json",
            "journey": "RunWorker -> real gateway -> real executor -> real container",
            "executor_base_url": BASE,
            "code_marker": CODE_MARKER,
            "owner_label": owner,
            "containers_left": len(leftover),
            "crashed": RESULT.get("crashed"),
            "checks": CHECKS,
        }
        ARTIFACT.write_text(json.dumps(payload, indent=1, sort_keys=True),
                            encoding="utf-8")
        print("RUNNER_E2E_RESULT " + json.dumps({"aggregate_ok": ok,
              "failed": [c["check"] for c in CHECKS if not c["pass"]]}))
    return 0 if RESULT["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())