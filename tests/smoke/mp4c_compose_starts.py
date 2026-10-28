"""MP-4C — bounded series of FRESH disposable Compose starts with diagnostics.

TEST-ONLY. For each attempt: clear any limbo container, start the executor in its
own Compose project (fresh container), wait for a terminal health status, and
record the service's own BOUNDED readiness diagnostics (failed checks, timings,
probe status) from the container log. Then tear the project down.

Never prints the service token. Reports EVERY attempt, including failures.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT = "mp4c-exec-test"
COMPOSE = os.path.join(REPO_ROOT, "docker-compose.yml")
CONTAINER = "research-assistant-executor"
# The shared backend<->executor signing secret (never printed).
SECRET_FILE = os.path.join(REPO_ROOT, "secrets", "executor_grant_key")
ATTEMPTS = int(os.environ.get("MP4C_ATTEMPTS", "3"))
SETTLE_S = float(os.environ.get("MP4C_SETTLE_S", "600"))


def d(args, *, timeout=1200):
    return subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                          timeout=timeout)


def compose(*args, timeout=2400):
    return d(["compose", "-p", PROJECT, "-f", COMPOSE] + list(args), timeout=timeout)


def names():
    return sorted(n for n in d(["ps", "-a", "--format", "{{.Names}}"]).stdout.splitlines()
                  if n.strip())


def health():
    return d(["inspect", CONTAINER, "--format",
              "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
              "|{{.State.Status}}|{{.RestartCount}}"]).stdout.strip()


def main() -> int:
    token = open(SECRET_FILE, encoding="utf-8").read().strip()
    before = names()
    results = []
    for attempt in range(1, ATTEMPTS + 1):
        rec = {"attempt": attempt}
        try:
            d(["rm", "-f", CONTAINER])
            up = compose("up", "-d", "--build", "--no-deps", "executor")
            rec["up_exit"] = up.returncode
            if up.returncode != 0:
                rec["up_stderr"] = up.stderr[-300:]
            deadline = time.time() + SETTLE_S
            st = ""
            while time.time() < deadline:
                st = health()
                if st.split("|")[0] in ("healthy", "unhealthy"):
                    break
                time.sleep(5)
            rec["health"] = st
            lg = d(["logs", "--tail", "40", CONTAINER])
            logs = lg.stdout + "\n" + lg.stderr
            rec["token_in_logs"] = token in logs
            rec["code_in_logs"] = "print(" in logs
            line = ""
            for candidate in logs.splitlines():
                if "executor_readiness" in candidate:
                    line = candidate.strip()
            rec["readiness_log"] = line[-600:]
            rec["log_bytes"] = len(logs)
            out = d(["ps", "-a", "--filter", "name=mp4c_probe_",
                     "--format", "{{.Names}}"]).stdout
            rec["probe_leftover"] = [x for x in out.splitlines() if x.strip()]
            rec["ok"] = (st.split("|")[0] == "healthy"
                         and "ready=True" in line
                         and rec["token_in_logs"] is False)
        finally:
            dn = compose("down", "-v", "--remove-orphans", timeout=600)
            rec["down_exit"] = dn.returncode
            d(["rm", "-f", CONTAINER])
        results.append(rec)
        print("ATTEMPT " + json.dumps(rec), flush=True)

    after = names()
    summary = {
        "attempts": len(results),
        "passed": sum(1 for r in results if r.get("ok")),
        "failed": [r["attempt"] for r in results if not r.get("ok")],
        "containers_before": len(before),
        "containers_after": len(after),
        "new_containers_left": [c for c in after if c not in before and "mp4c" in c],
        "results": results,
    }
    print("STARTS_RESULT " + json.dumps(summary), flush=True)
    return 0 if summary["failed"] == [] else 1


if __name__ == "__main__":
    sys.exit(main())
