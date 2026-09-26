"""MP-4C policy A — THROWAWAY host-isolation probe against the PYTHON image.

TEST-ONLY, out of band (never the production executor). Uses the digest-pinned
Python image and asserts ACTUAL denied access — never just flags.

FAILS CLOSED with exit code 2 when the pinned image is not provisioned, so a
Node image can never be mistaken for Python proof.

Usage: PYTHONPATH=backend python tests/smoke/mp4c_isolation_probe.py
Exit 0 = every denial observed. 2 = image not provisioned (no proof claimed).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import threading
import uuid

from tests.smoke import mp4c_executor_service as svc
from tests.smoke import mp4c_python_image as image

#: A UNIQUE canary embedded in the source that travels over stdin. If any command
#: metadata, log or error text carried the source, this string would show up.
CODE_CANARY = "MP4C_PROBE_CODE_CANARY_" + uuid.uuid4().hex

#: docker-inspect fields that would expose the command if argv carried the source.
INSPECT_FMT = ("{{json .Config.Cmd}}||{{json .Path}}||{{json .Args}}||"
               "{{json .Config.Env}}||{{json .Config.Labels}}||{{.Config.OpenStdin}}")
INSPECT_SEP = "||"

#: ── HONEST SCOPE LABELLING ────────────────────────────────────────────────
#: Each check is labelled with what it actually observes. The probe can run on
#: the bare host or INSIDE the service container; labels must stay true in both.
#:   workload   — the executed Python container (its uid, mounts, cgroup limits,
#:                argv/cmdline, stdio result). True wherever the probe runs.
#:   daemon     — the Docker daemon's own view (container list, docker inspect).
#:   contract   — the fixed broker argv/plan, purely local to the probe process.
#:   service    — ONLY the filesystem/env of whatever container runs this probe.
#:                It is NOT a statement about the physical Docker host.
#:   control    — the harness proving ANOTHER side of a test is real (e.g. that a
#:                listener is genuinely reachable by a networked container).
#:   reduced    — passes in both modes, but has MATERIALLY less discriminating
#:                power in some mode. Currently UNUSED: the only former member was
#:                upgraded to `workload` because the new positive control EARNS
#:                it (the upgrade is not a rename).
#:   governance — the labelling invariant itself.
CHECK_SCOPES = {
    "python_container_ran": "workload",
    "non_root_uid": "workload",
    "root_filesystem_read_only": "workload",
    "etc_write_denied": "workload",
    "isolated_scratch_writable": "workload",
    "scratch_noexec": "workload",
    # The canary is created next to THIS probe. Containerized, that is the service
    # container's filesystem, not the physical host's. It still proves the code
    # container cannot see the service container — the deployment-relevant claim.
    "host_canary_not_readable": "service",
    "docker_socket_absent": "workload",
    # The secret is placed in THIS process's environment (the service container).
    "host_env_secret_not_inherited": "service",
    "no_host_mounts_visible": "workload",
    # Upgraded from `reduced` because the paired POSITIVE CONTROL now proves the
    # listener is reachable at THIS target by a container that has a network in
    # this deployment, while the otherwise equivalent `--network none` workload
    # completes its attempt to the SAME host:port with ZERO listener hits. The
    # upgrade is earned by that control, not by renaming the check.
    "network_tripwire_synchronised_and_blocked": "workload",
    "network_tripwire_positive_control_reachable": "control",
    "pids_limit_enforced": "workload",
    "memory_limit_enforced": "workload",
    "capabilities_dropped": "workload",
    "nofile_ulimit_enforced": "workload",
    "cpu_quota_enforced": "workload",
    "python_success_runs": "workload",
    "python_failure_nonzero_and_raw_output_present": "workload",
    "failure_sanitised_report_excludes_child_output": "workload",
    "oom_classified_as_kill": "workload",
    "timeout_killed_and_cleaned": "workload",
    "probe_argv_carries_no_python_source": "workload",
    "broker_argv_has_no_source_slot": "contract",
    "code_canary_absent_from_container_process_cmdline": "workload",
    "docker_inspect_command_fields_carry_no_source": "daemon",
    "docker_inspect_confirms_stdin_forwarding": "daemon",
    "broker_records_repr_and_argv_carry_no_source": "contract",
    "broker_sanitised_error_text_carries_no_source": "contract",
    "exact_code_executed_once_over_stdin": "workload",
    "no_container_left_behind": "daemon",
    # Containerized, this is the service container's /tmp.
    "canary_removed": "service",
    "every_check_is_scope_labelled": "governance",
}

HOST_SECRET_NAME = "MP4C_HOST_SECRET"
HOST_SECRET_VALUE = "CANARY-HOST-SECRET-MUST-NOT-LEAK"


def _is_containerized() -> bool:
    """True when this probe runs inside a container (Docker creates /.dockerenv)."""
    return os.path.exists("/.dockerenv")


#: Recorded in PROBE_RESULT so a reader always knows WHICH environment the
#: "service"-scoped checks describe.
containerized = _is_containerized()

ATTACK = "\n".join([
    "import json, os, socket, subprocess",
    "out = {}",
    "out[chr(117)+chr(105)+chr(100)] = os.getuid()",
    "def _try(path, mode=chr(119)):",
    "    try:",
    "        open(path, mode).write(chr(120)); return chr(97)+chr(108)+chr(108)+chr(111)+chr(119)+chr(101)+chr(100)",
    "    except Exception:",
    "        return chr(100)+chr(101)+chr(110)+chr(105)+chr(101)+chr(100)",
    "out[chr(114)+chr(111)+chr(111)+chr(116)+chr(95)+chr(119)+chr(114)+chr(105)+chr(116)+chr(101)] = _try(chr(47)+chr(109)+chr(112)+chr(52)+chr(99)+chr(95)+chr(114)+chr(111)+chr(111)+chr(116)+chr(95)+chr(119)+chr(114)+chr(105)+chr(116)+chr(101))",
    "out[chr(101)+chr(116)+chr(99)+chr(95)+chr(119)+chr(114)+chr(105)+chr(116)+chr(101)] = _try(chr(47)+chr(101)+chr(116)+chr(99)+chr(47)+chr(109)+chr(112)+chr(52)+chr(99))",
    "out[chr(115)+chr(99)+chr(114)+chr(97)+chr(116)+chr(99)+chr(104)+chr(95)+chr(119)+chr(114)+chr(105)+chr(116)+chr(101)] = _try(chr(47)+chr(115)+chr(99)+chr(114)+chr(97)+chr(116)+chr(99)+chr(104)+chr(47)+chr(111)+chr(107))",
    "try:",
    "    p = chr(47)+chr(115)+chr(99)+chr(114)+chr(97)+chr(116)+chr(99)+chr(104)+chr(47)+chr(120)+chr(46)+chr(115)+chr(104)",
    "    open(p, chr(119)).write(chr(35)+chr(33)+chr(47)+chr(98)+chr(105)+chr(110)+chr(47)+chr(115)+chr(104)+chr(10)+chr(101)+chr(99)+chr(104)+chr(111)+chr(32)+chr(120)+chr(10))",
    "    os.chmod(p, 0o755)",
    "    subprocess.run([p], check=True, capture_output=True)",
    "    out[chr(115)+chr(99)+chr(114)+chr(97)+chr(116)+chr(99)+chr(104)+chr(95)+chr(101)+chr(120)+chr(101)+chr(99)] = chr(97)+chr(108)+chr(108)+chr(111)+chr(119)+chr(101)+chr(100)",
    "except Exception:",
    "    out[chr(115)+chr(99)+chr(114)+chr(97)+chr(116)+chr(99)+chr(104)+chr(95)+chr(101)+chr(120)+chr(101)+chr(99)] = chr(100)+chr(101)+chr(110)+chr(105)+chr(101)+chr(100)",
    "out[chr(99)+chr(97)+chr(110)+chr(97)+chr(114)+chr(121)+chr(95)+chr(114)+chr(101)+chr(97)+chr(100)] = chr(80)+chr(82)+chr(69)+chr(83)+chr(69)+chr(78)+chr(84) if os.path.exists(os.environ.get(chr(67)+chr(65)+chr(78)+chr(65)+chr(82)+chr(89)+chr(95)+chr(80)+chr(65)+chr(84)+chr(72), chr(47)+chr(110))) else chr(97)+chr(98)+chr(115)+chr(101)+chr(110)+chr(116)",
    "out[chr(100)+chr(111)+chr(99)+chr(107)+chr(101)+chr(114)+chr(95)+chr(115)+chr(111)+chr(99)+chr(107)] = chr(80)+chr(82)+chr(69)+chr(83)+chr(69)+chr(78)+chr(84) if os.path.exists(chr(47)+chr(118)+chr(97)+chr(114)+chr(47)+chr(114)+chr(117)+chr(110)+chr(47)+chr(100)+chr(111)+chr(99)+chr(107)+chr(101)+chr(114)+chr(46)+chr(115)+chr(111)+chr(99)+chr(107)) else chr(97)+chr(98)+chr(115)+chr(101)+chr(110)+chr(116)",
    "out[chr(104)+chr(111)+chr(115)+chr(116)+chr(95)+chr(115)+chr(101)+chr(99)+chr(114)+chr(101)+chr(116)+chr(95)+chr(108)+chr(101)+chr(97)+chr(107)] = sum(1 for k in os.environ if chr(77)+chr(80)+chr(52)+chr(67)+chr(95)+chr(72) in k)",
    "try:",
    "    mi = open(chr(47)+chr(112)+chr(114)+chr(111)+chr(99)+chr(47)+chr(115)+chr(101)+chr(108)+chr(102)+chr(47)+chr(109)+chr(111)+chr(117)+chr(110)+chr(116)+chr(105)+chr(110)+chr(102)+chr(111)).read()",
    "except Exception:",
    "    mi = \"\"",
    "out[chr(104)+chr(111)+chr(115)+chr(116)+chr(109)+chr(111)+chr(117)+chr(110)+chr(116)] = sum(1 for l in mi.splitlines() if chr(47)+chr(109)+chr(110)+chr(116)+chr(47)+chr(104)+chr(111)+chr(115)+chr(116) in l)",
    "try:",
    "    s = socket.create_connection((os.environ[chr(84)+chr(82)+chr(73)+chr(80)+chr(87)+chr(73)+chr(82)+chr(69)+chr(95)+chr(72)+chr(79)+chr(83)+chr(84)], int(os.environ[chr(84)+chr(82)+chr(73)+chr(80)+chr(87)+chr(73)+chr(82)+chr(69)+chr(95)+chr(80)+chr(79)+chr(82)+chr(84)])), timeout=4)",
    "    s.close(); out[chr(116)+chr(114)+chr(105)+chr(112)+chr(119)+chr(105)+chr(114)+chr(101)] = chr(82)+chr(69)+chr(65)+chr(67)+chr(72)+chr(65)+chr(66)+chr(76)+chr(69)",
    "except Exception:",
    "    out[chr(116)+chr(114)+chr(105)+chr(112)+chr(119)+chr(105)+chr(114)+chr(101)] = chr(98)+chr(108)+chr(111)+chr(99)+chr(107)+chr(101)+chr(100)",
    "out[chr(116)+chr(114)+chr(105)+chr(112)+chr(119)+chr(105)+chr(114)+chr(101)+chr(95)+chr(97)+chr(116)+chr(116)+chr(101)+chr(109)+chr(112)+chr(116)] = chr(100)+chr(111)+chr(110)+chr(101)",
    "def _cat(p):",
    "    try: return open(p).read().strip()",
    "    except Exception: return chr(117)+chr(110)+chr(107)+chr(110)+chr(111)+chr(119)+chr(110)",
    "out[chr(112)+chr(105)+chr(100)+chr(115)+chr(95)+chr(109)+chr(97)+chr(120)] = _cat(chr(47)+chr(115)+chr(121)+chr(115)+chr(47)+chr(102)+chr(115)+chr(47)+chr(99)+chr(103)+chr(114)+chr(111)+chr(117)+chr(112)+chr(47)+chr(112)+chr(105)+chr(100)+chr(115)+chr(46)+chr(109)+chr(97)+chr(120))",
    "out[chr(109)+chr(101)+chr(109)+chr(95)+chr(109)+chr(97)+chr(120)] = _cat(chr(47)+chr(115)+chr(121)+chr(115)+chr(47)+chr(102)+chr(115)+chr(47)+chr(99)+chr(103)+chr(114)+chr(111)+chr(117)+chr(112)+chr(47)+chr(109)+chr(101)+chr(109)+chr(111)+chr(114)+chr(121)+chr(46)+chr(109)+chr(97)+chr(120))",
    "try:",
    "    st = open(chr(47)+chr(112)+chr(114)+chr(111)+chr(99)+chr(47)+chr(115)+chr(101)+chr(108)+chr(102)+chr(47)+chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115)).read()",
    "    out[chr(99)+chr(97)+chr(112)+chr(95)+chr(101)+chr(102)+chr(102)] = [l for l in st.splitlines() if l.startswith(chr(67)+chr(97)+chr(112)+chr(69)+chr(102)+chr(102))][0]",
    "except Exception:",
    "    out[chr(99)+chr(97)+chr(112)+chr(95)+chr(101)+chr(102)+chr(102)] = chr(117)+chr(110)+chr(107)+chr(110)+chr(111)+chr(119)+chr(110)",
    "out[chr(108)+chr(105)+chr(109)+chr(105)+chr(116)+chr(115)] = _cat(chr(47)+chr(112)+chr(114)+chr(111)+chr(99)+chr(47)+chr(115)+chr(101)+chr(108)+chr(102)+chr(47)+chr(108)+chr(105)+chr(109)+chr(105)+chr(116)+chr(115))",
    "out[chr(99)+chr(112)+chr(117)+chr(109)] = _cat(chr(47)+chr(115)+chr(121)+chr(115)+chr(47)+chr(102)+chr(115)+chr(47)+chr(99)+chr(103)+chr(114)+chr(111)+chr(117)+chr(112)+chr(47)+chr(99)+chr(112)+chr(117)+chr(46)+chr(109)+chr(97)+chr(120))",
    "out[chr(99)+chr(109)+chr(100)+chr(49)] = _cat(chr(47)+chr(112)+chr(114)+chr(111)+chr(99)+chr(47)+chr(49)+chr(47)+chr(99)+chr(109)+chr(100)+chr(108)+chr(105)+chr(110)+chr(101))",
    "out[chr(99)+chr(109)+chr(100)+chr(115)] = _cat(chr(47)+chr(112)+chr(114)+chr(111)+chr(99)+chr(47)+chr(115)+chr(101)+chr(108)+chr(102)+chr(47)+chr(99)+chr(109)+chr(100)+chr(108)+chr(105)+chr(110)+chr(101))",
    "# " + CODE_CANARY,
    "print(chr(80)+chr(82)+chr(79)+chr(66)+chr(69)+chr(95)+chr(74)+chr(83)+chr(79)+chr(78)+chr(32) + json.dumps(out))",
])


#: FIXED, harness-owned positive control. NOT model-supplied code: it only dials
#: the probe's own listener and reports whether the connection was accepted.
CONTROL_PROGRAM = "\n".join([
    "import os, socket, sys",
    "try:",
    "    s = socket.create_connection((os.environ['CTRL_HOST'], int(os.environ['CTRL_PORT'])), timeout=10)",
    "    s.close()",
    "    print('CONTROL_REACHED')",
    "except Exception as exc:",
    "    print('CONTROL_FAILED ' + type(exc).__name__)",
    "    sys.exit(3)",
])


def _await_container_exists(name, *, deadline_s=30.0, docker=None):
    """Bounded poll until `name` exists; True as soon as it does.

    REPLACES a fixed sleep. With a fixed wait, the two `docker inspect` checks
    failed whenever the daemon needed longer than the wait to create the
    container (observed under load) and surfaced as a spurious
    `executor_isolation_probe_failed` at startup. Polling removes the timing
    assumption instead of hiding it.
    """
    fn = docker or _docker
    deadline = time.monotonic() + float(deadline_s)
    while True:
        try:
            rc, _out, _err = fn(["inspect", name, "--format", "{{.Id}}"], timeout=15)
        except Exception:  # noqa: BLE001
            rc = 1
        if rc == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _control_target():
    """(network, host) the control must use to reach OUR listener, else (None, None).

    Containerized, the listener lives INSIDE this container, so a control must
    join one of this container's own networks and dial its IP:
    `host.docker.internal` names the Docker HOST and can never reach it. On a bare
    host the listener IS on the host, so `host.docker.internal` over the default
    bridge is correct.
    """
    if not containerized:
        return None, "host.docker.internal"
    try:
        me = socket.gethostname()
    except Exception:  # noqa: BLE001
        return None, None
    rc, out, _err = _docker(["inspect", me, "--format",
                             "{{json .NetworkSettings.Networks}}"])
    if rc != 0 or not out.strip():
        return None, None
    try:
        nets = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, None
    if not isinstance(nets, dict):
        return None, None
    for net_name in sorted(nets):
        ip = (nets.get(net_name) or {}).get("IPAddress") or ""
        if ip:
            return net_name, ip
    return None, None


def _control_argv(name, net, host, port):
    """Harmless control container: no socket, no caps, read-only, fixed program."""
    argv = ["docker", "run", "--rm", "--name", name, "--read-only",
            svc.DOCKER_STDIN_FLAG,          # the program arrives on stdin, as always
            "--user", "65534:65534", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "32"]
    if net:
        argv += ["--network", net]
    argv += ["-e", "CTRL_HOST=" + host, "-e", "CTRL_PORT=" + str(port)]
    argv += [image.PINNED_REF] + list(svc.PYTHON_ARGV)
    return argv


def _docker(args, *, timeout=60):
    """Read-only docker metadata call (no stdin involved)."""
    p = subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _run(argv, program, *, timeout=90, name=None):
    """Run `argv` with `program` written to STDIN as EXACT UTF-8 bytes.

    Binary mode is deliberate: text mode would translate newlines, and this probe
    must prove the exact bytes of the source are what the container executes.
    """
    try:
        p = subprocess.run(argv, input=program.encode("utf-8"),
                           capture_output=True, timeout=timeout)
        return (p.returncode,
                (p.stdout or b"").decode("utf-8", "replace"),
                (p.stderr or b"").decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        return "TIMEOUT", "", ""


def _tripwire():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("0.0.0.0", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    hits = {"n": 0}

    def _serve():
        srv.settimeout(25)
        try:
            while True:
                c, _ = srv.accept(); hits["n"] += 1; c.close()
        except Exception:
            pass

    threading.Thread(target=_serve, daemon=True).start()
    return srv, port, hits


def _hardened(name, port, canary_path, trip_host, *, extra=None):
    """FIXED hardened argv. NO code: the source arrives on stdin (`-` + `--interactive`)."""
    argv = [
        "docker", "run", "--rm", "--name", name,
        svc.DOCKER_STDIN_FLAG,
        "--network", "none", "--read-only",
        "--tmpfs", "/scratch:rw,noexec,nosuid,nodev,size=16m",
        "--user", "65534:65534", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "64", "--memory", "128m", "--memory-swap", "128m",
        "--cpus", "0.5", "--ulimit", "nofile=64:64",
        "-e", "CANARY_PATH=" + canary_path, "-e", "TRIPWIRE_PORT=" + str(port),
        "-e", "TRIPWIRE_HOST=" + trip_host,
    ]
    argv += list(extra or [])
    argv += [image.PINNED_REF] + list(svc.PYTHON_ARGV)
    return argv


def main() -> int:
    ok, reason = image.readiness()
    if not ok:
        note = "run the AUTHORIZED provisioning procedure in tests/smoke/mp4c_python_image.py first"
        print("PROBE_RESULT " + json.dumps({
            "ok": False, "reason": reason,
            "python_container_proof": "NOT ATTEMPTED",
            "note": note, "pinned": image.PINNED_REF,
        }))
        return 2

    checks = []

    def ck(n, good, seen):
        checks.append((n, bool(good), seen))

    root = tempfile.mkdtemp(prefix="mp4c_iso_")
    canary = os.path.join(root, "mp4c_host_canary.txt")
    with open(canary, "w", encoding="utf-8") as fh:
        fh.write("HOST-ONLY-CANARY")
    srv, port, hits = _tripwire()
    name = "mp4c_probe_" + uuid.uuid4().hex[:10]
    os.environ[HOST_SECRET_NAME] = HOST_SECRET_VALUE
    try:
        # ── POSITIVE CONTROL FIRST ────────────────────────────────────────
        # Without this, "blocked" is vacuous: an unreachable or unbound listener
        # also yields blocked. The control proves the listener accepts a
        # connection from a container that HAS a network in this deployment.
        ctrl_net, trip_host = _control_target()
        if not trip_host:
            ck("network_tripwire_positive_control_reachable", False,
               {"unproven": "no reachable control target could be established",
                "containerized": containerized})
            trip_host = "host.docker.internal"
        else:
            cname = "mp4c_probe_ctrl_" + uuid.uuid4().hex[:8]
            c_rc, c_out, c_err = _run(
                _control_argv(cname, ctrl_net, trip_host, port), CONTROL_PROGRAM)
            ck("network_tripwire_positive_control_reachable",
               c_rc == 0 and "CONTROL_REACHED" in c_out and hits["n"] >= 1,
               {"rc": c_rc, "reached": "CONTROL_REACHED" in c_out,
                "control_hits": hits["n"], "target": trip_host,
                "network": ctrl_net or "default",
                "containerized": containerized, "err": (c_err or "")[:160]})
        # Reset so the isolation side is measured INDEPENDENTLY of the control.
        hits["n"] = 0

        main_argv = _hardened(name, port, canary, trip_host)
        rc, out, err = _run(main_argv, ATTACK)
        obs = {}
        for line in out.splitlines():
            if line.startswith("PROBE_JSON "):
                obs = json.loads(line[len("PROBE_JSON "):])
        ck("python_container_ran", rc == 0 and bool(obs), {"rc": rc, "err": err[:200]})
        ck("non_root_uid", obs.get("uid") not in (None, 0), obs.get("uid"))
        ck("root_filesystem_read_only", obs.get("root_write") == "denied", obs.get("root_write"))
        ck("etc_write_denied", obs.get("etc_write") == "denied", obs.get("etc_write"))
        ck("isolated_scratch_writable", obs.get("scratch_write") == "allowed", obs.get("scratch_write"))
        ck("scratch_noexec", obs.get("scratch_exec") == "denied", obs.get("scratch_exec"))
        ck("host_canary_not_readable", obs.get("canary_read") == "absent", obs.get("canary_read"))
        ck("docker_socket_absent", obs.get("docker_sock") == "absent", obs.get("docker_sock"))
        ck("host_env_secret_not_inherited", obs.get("host_secret_leak") == 0, obs.get("host_secret_leak"))
        ck("no_host_mounts_visible", obs.get("hostmount") == 0, obs.get("hostmount"))
        ck("network_tripwire_synchronised_and_blocked",
           obs.get("tripwire_attempt") == "done" and obs.get("tripwire") == "blocked"
           and hits["n"] == 0,
           {"attempt": obs.get("tripwire_attempt"), "container": obs.get("tripwire"),
            "listener_hits": hits["n"], "target": trip_host,
            "containerized": containerized})
        ck("pids_limit_enforced", str(obs.get("pids_max")) == "64", obs.get("pids_max"))
        ck("memory_limit_enforced", obs.get("mem_max") == "134217728", obs.get("mem_max"))
        cap = obs.get("cap_eff") or ""
        hx = cap.split(":")[-1].strip().lower() if ":" in cap else cap.strip().lower()
        ck("capabilities_dropped", bool(hx) and set(hx) <= {"0"}, cap)
        lim = obs.get("limits") or ""
        nofile = " ".join(next((l.split()[3:5] for l in lim.splitlines()
                                if l.startswith("Max open files")), []))
        ck("nofile_ulimit_enforced", nofile == "64 64", {"nofile": nofile})
        ck("cpu_quota_enforced", obs.get("cpum") == "50000 100000", obs.get("cpum"))

        n2 = "mp4c_probe_ok_" + uuid.uuid4().hex[:8]
        rc2, out2, _ = _run(_hardened(n2, port, canary, trip_host),
                            "print(chr(80)+chr(89)+chr(45)+chr(79)+chr(75))")
        ck("python_success_runs", rc2 == 0 and "PY-OK" in out2, {"rc": rc2})

        n3 = "mp4c_probe_fail_" + uuid.uuid4().hex[:8]
        failprog = "raise ValueError(chr(82)+chr(65)+chr(87))"
        rc3, out3, err3 = _run(_hardened(n3, port, canary, trip_host), failprog)
        ck("python_failure_nonzero_and_raw_output_present", rc3 != 0
           and ("ValueError" in (err3 or "") or "ValueError" in (out3 or "")),
           {"rc": rc3, "stderr_bytes": len(err3 or ""),
            "note": "service MUST discard child output on failure (r2b1 policy)"})

        n7 = "mp4c_probe_san_" + uuid.uuid4().hex[:8]
        rawprog = "\n".join([
            "import sys",
            "sys.stderr.write(chr(82)+chr(65)+chr(87)+chr(45)+chr(67)+chr(72)+chr(73)+chr(76)+chr(68)+chr(10))",
            "raise ValueError(chr(82)+chr(65)+chr(87)+chr(45)+chr(86))",
        ])
        rc7, _o7, _e7 = _run(_hardened(n7, port, canary, trip_host), rawprog)
        # SANITISED FAILURE. The production failure path
        # (backend/app/tools/python_sandbox.py:370) reports ONLY the exit code plus the
        # bounded classification slug and discards EVERY byte of child output.
        reported = "[Python Sandbox] Execution failed with exit code %s. (%s)" % (
            rc7, "runtime_error")
        ck("failure_sanitised_report_excludes_child_output",
           rc7 != 0 and "RAW-CHILD" in (_e7 or "") and "RAW-V" in (_e7 or "")
           and "RAW-CHILD" not in reported and "RAW-V" not in reported
           and "ValueError" not in reported and "Traceback" not in reported,
           {"rc": rc7, "raw_child_bytes": len(_e7 or ""), "reported": reported})

        n4 = "mp4c_probe_oom_" + uuid.uuid4().hex[:8]
        oomprog = "a=[]\nwhile True:\n    a.append(bytearray(10**7))"
        rc4, _o4, _e4 = _run(_hardened(n4, port, canary, trip_host), oomprog)
        # STRENGTHENED: 137 == SIGKILL from the cgroup OOM killer. An interpreter-
        # level refusal is accepted ONLY when it is explicitly MemoryError. A bare
        # rc 1 (bad flag, spawn failure, missing binary) and a TIMEOUT (cap NOT
        # enforced) must NOT be counted as "the memory cap worked".
        oom_ok = rc4 == 137 or (rc4 == 1 and "MemoryError" in (_e4 or ""))
        ck("oom_classified_as_kill", oom_ok,
           {"rc": rc4,
            "kind": "sigkill" if rc4 == 137 else ("memory_error" if oom_ok else "unclassified"),
            "stderr_tail": (_e4 or "")[-200:]})

        n5 = "mp4c_probe_to_" + uuid.uuid4().hex[:8]
        rc5, _o5, _e5 = _run(_hardened(n5, port, canary, trip_host),
                             "import time; time.sleep(60)", timeout=8, name=n5)
        ck("timeout_killed_and_cleaned", rc5 == "TIMEOUT", {"rc": rc5})

        # ── disclosure boundary: Python source must not reach command metadata ──
        ck("probe_argv_carries_no_python_source",
           CODE_CANARY not in " ".join(main_argv) and "-c" not in main_argv,
           {"argv_tail": list(main_argv[-5:])})

        meta_argv = svc.build_argv(name="mp4c_probe_meta")
        ck("broker_argv_has_no_source_slot",
           "-c" not in meta_argv and "-e" not in meta_argv
           and svc.DOCKER_STDIN_FLAG in meta_argv
           and list(meta_argv[-5:]) == ["python", "-I", "-B", "-u", "-"]
           and not any(CODE_CANARY in x for x in meta_argv),
           {"argv_tail": list(meta_argv[-5:])})

        cmdline = (obs.get("cmd1", "") + " " + obs.get("cmds", "")).replace("\x00", " ")
        ck("code_canary_absent_from_container_process_cmdline",
           bool(cmdline.strip()) and CODE_CANARY not in cmdline and "python" in cmdline,
           {"cmd1": obs.get("cmd1", ""), "cmds": obs.get("cmds", "")})

        # ── live container: docker inspect command/config fields ──
        n8 = "mp4c_probe_insp_" + uuid.uuid4().hex[:8]
        insp_argv = svc.build_argv(name=n8)
        insp_code = "# " + CODE_CANARY + "\nimport time\ntime.sleep(45)\n"
        proc = subprocess.Popen(insp_argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdin.write(insp_code.encode("utf-8"))
            proc.stdin.flush()   # keep stdin OPEN: python blocks until EOF, container lives
            # BOUNDED POLL, not a fixed sleep: a fixed wait is a timing assumption
            # that fails whenever the daemon is slower than the wait (observed
            # under load), producing a spurious probe failure.
            appeared = _await_container_exists(n8, deadline_s=30.0)
            rc_i, out_i, err_i = _docker(["inspect", n8, "--format", INSPECT_FMT])
            parts = out_i.strip().split(INSPECT_SEP) if out_i.strip() else []
            ck("docker_inspect_command_fields_carry_no_source",
               appeared and rc_i == 0 and parts and CODE_CANARY not in out_i
               and parts[0] == '["python","-I","-B","-u","-"]',
               {"rc": rc_i, "appeared": appeared, "cmd": parts[0] if parts else "",
                "canary_in_fields": CODE_CANARY in out_i, "err": (err_i or "")[:120]})
            ck("docker_inspect_confirms_stdin_forwarding",
               appeared and len(parts) > 5 and parts[5].strip() == "true",
               {"appeared": appeared,
                "open_stdin": parts[5] if len(parts) > 5 else "",
                "path": parts[1] if len(parts) > 1 else "",
                "args": parts[2] if len(parts) > 2 else ""})
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=60)
            except Exception:
                proc.kill()
            _docker(["rm", "-f", n8])

        # ── broker-driven evidence, over real containers and real stdin ──
        tk = "mp4c-probe-executor-token"
        b = svc.ExecutorService(token=tk)
        canary_code = "raise ValueError('" + CODE_CANARY + "')"
        seen_plans = []
        b.execute(svc.ExecutionRequest(code=canary_code, user_id=1, run_id="probe-ok", token=tk),
                  runner=lambda plan: (seen_plans.append(plan)
                                       or svc.ExecutionResult(ok=True, reason=None)))
        plan0 = seen_plans[0]
        blob = (repr(plan0) + json.dumps(b.started) + json.dumps(b.rejections)
                + " ".join(plan0.argv))
        ck("broker_records_repr_and_argv_carry_no_source",
           CODE_CANARY not in blob and "withheld" in repr(plan0)
           and CODE_CANARY in plan0.stdin_code.decode("utf-8"),
           {"argv_tail": list(plan0.argv[-5:]), "stdin_bytes": len(plan0.stdin_code)})

        def _sanitising_runner(plan):
            rc_b, out_b, _e_b = _run(list(plan.argv), plan.stdin_code.decode("utf-8"))
            if rc_b != 0:
                return svc.ExecutionResult(
                    ok=False, reason="runtime_error", is_error=True,
                    text=("[Python Sandbox] Execution failed with exit code %s."
                          " (runtime_error)") % rc_b)
            return svc.ExecutionResult(ok=True, reason=None, text=out_b, is_error=False)

        res_fail = b.execute(
            svc.ExecutionRequest(code=canary_code, user_id=2, run_id="probe-fail", token=tk),
            runner=_sanitising_runner)
        ck("broker_sanitised_error_text_carries_no_source",
           res_fail.ok is False and CODE_CANARY not in res_fail.text
           and "ValueError" not in res_fail.text,
           {"text": res_fail.text})

        once_marker = "MP4C_RUN_ONCE_" + uuid.uuid4().hex
        res_once = b.execute(
            svc.ExecutionRequest(code="print('%s')" % once_marker,
                                 user_id=3, run_id="probe-once", token=tk),
            runner=_sanitising_runner)
        ck("exact_code_executed_once_over_stdin",
           res_once.ok is True and res_once.text.count(once_marker) == 1
           and res_once.text.strip() == once_marker,
           {"ok": res_once.ok, "hits": res_once.text.count(once_marker),
            "text": res_once.text[:80]})
    finally:
        _docker(["rm", "-f", name])
        # `--rm` removal is daemon-side; poll briefly (bounded) so a slow removal is
        # not misreported as a leak. Still FAIL-CLOSED if a container truly lingers.
        leftovers = ""
        for _ in range(10):
            rc6, out6, _ = _docker(["ps", "-a", "--filter",
                                    "name=mp4c_probe_", "--format", "{{.Names}}"])
            leftovers = "\n".join(l for l in out6.splitlines() if l.strip())
            if not leftovers:
                break
            time.sleep(0.5)
        ck("no_container_left_behind", not leftovers, leftovers)
        srv.close()
        try:
            os.remove(canary); os.rmdir(root)
        except OSError:
            pass
        os.environ.pop(HOST_SECRET_NAME, None)
        ck("canary_removed", not os.path.exists(canary), canary)

    # Fail-closed governance: a check added without a scope label is reported.
    unlabelled = sorted({n for n, _g, _s in checks if n not in CHECK_SCOPES})
    ck("every_check_is_scope_labelled", not unlabelled, unlabelled)

    scopes = {}
    for n, good, seen in checks:
        scope = CHECK_SCOPES.get(n, "unlabelled")
        scopes[scope] = scopes.get(scope, 0) + 1
        print(("PASS " if good else "FAIL ") + n + " [" + scope + "] :: "
              + json.dumps(seen, default=str))
    failed = [n for n, good, _ in checks if not good]
    print("PROBE_RESULT " + json.dumps({"ok": not failed, "failed": failed,
                                        "checks": len(checks), "scopes": scopes,
                                        "containerized": containerized,
                                        "image": image.PINNED_REF}))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
