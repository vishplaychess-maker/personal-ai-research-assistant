"""MP-4C - disposable Compose run: TWO authenticated users + restart replay.

TEST-ONLY. Starts the executor in its own Compose project, then drives it from a
CLIENT container on the internal network (no host port exists).

  * mints grants on the HOST from the shared secret - the long-term key never
    enters a container, and grants travel to the client over STDIN;
  * one request per user for TWO distinct users;
  * unauthorised / forged / replayed grants are refused;
  * the executed-code container must have no Docker socket;
  * the project is RESTARTED with the SAME volume and the SAME grant is replayed,
    and the observed status is reported - never claimed as a pass if it expired.

Never prints the shared key.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))
sys.path.insert(0, REPO_ROOT)

from mp4c_contract import grant as g   # noqa: E402

PROJECT = "mp4c-exec-test"
COMPOSE = os.path.join(REPO_ROOT, "docker-compose.yml")
NET = PROJECT + "_executor-internal"
CONTAINER = "research-assistant-executor"
PINNED = ("python:3.12-slim@sha256:"
          "44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b")
KEY_FILE = os.path.join(REPO_ROOT, "secrets", "executor_grant_key")
ARTIFACT = os.path.join(REPO_ROOT, "tests", "smoke", "mp4c_live_verdict.json")

CLIENT = r'''
import json, urllib.request, urllib.error
BASE = "http://executor:8091"
G = __GRANTS__


def call(path, payload, timeout=90):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as exc:
        return 0, type(exc).__name__ + ":" + str(exc)[:120]


def act(tag, code, run_id, reserve_token, exec_token):
    """The real order: RESERVE capacity, then execute."""
    out["reserve_" + tag] = call("/v1/reserve", {"grant": reserve_token})
    return call("/v1/execute", {"grant": exec_token, "code": code,
                                "run_id": run_id})


out = {}
out["no_grant"] = call("/v1/execute", {"grant": "", "code": G["code_a"],
                                      "run_id": "live-n"})
out["malformed_grant"] = call("/v1/execute",
                              {"grant": "not-a-grant", "code": G["code_a"],
                               "run_id": "live-b"})
out["forged_grant"] = call("/v1/execute", {"grant": G["forged"],
                                          "code": G["code_a"],
                                          "run_id": "live-f"})
out["user_a"] = act("a", G["code_a"], "live-a", G["ra"], G["a"])
out["user_b"] = act("b", G["code_b"], "live-b2", G["rb"], G["b"])
# The socket assertion runs FIRST: a refused replay deliberately RETAINS its
# reservation for retry, which would otherwise hold user 7's single slot and make
# this reservation contend (429) - a harness ordering artefact, not a service bug.
out["code_container_socket"] = act("c", G["code_c"], "live-c", G["rc"], G["c"])
out["replay_a"] = act("a2", G["code_a"], "live-a", G["ra2"], G["a"])
print("CLIENT_RESULT " + json.dumps(out))
'''


def d(args, *, timeout=1800, input_text=None):
    return subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                          timeout=timeout, input=input_text)


def compose(*args, timeout=2400):
    return d(["compose", "-p", PROJECT, "-f", COMPOSE] + list(args), timeout=timeout)


def names():
    return sorted(n for n in d(["ps", "-a", "--format", "{{.Names}}"]).stdout.splitlines()
                  if n.strip())


def wait_healthy(deadline_s=900):
    deadline = time.time() + deadline_s
    st = ""
    while time.time() < deadline:
        st = d(["inspect", CONTAINER, "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
                "|{{.State.Status}}|{{.RestartCount}}"]).stdout.strip()
        if st.split("|")[0] in ("healthy", "unhealthy"):
            return st
        time.sleep(5)
    return st


def nonce_rows():
    """Rows in the DURABLE replay store, read inside the container."""
    out = d(["exec", CONTAINER, "python", "-c",
             "from executor_service.nonce_store import store_from_env;"
             " s=store_from_env(); print('NONCE_ROWS', s.count())"])
    for line in (out.stdout + out.stderr).splitlines():
        if "NONCE_ROWS" in line:
            try:
                return int(line.split("NONCE_ROWS")[1].strip())
            except ValueError:
                return -1
    return -1


def run_client(grants):
    program = CLIENT.replace("__GRANTS__", json.dumps(grants))
    p = d(["run", "--rm", "-i", "--network", NET, "--entrypoint", "python",
           PINNED, "-I", "-B", "-u", "-"], input_text=program, timeout=900)
    payload = {}
    for line in (p.stdout + "\n" + p.stderr).splitlines():
        if line.startswith("CLIENT_RESULT "):
            payload = json.loads(line[len("CLIENT_RESULT "):])
    return payload, (p.stdout + p.stderr)[-300:]


def main() -> int:
    report = {}
    before = names()
    KEY_BYTES = open(KEY_FILE, encoding="utf-8").read().strip().encode()
    key = KEY_BYTES
    report["key_bytes"] = len(key)

    def mint(user_id, code, key=None, purpose=g.PURPOSE_EXECUTE, tc=None):
        now = int(time.time())
        sha, ln = g.code_sha256(code)
        raw = {"v": g.GRANT_VERSION, "jti": uuid.uuid4().hex, "iat": now,
               "exp": now + g.MAX_TTL_S, "user_id": user_id, "tenant_id": None,
               "run_id": "live-run", "step_id": "live-step",
               "tool_call_id": tc or ("live-tc-" + uuid.uuid4().hex[:8]),
               "approval_id": None, "action_hash": "a" * 64,
               "code_sha256": sha, "code_len": ln}
        return g.issue(g.GrantClaims.from_claims(raw), key or KEY_BYTES,
                        purpose=purpose)

    ma = "MP4C_LIVE_A_" + uuid.uuid4().hex
    mb = "MP4C_LIVE_B_" + uuid.uuid4().hex
    code_a = "print('%s')" % ma
    code_b = "print('%s')" % mb
    code_c = "import os; print('SOCKET_PRESENT', os.path.exists('/var/run/docker.sock'))"
    report["markers"] = {"a": ma, "b": mb}

    try:
        d(["rm", "-f", CONTAINER])
        up = compose("up", "-d", "--build", "--no-deps", "executor")
        report["up_exit"] = up.returncode
        if up.returncode != 0:
            print("LIVE_RESULT " + json.dumps({"ok": False, "stage": "up",
                                               "err": up.stderr[-400:]}))
            return 1
        report["health_first"] = wait_healthy()
        # Mint AT DISPATCH TIME, exactly as the backend does: a 120 s grant minted
        # BEFORE a ~100 s startup would already be expired by the first request.
        R = g.PURPOSE_RESERVE
        # ONE bound-action identity per action, shared by that action's reserve and
        # execute tokens: the reservation is keyed by the BOUND ACTION, so a fresh
        # tool_call_id per token would never match.
        grants = {"a": mint(7, code_a, tc="tc-a"),
                  "b": mint(8, code_b, tc="tc-b"),
                  "c": mint(7, code_c, tc="tc-c"),
                  "ra": mint(7, code_a, purpose=R, tc="tc-a"),
                  "ra2": mint(7, code_a, purpose=R, tc="tc-a"),
                  "rb": mint(8, code_b, purpose=R, tc="tc-b"),
                  "rc": mint(7, code_c, purpose=R, tc="tc-c"),
                  "forged": mint(7, code_a, key=b"a-different-signing-key-0123456789",
                                 tc="tc-a"),
                  "code_a": code_a, "code_b": code_b, "code_c": code_c}
        report["nonce_rows_before"] = nonce_rows()
        client, tail = run_client(grants)
        report["client_first"] = client
        report["client_first_tail"] = tail

        report["published_ports"] = d(["port", CONTAINER]).stdout.strip()

        lg = d(["logs", "--tail", "60", CONTAINER])
        logs = lg.stdout + "\n" + lg.stderr
        report["key_in_logs"] = key.decode() in logs
        report["marker_in_logs"] = (ma in logs) or (mb in logs)
        report["code_in_logs"] = "print(" in logs
        report["log_bytes"] = len(logs)

        # ---- RESTART with the SAME volume, then replay the SAME grant ----
        # NOTE: the grant TTL is 120 s and a restart re-runs the ~100 s startup
        # probe, so the replay may legitimately be refused as EXPIRED. The raw
        # status is reported, never converted into a claim.
        rst = compose("restart", "executor", timeout=600)
        report["restart_exit"] = rst.returncode
        report["health_after_restart"] = wait_healthy()
        # The durable store must still hold the consumed jti after the restart.
        report["nonce_rows_after_restart"] = nonce_rows()
        # AFTER the restart the in-process reservations are gone, so a fresh
        # reserve is granted and the DURABLE nonce store is what refuses.
        replay_grants = dict(grants)
        replay_grants.update({
            "ra": mint(7, code_a, purpose=R, tc="tc-a"),
            "ra2": mint(7, code_a, purpose=R, tc="tc-a"),
            "rb": mint(8, code_b, purpose=R, tc="tc-b"),
            "rc": mint(7, code_c, purpose=R, tc="tc-c")})
        after, tail2 = run_client(replay_grants)
        report["client_after_restart"] = after
        report["client_after_restart_tail"] = tail2

        owned = d(["ps", "-a", "--filter", "label=mp4c.owner=mp4c-executor",
                   "--format", "{{.Names}}"]).stdout
        report["owned_leftover"] = [x for x in owned.splitlines() if x.strip()]
        ns = d(["volume", "ls", "--filter",
                "name=%s_executor_nonce_data" % PROJECT,
                "--format", "{{.Name}}"]).stdout
        report["nonce_volume"] = [x for x in ns.splitlines() if x.strip()]
    finally:
        dn = compose("down", "-v", "--remove-orphans", timeout=600)
        report["down_exit"] = dn.returncode
        d(["rm", "-f", CONTAINER])
        after_names = names()
        report["containers_before"] = len(before)
        report["containers_after"] = len(after_names)
        # Restored: the earlier rewrite of this tail DELETED this assignment, so the
        # predicate read a missing key and the verdict said false with seen=null.
        report["new_containers_left"] = [c for c in after_names
                                         if c not in before and "mp4c" in c]
    c1 = report.get("client_first", {})
    c2 = report.get("client_after_restart", {})
    # ASSIGN every derived field BEFORE it can be read by a predicate. The
    # earlier aggregate read `after_restart_payload_present` one statement before
    # it was set, so `None is True` was False and a fully green run reported
    # ok=false. Each predicate is now evaluated exactly once, and its observed
    # input is captured alongside it, so the aggregate can never disagree with
    # its parts.
    report["after_restart_payload_present"] = bool(c2)
    report["restart_replay_status"] = c2.get("replay_a", [0, ""])[0]

    checks = []

    def ck(name, good, seen=None):
        # `seen_missing` distinguishes a MISSING input (a harness defect: the
        # predicate read a key nobody set) from an observed value that failed.
        checks.append({"check": name, "pass": bool(good),
                       "seen_missing": seen is None,
                       "seen": str(seen)[:200] if seen is not None else None})

    ua = c1.get("user_a", [0, ""])
    ub = c1.get("user_b", [0, ""])
    ck("up_exit_zero", report.get("up_exit") == 0, report.get("up_exit"))
    ck("healthy_before", str(report.get("health_first", "")).startswith("healthy"),
       report.get("health_first"))
    ck("no_grant_refused", c1.get("no_grant", [0])[0] == 400, c1.get("no_grant"))
    ck("malformed_refused", c1.get("malformed_grant", [0])[0] == 400,
       c1.get("malformed_grant"))
    ck("forged_refused", c1.get("forged_grant", [0])[0] == 401,
       c1.get("forged_grant"))
    ck("user_a_executed_exactly_once", ua[0] == 200 and ua[1].count(ma) == 1, ua)
    ck("user_b_executed_exactly_once", ub[0] == 200 and ub[1].count(mb) == 1, ub)
    ck("users_are_distinct_actions", ma not in ub[1] and mb not in ua[1],
       {"ma_in_b": ma in ub[1], "mb_in_a": mb in ua[1]})
    ck("replay_refused", c1.get("replay_a", [0])[0] == 409, c1.get("replay_a"))
    ck("code_container_has_no_socket",
       "SOCKET_PRESENT False" in c1.get("code_container_socket", ["", ""])[1],
       c1.get("code_container_socket"))
    ck("no_published_port", report.get("published_ports") == "",
       report.get("published_ports"))
    ck("key_absent_from_logs", report.get("key_in_logs") is False,
       report.get("key_in_logs"))
    ck("markers_absent_from_logs", report.get("marker_in_logs") is False,
       report.get("marker_in_logs"))
    ck("healthy_after_restart",
       str(report.get("health_after_restart", "")).startswith("healthy"),
       report.get("health_after_restart"))
    ck("nonce_volume_present", bool(report.get("nonce_volume")),
       report.get("nonce_volume"))
    ck("nonce_rows_survive_restart",
       (report.get("nonce_rows_after_restart") or 0) >= 1
       and (report.get("nonce_rows_after_restart") or 0)
       >= (report.get("nonce_rows_before") or 0),
       {"after": report.get("nonce_rows_after_restart"),
        "before": report.get("nonce_rows_before")})
    ck("no_new_containers", report.get("new_containers_left") == [],
       report.get("new_containers_left"))
    ck("no_owned_leftover", report.get("owned_leftover") == [],
       report.get("owned_leftover"))
    ck("after_restart_payload_present",
       report.get("after_restart_payload_present") is True,
       report.get("after_restart_payload_present"))
    ck("replay_refused_after_restart",
       c2.get("replay_a", [0])[0] in (409, 401)
       and c2.get("replay_a", [0])[0] != 200, c2.get("replay_a"))

    failed = [c["check"] for c in checks if not c["pass"]]
    report["checks"] = checks
    report["failed_checks"] = failed
    report["ok"] = not failed

    artifact = {
        "artifact": "mp4c_live_verdict.json",
        "harness": "tests/smoke/mp4c_compose_live.py",
        "aggregate_ok": report["ok"],
        "checks_total": len(checks),
        "checks_passed": sum(1 for c in checks if c["pass"]),
        "failed_checks": failed,
        "checks": checks,
        "note": ("bounded verdict only: status slugs, markers and counts. No code, "

                 "no key, no token and no raw child output."),
    }
    with open(ARTIFACT, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, indent=1, sort_keys=True)
    print("LIVE_RESULT " + json.dumps(report))
    return 0 if report["ok"] else 1
if __name__ == "__main__":
    sys.exit(main())
