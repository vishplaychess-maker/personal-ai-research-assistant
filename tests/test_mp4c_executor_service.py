"""MP-4C — executor-service contract: focused negative + boundary tests.

TEST-ONLY. No container is launched and no image is pulled. These pin that every
rejection happens BEFORE anything is recorded as started, and that the request
cannot influence image/argv/mounts/network/limits.
"""
from __future__ import annotations

import json
import uuid

import pytest

# The pin + readiness are authoritative in the separately runnable service, so
# monkeypatching must target THAT module (a re-export would not intercept calls).
from executor_service import image
from tests.smoke import mp4c_executor_service as svc

TOKEN = "server-owned-executor-token"


@pytest.fixture
def ready(monkeypatch):
    """Pretend the pinned image IS provisioned so the later guards are reached."""
    monkeypatch.setattr(image, "readiness", lambda **kw: (True, None))


def _svc():
    return svc.ExecutorService(token=TOKEN)


def _req(user_id=7, code="print(1)", token=TOKEN, run_id="r1"):
    return svc.ExecutionRequest(code=code, user_id=user_id, run_id=run_id, token=token)


# ── image pinning ───────────────────────────────────────────────────


def test_pinned_reference_is_digest_form_not_a_bare_tag():
    assert image.pinned_reference_is_digest_form() is True
    assert "@sha256:" in image.PINNED_REF
    assert image.IMAGE_AMD64_DIGEST in image.PINNED_REF
    assert image.IMAGE_AMD64_DIGEST != image.IMAGE_INDEX_DIGEST


def test_readiness_fails_closed_when_the_image_is_absent(monkeypatch):
    # HERMETIC: inject ABSENCE rather than depending on whether THIS host happens
    # to have the pinned image pulled. The real fail-closed logic still runs.
    monkeypatch.setattr(image, "local_repo_digests",
                        lambda ref=image.PINNED_REF: None)
    ok, reason = image.readiness()
    assert ok is False and reason == "python_image_not_provisioned"


def test_present_matching_image_passes_readiness(monkeypatch):
    # The accept path, also hermetic: exactly the pinned digest present locally.
    monkeypatch.setattr(
        image, "local_repo_digests",
        lambda ref=image.PINNED_REF: [image.IMAGE_REF + "@" + image.IMAGE_AMD64_DIGEST])
    ok, reason = image.readiness()
    assert ok is True and reason is None


def test_missing_image_is_rejected_before_any_started_record(monkeypatch):
    monkeypatch.setattr(image, "local_repo_digests",
                        lambda ref=image.PINNED_REF: None)
    s = _svc()
    res = s.execute(_req())
    assert res.ok is False and res.reason == svc.REJECT_IMAGE_MISSING
    assert s.started == [], "nothing may be recorded as started"


def test_changed_image_digest_is_rejected(monkeypatch):
    monkeypatch.setattr(image, "local_repo_digests",
                        lambda ref=image.PINNED_REF: ["python@sha256:" + "a" * 64])
    ok, reason = image.readiness()
    assert ok is False and reason == "python_image_digest_mismatch"
    s = _svc()
    res = s.execute(_req())
    assert res.ok is False and res.reason == svc.REJECT_IMAGE_DIGEST
    assert s.started == []


def test_failed_readiness_probe_is_rejected(monkeypatch):
    monkeypatch.setattr(image, "readiness", lambda **kw: (False, "executor_probe_failed"))
    s = _svc()
    res = s.execute(_req())
    assert res.ok is False and res.reason == svc.REJECT_IMAGE_MISSING
    assert s.started == []


# ── authentication ──────────────────────────────────────────────────


def test_unauthenticated_request_is_rejected(ready):
    s = _svc()
    assert s.execute(_req(token=None)).reason == svc.REJECT_UNAUTHORIZED
    assert s.execute(_req(token="wrong")).reason == svc.REJECT_UNAUTHORIZED
    assert s.execute(_req(token="")).reason == svc.REJECT_UNAUTHORIZED
    assert s.started == []


# ── quota: 1 active per user, 2 globally, before started ────────────


def test_quota_rejects_a_second_active_run_for_the_same_user(ready):
    s = _svc()
    first, reason = s.prepare(_req(user_id=7))
    assert first is not None and reason is None
    second, reason2 = s.prepare(_req(user_id=7))
    assert second is None and reason2 == svc.REJECT_QUOTA
    assert s.started == [], "a quota rejection must not record started"
    s.broker.release(7)


def test_quota_rejects_the_third_run_globally(ready):
    s = _svc()
    assert s.prepare(_req(user_id=1))[0] is not None
    assert s.prepare(_req(user_id=2))[0] is not None
    third, reason = s.prepare(_req(user_id=3))
    assert third is None and reason == svc.REJECT_QUOTA
    assert s.started == []
    assert s.broker.active_total() == 2


def test_broker_limits_are_exactly_one_per_user_and_two_global():
    b = svc.SingleBroker()
    assert b.per_user == 1 and b.global_limit == 2


# ── request surface / fixed server-side configuration ───────────────


def test_request_cannot_carry_image_argv_mounts_network_or_limits():
    fields = set(svc.ExecutionRequest.__dataclass_fields__)
    assert fields == {"code", "user_id", "run_id", "tenant_id", "token"}, fields


def test_build_argv_is_fixed_and_has_no_mounts_or_daemon_socket():
    a = svc.build_argv(name="n1")
    b = svc.build_argv(name="n2")
    joined = " ".join(a)
    assert image.PINNED_REF in a, "the digest-pinned image must be used"
    assert "--network" in a and a[a.index("--network") + 1] == "none"
    assert "--read-only" in a
    assert "--cap-drop" in a and a[a.index("--cap-drop") + 1] == "ALL"
    assert "--pids-limit" in a and a[a.index("--pids-limit") + 1] == "64"
    assert a[a.index("--memory") + 1] == "128m"
    for forbidden in ("-v", "--volume", "--mount", "--privileged",
                      "docker.sock", "/var/run/docker.sock"):
        assert forbidden not in a, forbidden
    # NO source, NO shell, NO env-carried code, NO host temp file
    assert "-c" not in a, a
    assert "sh" not in a and "bash" not in a, a
    assert "-e" not in a and "--env" not in a, a
    assert not any(x.endswith(".py") for x in a), a   # no source file is handed over
    assert svc.DOCKER_STDIN_FLAG in a, "stdin must be forwarded into the container"
    assert a[-5:] == ["python", "-I", "-B", "-u", "-"]
    assert "print(1)" not in joined
    # identical apart from the container name
    assert _strip_name(a) == _strip_name(b)


def test_execute_records_started_only_after_all_guards_pass(ready):
    s = _svc()
    seen = []
    res = s.execute(_req(), runner=lambda plan: (seen.append(plan) or
                                                 svc.ExecutionResult(ok=True, reason=None)))
    assert res.ok is True
    assert len(s.started) == 1 and seen and seen[0].argv[0] == "docker"
    assert seen[0].stdin_code == b"print(1)"
    assert s.broker.active_total() == 0, "the slot must be released"


# ── code transport: command metadata must NEVER carry Python source ─
#
# RED before the stdin change: build_argv appended `-c <code>`, so the model's
# source was visible in the Docker argv, in `docker inspect` Config.Cmd/Path/Args,
# and in the container process command line.

CODE_CANARY = "MP4C_CODE_CANARY_" + uuid.uuid4().hex


def _runner_argv(received):
    """The argv the runner actually got, whatever shape it has."""
    return list(getattr(received, "argv", received))


def _strip_name(argv):
    out = list(argv)
    out[out.index("--name") + 1] = "<name>"
    return out


def _capture_execute(s, code):
    seen = []
    res = s.execute(_req(code=code),
                    runner=lambda received: (
                        seen.append(_runner_argv(received))
                        or svc.ExecutionResult(ok=True, reason=None)))
    assert res.ok is True, res
    return seen[0]


def test_unique_code_canary_is_absent_from_command_metadata(ready):
    """The focused RED: a unique code canary must not reach command metadata."""
    argv = _capture_execute(_svc(), "x = 1  # " + CODE_CANARY + "\nprint(x)\n")
    metadata = " ".join(argv)
    assert CODE_CANARY not in metadata, metadata
    assert "x = 1" not in metadata, metadata


def test_code_is_not_transported_with_dash_c(ready):
    argv = _capture_execute(_svc(), "print(1)")
    assert "-c" not in argv, argv
    assert argv[-1] == "-", argv
    assert argv[-5:] == ["python", "-I", "-B", "-u", "-"], argv


def test_two_different_codes_produce_identical_argv(ready):
    s = _svc()
    first = _capture_execute(s, "print(1)")
    second = _capture_execute(s, "print(2)  # a completely different program")
    assert _strip_name(first) == _strip_name(second), (first, second)


# ── the new stdin transport itself (coverage added WITH the change) ─


def test_stdin_payload_is_exactly_the_bounded_utf8_code(ready):
    s = _svc()
    code = "print('\u00e9\u4e2d')  # multibyte source"
    plan, reason = s.prepare(_req(code=code))
    assert reason is None
    try:
        assert plan.stdin_code == code.encode("utf-8")
        assert len(plan.stdin_code) <= svc.MAX_CODE_BYTES
        assert list(plan.argv[-5:]) == ["python", "-I", "-B", "-u", "-"]
    finally:
        s.broker.release(7)


def test_plan_repr_and_broker_records_never_contain_code(ready):
    s = _svc()
    plan, reason = s.prepare(_req(code="secret = 1  # " + CODE_CANARY))
    assert reason is None
    try:
        assert CODE_CANARY in plan.stdin_code.decode("utf-8")
        blob = repr(plan) + json.dumps(s.started) + json.dumps(s.rejections)
        assert CODE_CANARY not in blob, blob
        assert "withheld" in repr(plan)
    finally:
        s.broker.release(7)


def test_sanitized_error_text_and_records_contain_no_code(ready):
    s = _svc()
    canary = "MP4C_ERR_CANARY_" + uuid.uuid4().hex
    code = "raise ValueError('" + canary + "')"

    def sanitising_runner(plan):
        assert canary in plan.stdin_code.decode("utf-8")
        assert canary not in " ".join(plan.argv)
        return svc.ExecutionResult(
            ok=False, reason="runtime_error", is_error=True,
            text="[Python Sandbox] Execution failed with exit code 1. (runtime_error)")

    res = s.execute(_req(code=code), runner=sanitising_runner)
    assert res.ok is False
    assert canary not in res.text and "ValueError" not in res.text
    assert canary not in json.dumps(s.started) + json.dumps(s.rejections)


def test_oversized_code_is_rejected_before_any_started_record(ready):
    s = _svc()
    res = s.execute(_req(code="x = 1\n" * 2000))  # 12000 bytes > 8192
    assert res.ok is False and res.reason == svc.REJECT_CODE_INVALID
    assert s.started == []
    assert s.broker.active_total() == 0, "a rejected request must not hold a slot"


def test_code_size_bound_is_utf8_bytes_not_characters(ready):
    s = _svc()
    res = s.execute(_req(code="\u00e9" * 5000))  # 5000 chars, 10000 UTF-8 bytes
    assert res.ok is False and res.reason == svc.REJECT_CODE_INVALID
    assert s.started == []
