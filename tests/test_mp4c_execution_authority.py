"""MP-4C - the ONE execution authority, the broker client and the inert wiring.

Everything here is offline: the transport is a recording opener, so no socket is
opened and no container is involved. What is pinned:

  * `real_python_execution_available()` is the ONLY authorization, it is False by
    default, and it requires opt-in + endpoint + a LIVE ready probe;
  * the probe can only ever DENY - a missing, raising or not-ready probe is
    False, and nothing consults the probe outside that one predicate;
  * the broker client is INERT at construction and reduces every response to a
    bounded result without leaking a grant, code or body fragment;
  * `RunWorkerSupervisor` wires the broker without performing ANY I/O at
    startup, and a missing/unreachable broker cannot stop it starting.
"""
from __future__ import annotations

import json
import logging

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.run_engine import executor_broker as eb
from app.run_engine import sandbox_policy
from app.run_engine.worker_supervisor import RunWorkerSupervisor

URL = "http://executor:8091"
KEY = "authority-grant-key-0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("EXECUTOR_OPERATOR_OPT_IN", raising=False)
    monkeypatch.delenv("EXECUTOR_SERVICE_URL", raising=False)
    monkeypatch.delenv("EXECUTOR_SERVICE_GRANT_KEY", raising=False)
    monkeypatch.delenv("EXECUTOR_SERVICE_GRANT_KEY_FILE", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-authority-hmac-key1")
    sandbox_policy.reset_executor_readiness_probe()
    yield
    sandbox_policy.reset_executor_readiness_probe()


class Recorder:
    """A transport double: records every call and replays queued responses."""

    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, method, url, data, timeout):
        self.calls.append({"method": method, "url": url,
                           "body": json.loads(data.decode("utf-8"))
                           if data is not None else None,
                           "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected call: %s %s" % (method, url))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _client(*responses, base=URL):
    rec = Recorder(*responses)
    return eb.ExecutorBrokerClient(base_url=base, opener=rec), rec


# ── 1. the ONE authority: default OFF and strictly conjunctive ───────


def test_authority_is_off_with_no_configuration_at_all():
    assert sandbox_policy.operator_opt_in_enabled() is False
    assert sandbox_policy.executor_configured() is False
    assert sandbox_policy.real_python_execution_available() is False


def test_opt_in_alone_is_not_enough(monkeypatch):
    monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", "1")
    sandbox_policy.register_executor_readiness_probe(lambda: True)
    assert sandbox_policy.operator_opt_in_enabled() is True
    assert sandbox_policy.real_python_execution_available() is False, \
        "an endpoint is still required"


def test_endpoint_alone_is_not_enough(monkeypatch):
    monkeypatch.setenv("EXECUTOR_SERVICE_URL", URL)
    sandbox_policy.register_executor_readiness_probe(lambda: True)
    assert sandbox_policy.executor_configured() is True
    assert sandbox_policy.real_python_execution_available() is False, \
        "an explicit operator opt-in is still required"


def test_ready_probe_alone_is_not_enough(monkeypatch):
    monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", "1")
    monkeypatch.setenv("EXECUTOR_SERVICE_URL", URL)
    assert sandbox_policy.real_python_execution_available() is False, \
        "an unregistered probe is not readiness"


def test_authority_true_only_when_every_requirement_holds(monkeypatch):
    monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", "1")
    monkeypatch.setenv("EXECUTOR_SERVICE_URL", URL)
    sandbox_policy.register_executor_readiness_probe(lambda: True)
    assert sandbox_policy.real_python_execution_available() is True


def test_a_raising_or_not_ready_probe_is_never_readiness(monkeypatch):
    monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", "1")
    monkeypatch.setenv("EXECUTOR_SERVICE_URL", URL)

    def _boom():
        raise RuntimeError("executor is down")

    sandbox_policy.register_executor_readiness_probe(_boom)
    assert sandbox_policy.real_python_execution_available() is False
    sandbox_policy.register_executor_readiness_probe(lambda: False)
    assert sandbox_policy.real_python_execution_available() is False
    sandbox_policy.reset_executor_readiness_probe()
    assert sandbox_policy.real_python_execution_available() is False


def test_opt_in_env_parsing_is_strict(monkeypatch):
    monkeypatch.setenv("EXECUTOR_SERVICE_URL", URL)
    for value in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", value)
        assert sandbox_policy.operator_opt_in_enabled() is True, value
    for value in ("", "0", "false", "off", "no", "maybe", "2"):
        monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", value)
        assert sandbox_policy.operator_opt_in_enabled() is False, value


# ── 2. the broker client: inert, bounded, non-leaking ────────────────


def test_construction_performs_no_io():
    rec = Recorder()
    eb.ExecutorBrokerClient(base_url=URL, opener=rec)
    assert rec.calls == []


def test_build_from_env_fails_closed(monkeypatch):
    assert eb.build_from_env() is None                       # nothing configured
    monkeypatch.setenv("EXECUTOR_SERVICE_URL", URL)
    assert eb.build_from_env() is None                       # no signing secret
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", "short")
    assert eb.build_from_env() is None, "a trivial key is not a key"
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", KEY)
    client = eb.build_from_env()
    assert client is not None
    assert eb.build_from_env({"EXECUTOR_SERVICE_URL": "not-a-url",
                              "EXECUTOR_SERVICE_GRANT_KEY": KEY}) is None


def test_ready_is_strictly_fail_closed():
    ok, _ = _client((200, b'{"ready": true}'))
    assert ok.ready() is True
    for response in ((200, b"{}"), (200, b"not json"), (503, b'{"ready": true}'),
                     (200, b'{"ready": "true"}'), (200, b"null")):
        client, _ = _client(response)
        assert client.ready() is False, response
    boom, _ = _client(RuntimeError("connection refused"))
    assert boom.ready() is False


def test_every_request_body_carries_no_identity_beyond_the_grant():
    client, rec = _client((200, b'{"ok": true, "status": "executor_reserved"}'),
                          (200, b'{"ok": true, "status": "executor_released"}'),
                          (200, b'{"ok": true, "status": "ok", "output": "OUT"}'))
    client.reserve(grant="G1")
    client.release(grant="G1")
    client.execute(grant="G1", code="print(1)", run_id="R1")
    assert [(c["method"], c["url"], sorted(c["body"])) for c in rec.calls] == [
        ("POST", URL + "/v1/reserve", ["grant"]),
        ("POST", URL + "/v1/release", ["grant"]),
        ("POST", URL + "/v1/execute", ["code", "grant", "run_id"]),
    ]
    assert rec.calls[2]["body"] == {"grant": "G1", "code": "print(1)",
                                    "run_id": "R1"}


def test_results_are_bounded_even_for_hostile_bodies():
    huge_body = json.dumps({"ok": False, "status": "A" * 400}).encode("utf-8")
    client, _ = _client(
        (429, b'{"ok": false, "status": "executor_quota_exceeded"}'),
        (400, huge_body),
        (400, b"<html>a proxy rejected this</html>"),
        (502, b"<html>gateway blew up</html>"),
        RuntimeError("socket closed"),
        (200, b'{"ok": true, "status": "ok", "output": 42}'),
    )
    quota = client.reserve(grant="G")
    assert (quota.ok, quota.status) == (False, "executor_quota_exceeded")
    huge = client.reserve(grant="G")
    assert huge.status == "executor_unavailable", huge.status
    bad = client.reserve(grant="G")
    assert bad.status == eb.STATUS_REQUEST_REJECTED, bad.status
    html = client.reserve(grant="G")
    assert html.status == eb.STATUS_UNAVAILABLE, html.status
    down = client.reserve(grant="G")
    assert (down.ok, down.status) == (False, eb.STATUS_UNAVAILABLE)
    odd = client.execute(grant="G", code="c", run_id="r")
    assert odd.output == "" and odd.ok is True


def test_the_grant_and_code_never_reach_a_log_or_an_exception(caplog):
    secret_grant = "TOKEN-DO-NOT-LEAK-0123456789"
    secret_code = "print('DO-NOT-LEAK-CODE')"
    client, _ = _client((200, b'{"ok": false, "status": "executor_unavailable"}'),
                        RuntimeError("boom"))
    with caplog.at_level(logging.DEBUG):
        client.reserve(grant=secret_grant)
        client.execute(grant=secret_grant, code=secret_code, run_id="R")
    assert secret_grant not in caplog.text
    assert secret_code not in caplog.text
    assert secret_grant not in repr(client.__dict__)


# ── 3. the supervisor wiring is INERT ────────────────────────────────


def _configure(monkeypatch, *, key=KEY, url=URL):
    monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", "1")
    if url:
        monkeypatch.setenv("EXECUTOR_SERVICE_URL", url)
    if key:
        monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", key)


def test_supervisor_startup_performs_no_io_and_still_starts(monkeypatch):
    _configure(monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(eb, "_http_opener", rec)
    sv = RunWorkerSupervisor()
    assert rec.calls == [], "supervisor construction must not touch the network"
    worker = sv._worker_factory()
    assert worker._python_broker is not None
    assert worker._python_executor_injected is False
    assert callable(worker._broker_ready)
    assert rec.calls == [], "building the worker must not touch the network"
    # Only the authority consults the probe, and only when it is asked.
    rec.responses = [(200, b'{"ready": true}')]
    assert sandbox_policy.real_python_execution_available() is True
    assert [c["url"] for c in rec.calls] == [URL + "/healthz"]


def test_registering_the_probe_is_io_free_and_not_authorization(monkeypatch):
    _configure(monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(eb, "_http_opener", rec)
    assert eb.wire_from_env() is not None
    assert rec.calls == [], "registration performs NO I/O"
    rec.responses = [(503, b'{"ready": false}')]
    assert sandbox_policy.real_python_execution_available() is False
    assert [c["url"] for c in rec.calls] == [URL + "/healthz"]


def test_a_missing_secret_leaves_the_authority_false(monkeypatch):
    _configure(monkeypatch, key=None)
    sv = RunWorkerSupervisor()
    worker = sv._worker_factory()
    assert worker._python_broker is None
    assert sandbox_policy.real_python_execution_available() is False


def test_a_broker_outage_is_not_authorization(monkeypatch):
    _configure(monkeypatch)
    rec = Recorder(RuntimeError("connection refused"))
    monkeypatch.setattr(eb, "_http_opener", rec)
    RunWorkerSupervisor()
    assert sandbox_policy.real_python_execution_available() is False
    assert [c["url"] for c in rec.calls] == [URL + "/healthz"]


def test_broken_broker_wiring_must_not_stop_the_supervisor(monkeypatch):
    _configure(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("wiring exploded")

    monkeypatch.setattr(eb, "wire_from_env", _boom)
    sv = RunWorkerSupervisor()                 # must not raise
    assert sv._worker_factory()._python_broker is None
    assert sandbox_policy.real_python_execution_available() is False


def test_compose_keeps_the_opt_in_off_by_default():
    import pathlib
    compose = (pathlib.Path(__file__).resolve().parents[1]
               / "docker-compose.yml").read_text(encoding="utf-8")
    assert "EXECUTOR_OPERATOR_OPT_IN=${EXECUTOR_OPERATOR_OPT_IN:-}" in compose
    assert "EXECUTOR_SERVICE_URL=${EXECUTOR_SERVICE_URL:-}" in compose
    assert "true" not in compose.split("EXECUTOR_OPERATOR_OPT_IN")[1][:20]


def test_all_eight_run_flags_stay_default_false():
    for name in ("run_engine_enabled", "run_worker_enabled",
                 "run_messages_stream_durable_enabled", "run_deep_research_enabled",
                 "run_multi_agent_enabled", "run_public_api_enabled",
                 "run_chat_stream_enabled", "run_engine_sandbox"):
        assert getattr(settings, name) is False, name
