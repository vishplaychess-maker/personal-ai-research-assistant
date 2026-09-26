"""MP-4C — deployable executor topology: hermetic Compose + loader tests.

TEST-ONLY. Nothing here starts a container or touches the daemon: it parses the
Compose file, the Dockerfile and the credential loader, and pins the boundaries
that the deployment must keep.

The live Compose startup + authenticated request is exercised separately (see the
slice report); these tests are the fast regression gate for the topology.
"""
from __future__ import annotations

import os

import pytest
import yaml

from executor_service import identity as identity_mod
from executor_service.identity import (IdentityRegistry, load_registry,
                                       registry_from_env, registry_from_file)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = os.path.join(REPO_ROOT, "docker-compose.yml")
DOCKERFILE = os.path.join(REPO_ROOT, "executor_service", "Dockerfile")
PROBE = os.path.join(REPO_ROOT, "tests", "smoke", "mp4c_isolation_probe.py")
GITIGNORE = os.path.join(REPO_ROOT, ".gitignore")
SOCKET = "/var/run/docker.sock"
SECRET_NAME = "executor_grant_key"
SECRET_TARGET = "/run/secrets/executor_grant_key"
SIGNING_KEY_ENV = "EXECUTOR_SERVICE_GRANT_KEY_FILE"


@pytest.fixture(scope="module")
def compose():
    with open(COMPOSE, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _volumes(service):
    return service.get("volumes") or []


def _env(service):
    """Compose allows a list of KEY=VALUE or a mapping; normalise to a dict."""
    env = service.get("environment") or {}
    if isinstance(env, list):
        out = {}
        for item in env:
            k, _sep, v = str(item).partition("=")
            out[k] = v
        return out
    return dict(env)


def _mounts_docker(service):
    for v in _volumes(service):
        if isinstance(v, str) and SOCKET in v:
            return True
        if isinstance(v, dict) and SOCKET in str(v.get("source", "")):
            return True
    return False


# ── the socket boundary ─────────────────────────────────────────────


def test_only_the_executor_service_reaches_the_docker_daemon(compose):
    holders = [n for n, s in compose["services"].items() if _mounts_docker(s)]
    assert holders == ["executor"], holders


def test_exactly_one_socket_mount_and_it_is_the_default_path(compose):
    vols = [v for v in _volumes(compose["services"]["executor"])
            if SOCKET in str(v)]
    assert len(vols) == 1, vols
    v = vols[0]
    assert (v["source"] if isinstance(v, dict) else v) in (SOCKET, SOCKET + ":" + SOCKET)
    if isinstance(v, dict):
        assert v["target"] == SOCKET


def test_backend_frontend_and_chromadb_get_no_socket(compose):
    for name in ("backend", "frontend", "chromadb"):
        assert not _mounts_docker(compose["services"][name]), name


def test_executed_containers_get_no_socket(compose):
    """The executor's own contract is the only thing it may launch, and that
    contract never mounts a socket (pinned by the probe's docker_socket_absent)."""
    with open(os.path.join(REPO_ROOT, "executor_service", "contract.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert SOCKET not in src and "docker-sock" not in src
    assert "-v" not in src.split("def build_argv")[1].split("]")[0]


# ── network exposure ────────────────────────────────────────────────


def test_executor_publishes_no_host_port(compose):
    ex = compose["services"]["executor"]
    assert not ex.get("ports"), ex.get("ports")
    assert ex.get("expose") == ["8091"]


def test_only_the_backend_shares_the_executor_network(compose):
    services = compose["services"]
    on_net = {n for n, s in services.items()
              if "executor-internal" in (s.get("networks") or [])}
    assert on_net == {"executor", "backend"}, on_net
    assert services["executor"].get("networks") == ["executor-internal"]


def test_backend_keeps_its_existing_network_access(compose):
    nets = compose["services"]["backend"].get("networks") or []
    assert "research-network" in nets, nets


def test_the_executor_network_is_internal(compose):
    assert compose["networks"]["executor-internal"]["internal"] is True


def test_no_service_depends_on_executor_startup(compose):
    """Adding a depends_on would couple the app's startup to a multi-minute
    readiness probe; the executor is additive in this slice."""
    for name, svc in compose["services"].items():
        assert "executor" not in (svc.get("depends_on") or {}), name


# ── credential provisioning ─────────────────────────────────────────


def test_the_signing_secret_is_a_file_secret_provisioned_to_exactly_two_services(compose):
    services = compose["services"]
    holders = {n for n, s in services.items()
               if SECRET_NAME in [x["source"] if isinstance(x, dict) else x
                                  for x in (s.get("secrets") or [])]}
    assert holders == {"backend", "executor"}, holders
    assert "file" in compose["secrets"][SECRET_NAME]
    env = _env(services["executor"])
    assert "EXECUTOR_SERVICE_GRANT_KEY" not in env, "never an inline env value"
    assert env.get(SIGNING_KEY_ENV) == SECRET_TARGET
    assert _env(services["backend"]).get(SIGNING_KEY_ENV) == SECRET_TARGET


def test_the_static_token_map_is_not_a_production_fallback(compose):
    """The one-token-per-user map cannot represent arbitrary users, so it must not
    appear in the deployment at all."""
    env = _env(compose["services"]["executor"])
    assert "EXECUTOR_SERVICE_TOKENS" not in env
    assert not any("tokens" in str(s).lower()
                   for s in (compose["services"]["executor"].get("secrets") or []))
    assert "executor_service_tokens" not in compose["secrets"]


def test_the_durable_nonce_volume_is_mounted_on_the_executor(compose):
    vols = compose["services"]["executor"].get("volumes") or []
    joined = " ".join(str(v) for v in vols)
    assert "executor_nonce_data:/var/lib/executor" in joined, joined
    assert "executor_nonce_data" in compose["volumes"], "the volume must be declared"


def test_the_nonce_volume_is_mounted_nowhere_else(compose):
    for name, svc in compose["services"].items():
        if name == "executor":
            continue
        vols = svc.get("volumes") or []
        assert not any("executor_nonce_data" in str(v) for v in vols), name


def test_no_token_is_checked_into_the_compose_file(compose):
    # Structural, not text-matching: a token map carries "principal"/"user_id", so
    # assert none of it is supplied as an environment VALUE anywhere.
    for name, svc in compose["services"].items():
        for key, value in _env(svc).items():
            assert "principal" not in value and "user_id" not in value, (name, key)
    ref = compose["secrets"][SECRET_NAME]
    assert "file" in ref and "secrets" in ref["file"]
    assert "environment" not in ref


def test_the_secret_file_is_gitignored():
    with open(GITIGNORE, "r", encoding="utf-8") as fh:
        gi = fh.read()
    assert "secrets/*" in gi
    assert "!secrets/README.md" in gi
    assert "!secrets/*.example" in gi


def test_example_template_is_tracked_and_holds_no_real_token():
    with open(os.path.join(REPO_ROOT, "secrets", "executor_grant_key.example"),
              encoding="utf-8") as fh:
        text = fh.read()
    assert "REPLACE-WITH" in text


# ── single replica / process-local quotas ───────────────────────────


def test_executor_is_single_replica_and_documented_as_process_local(compose):
    ex = compose["services"]["executor"]
    assert "deploy" not in ex or not (ex.get("deploy") or {}).get("replicas")
    assert "scale" not in ex
    with open(COMPOSE, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "PROCESS-LOCAL" in text


# ── image ───────────────────────────────────────────────────────────


def test_executor_image_uses_the_pinned_digest_and_only_the_cli():
    with open(DOCKERFILE, "r", encoding="utf-8") as fh:
        src = fh.read()
    assert "sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b" in src
    assert "docker-cli" in src
    assert "dockerd" not in src and "docker.io" not in src
    assert "backend/mp4c_contract" in src, "the SHARED contract must ship in the image"
    assert "/var/lib/executor" in src, "the nonce volume mount point must be owned"
    assert "USER 10001:0" in src, "non-root, but GID 0 for the root:root 0660 socket"
    assert "mp4c_isolation_probe.py" in src


def test_dockerfile_ships_no_application_code():
    with open(DOCKERFILE, "r", encoding="utf-8") as fh:
        src = fh.read()
    # The SHARED contract is the only backend/ path allowed into this image.
    stripped = src.replace("COPY backend/mp4c_contract /srv/mp4c_contract/", "")
    assert "backend/" not in stripped
    assert "COPY frontend" not in src


# ── credential loader ───────────────────────────────────────────────


def test_registry_from_file_fails_closed(tmp_path):
    missing = str(tmp_path / "nope.json")
    assert len(registry_from_file(missing)) == 0
    blank = tmp_path / "blank.json"
    blank.write_text("   ", encoding="utf-8")
    assert len(registry_from_file(str(blank))) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert len(registry_from_file(str(bad))) == 0
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2]", encoding="utf-8")
    assert len(registry_from_file(str(arr))) == 0


def test_registry_from_file_parses_a_valid_secret(tmp_path):
    good = tmp_path / "ok.json"
    good.write_text('{"tok-a": {"principal": "backend", "user_id": 5,'
                    ' "tenant_id": "t-2"}}', encoding="utf-8")
    reg = registry_from_file(str(good))
    assert len(reg) == 1
    ident = reg.resolve("tok-a")
    assert (ident.principal, ident.user_id, ident.tenant_id) == ("backend", 5, "t-2")
    assert reg.resolve("wrong") is None


def test_load_registry_prefers_the_file_and_is_authoritative(tmp_path):
    f = tmp_path / "secrets.json"
    f.write_text('{"from-file": {"user_id": 1}}', encoding="utf-8")
    env = {"EXECUTOR_SERVICE_TOKENS_FILE": str(f),
           "EXECUTOR_SERVICE_TOKENS": '{"from-env": {"user_id": 2}}'}
    reg = load_registry(env)
    assert reg.resolve("from-file") is not None
    assert reg.resolve("from-env") is None, "an existing file must win outright"


def test_load_registry_falls_back_to_env_only_when_no_file(tmp_path):
    env = {"EXECUTOR_SERVICE_TOKENS_FILE": str(tmp_path / "absent.json"),
           "EXECUTOR_SERVICE_TOKENS": '{"from-env": {"user_id": 2}}'}
    assert load_registry(env).resolve("from-env") is not None


def test_a_present_but_broken_file_does_not_fall_back_to_env(tmp_path):
    f = tmp_path / "broken.json"
    f.write_text("not json", encoding="utf-8")
    env = {"EXECUTOR_SERVICE_TOKENS_FILE": str(f),
           "EXECUTOR_SERVICE_TOKENS": '{"from-env": {"user_id": 2}}'}
    reg = load_registry(env)
    assert len(reg) == 0 and reg.resolve("from-env") is None


def test_load_registry_is_empty_when_nothing_is_configured(tmp_path):
    env = {"EXECUTOR_SERVICE_TOKENS_FILE": str(tmp_path / "absent.json")}
    assert len(load_registry(env)) == 0


# ── bind opt-in ─────────────────────────────────────────────────────


def test_bind_is_loopback_by_default_and_internal_bind_needs_optin():
    import executor_service.__main__ as entry
    for host in ("127.0.0.1", "::1", "localhost"):
        assert entry.bind_is_allowed(host, {}) is True
    for host in ("0.0.0.0", "10.0.0.5", "::"):
        assert entry.bind_is_allowed(host, {}) is False, host
    opt = {entry.ALLOW_INTERNAL_BIND_ENV: "1"}
    assert entry.bind_is_allowed("0.0.0.0", opt) is True
    for falsy in ("", "0", "no", "off", "false"):
        assert entry.bind_is_allowed("0.0.0.0",
                                     {entry.ALLOW_INTERNAL_BIND_ENV: falsy}) is False


def test_entrypoint_allows_the_container_bind_only_with_the_optin(monkeypatch, tmp_path):
    import uvicorn
    import executor_service.__main__ as entry
    import executor_service.runner as runner_impl
    from executor_service import readiness as readiness_mod

    class _Ready:
        ready = True
        reasons = ()
        detail = {}
        failed_checks = ()

    monkeypatch.setattr(readiness_mod, "check_readiness", lambda **kw: _Ready())
    monkeypatch.setattr(runner_impl, "sweep_owned_orphans", lambda owner: [])
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv("EXECUTOR_SERVICE_HOST", "0.0.0.0")
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", "k" * 32)
    monkeypatch.setenv("EXECUTOR_SERVICE_NONCE_DB", str(tmp_path / "n.db"))
    monkeypatch.delenv(entry.ALLOW_INTERNAL_BIND_ENV, raising=False)
    assert entry.main() == 2, "0.0.0.0 without the opt-in must be refused"
    monkeypatch.setenv(entry.ALLOW_INTERNAL_BIND_ENV, "1")
    assert entry.main() == 0


# ── REGRESSION: readiness must be evaluated exactly ONCE per start ──


def test_entrypoint_evaluates_readiness_exactly_once(monkeypatch, tmp_path):
    """The entrypoint validated readiness and the app lifecycle then ran the
    FULL probe AGAIN. Two independent evaluations could disagree, leaving the
    service serving /healthz 503 after startup had already been approved."""
    import uvicorn
    import executor_service.__main__ as entry
    import executor_service.runner as runner_impl
    import executor_service.readiness as readiness_mod
    from executor_service.readiness import ServiceReadiness

    calls = []
    result = ServiceReadiness(True, (), {"timings_ms": {}, "probe": {"probe": "ok"}})
    monkeypatch.setattr(readiness_mod, "check_readiness",
                        lambda **kw: (calls.append(1) or result))
    monkeypatch.setattr(runner_impl, "sweep_owned_orphans", lambda owner: [])
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv("EXECUTOR_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", "k" * 32)
    monkeypatch.setenv("EXECUTOR_SERVICE_NONCE_DB", str(tmp_path / "n.db"))
    assert entry.main() == 0
    assert calls == [1], "readiness must be evaluated exactly once per start"


def test_app_reuses_an_injected_readiness_without_a_second_probe(monkeypatch,
                                                                 tmp_path):
    from fastapi.testclient import TestClient
    import executor_service.app as app_mod
    import executor_service.readiness as readiness_mod
    from executor_service.nonce_store import NonceStore
    from executor_service.readiness import ServiceReadiness

    calls = []
    monkeypatch.setattr(readiness_mod, "check_readiness",
                        lambda **kw: (calls.append(1) or ServiceReadiness(True)))
    injected = ServiceReadiness(True, (),
                                {"probe": {"probe": "ok"}, "timings_ms": {}})
    app = app_mod.build_default_app(readiness=injected, key=b"k" * 32,
                                    nonce_store=NonceStore(str(tmp_path / "n.db")))
    res = TestClient(app).get("/healthz")
    assert res.status_code == 200 and res.json()["ready"] is True
    assert calls == [], "an injected result must be reused, never recomputed"


# ── probe scope labelling ───────────────────────────────────────────


def _load_probe():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mp4c_probe_under_test", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_probe_check_is_scope_labelled():
    scopes = _load_probe().CHECK_SCOPES
    assert scopes["every_check_is_scope_labelled"] == "governance"
    service_scoped = {k for k, v in scopes.items() if v == "service"}
    assert service_scoped == {"host_canary_not_readable",
                             "host_env_secret_not_inherited", "canary_removed"}
    assert scopes["docker_socket_absent"] == "workload"
    assert scopes["no_container_left_behind"] == "daemon"


def test_network_scopes_are_earned_not_renamed():
    """The workload check was upgraded from the old placeholder scope ONLY
    because a real paired positive control now exists."""
    scopes = _load_probe().CHECK_SCOPES
    assert scopes["network_tripwire_positive_control_reachable"] == "control"
    assert scopes["network_tripwire_synchronised_and_blocked"] == "workload"
    assert "reduced" not in set(scopes.values()), "no check may hide"


# ── REGRESSION: the diagnosed startup failure ──────────────────────


def test_await_container_exists_polls_instead_of_sleeping():
    """A fixed sleep made the two docker-inspect checks fail whenever the
    daemon needed longer than the wait to create the container -- the observed
    spurious executor_isolation_probe_failed at startup."""
    mod = _load_probe()
    calls = []

    class SlowDocker:
        def __init__(self, succeed_on):
            self.succeed_on = succeed_on

        def __call__(self, args, **kw):
            calls.append(list(args))
            return (0 if len(calls) >= self.succeed_on else 1), "container-id", ""

    assert mod._await_container_exists("c", deadline_s=5.0,
                                       docker=SlowDocker(succeed_on=3)) is True
    assert len(calls) == 3, calls


def test_await_container_exists_fails_closed_after_the_deadline():
    mod = _load_probe()
    calls = []

    def never(args, **kw):
        calls.append(list(args))
        return 1, "", "Error: No such object"

    assert mod._await_container_exists("c", deadline_s=0.4, docker=never) is False
    assert len(calls) >= 1


def test_no_fixed_sleep_remains_before_the_inspect_checks():
    with open(PROBE, encoding="utf-8") as fh:
        src = fh.read()
    assert "_await_container_exists(n8" in src
    assert "time.sleep(2.0)" not in src


# ── both sides of the network control ──────────────────────────────


def test_control_argv_is_harmless_and_uses_stdin():
    mod = _load_probe()
    argv = mod._control_argv("ctl", "somenet", "10.0.0.5", 1234)
    assert mod.svc.DOCKER_STDIN_FLAG in argv, "no stdin flag => control is a no-op"
    assert argv[-5:] == ["python", "-I", "-B", "-u", "-"]
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert argv[argv.index("--network") + 1] == "somenet"
    assert "docker.sock" not in " ".join(argv)
    assert "-c" not in argv
    assert "CTRL_HOST=10.0.0.5" in argv


def test_control_argv_omits_the_network_flag_on_a_bare_host():
    argv = _load_probe()._control_argv("ctl", None, "host.docker.internal", 1234)
    assert "--network" not in argv
    assert "CTRL_HOST=host.docker.internal" in argv


def test_control_program_is_harness_owned_and_only_dials():
    prog = _load_probe().CONTROL_PROGRAM
    assert "CONTROL_REACHED" in prog and "create_connection" in prog
    for forbidden in ("subprocess", "exec(", "eval(", "os.system", "docker.sock"):
        assert forbidden not in prog, forbidden


def test_probe_runs_the_control_before_the_workload_and_resets_hits():
    with open(PROBE, encoding="utf-8") as fh:
        src = fh.read()
    ctrl_at = src.index("network_tripwire_positive_control_reachable")
    reset_at = src.index(chr(104) + chr(105) + chr(116) + chr(115) + chr(91)
                         + chr(34) + chr(110) + chr(34) + chr(93) + " = 0")
    main_at = src.index("main_argv = _hardened(")
    assert ctrl_at < reset_at < main_at, (ctrl_at, reset_at, main_at)


def test_control_target_never_silently_falls_back_when_containerized():
    with open(PROBE, encoding="utf-8") as fh:
        src = fh.read()
    body = src.split("def _control_target", 1)[1].split("def _control_argv", 1)[0]
    assert "if not containerized:" in body
    assert "unproven" in src


def test_probe_reports_whether_it_is_containerized():
    with open(PROBE, encoding="utf-8") as fh:
        src = fh.read()
    assert '"containerized": containerized' in src
    assert "/.dockerenv" in src
