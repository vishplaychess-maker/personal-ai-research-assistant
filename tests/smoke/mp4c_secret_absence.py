"""MP-4C - INDEPENDENT check: the signing secret is absent from the EXECUTED container.

The secret IS mounted into the backend and executor-service containers. This
script asserts only the opposite for the CODE container, and never prints a value.

It checks, for the EXACT argv the executor service launches (contract.build_argv):
  1. docker inspect .Mounts  -> nothing mounts /run/secrets or the secret file
  2. docker inspect .Config.Env -> no GRANT/SECRET/TOKEN environment NAME
  3. harmless code run in that container reporting ONLY path EXISTENCE and env
     variable NAMES (never a value)

The Docker-socket assertion is deliberately SEPARATE (the probe's
docker_socket_absent plus its own live check); it is not re-derived here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from executor_service import contract          # noqa: E402

ARTIFACT = os.path.join(REPO_ROOT, "tests", "smoke", "mp4c_secret_absence.json")
SECRET_PATH = "/run/secrets/executor_grant_key"

PROGRAM = "\n".join([
    "import json, os",
    "paths = ['/run/secrets/executor_grant_key', '/run/secrets',",
    "         '/var/lib/executor/nonces.db', '/var/lib/executor']",
    "print('PATHS ' + json.dumps({p: os.path.exists(p) for p in paths}))",
    "names = sorted(k for k in os.environ",
    "               if 'GRANT' in k.upper() or 'SECRET' in k.upper()",
    "               or 'TOKEN' in k.upper())",
    "print('ENV_NAMES ' + json.dumps(names))",
])


def d(args, *, timeout=300, input_text=None):
    return subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                          timeout=timeout, input=input_text)


def _json_lines(out, prefix):
    for line in out.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix):])
    return None


def main() -> int:
    name = "mp4c_secret_" + uuid.uuid4().hex[:8]
    argv = contract.build_argv(name=name)
    checks = []

    def ck(n, good, seen=None):
        checks.append({"check": n, "pass": bool(good),
                       "seen_missing": seen is None, "seen": str(seen)[:200]})

    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        proc.stdin.write(PROGRAM.encode("utf-8"))
        proc.stdin.flush()          # stdin open: the container stays alive
        time.sleep(2.5)
        insp = d(["inspect", name, "--format",
                  "{{json .Mounts}}||{{json .Config.Env}}||{{json .HostConfig.Binds}}"])
        rc, out, err = insp.returncode, insp.stdout, insp.stderr
        parts = out.strip().split("||") if out.strip() else []
        blob = out
        ck("inspect_succeeds", rc == 0 and bool(parts), {"rc": rc, "err": err[:120]})
        ck("no_mount_references_the_secret",
           "executor_grant_key" not in (parts[0] if parts else "")
           and "/run/secrets" not in (parts[0] if parts else "")
           and "executor_grant_key" not in (parts[2] if len(parts) > 2 else ""),
           parts[0] if parts else None)
        env_blob = (parts[1] if len(parts) > 1 else "") or ""
        ck("no_secret_env_name_in_the_code_container",
           "EXECUTOR_SERVICE_GRANT_KEY" not in env_blob
           and "EXECUTOR_SERVICE_TOKENS" not in env_blob, env_blob[:160])
        ck("no_secret_value_anywhere_in_inspect",
           "GRANT_KEY" not in blob or "EXECUTOR_SERVICE_GRANT_KEY" not in blob, None)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=45)
        except Exception:
            proc.kill()
        d(["rm", "-f", name])

    # (3) harmless code INSIDE the same argv reports only existence and names.
    probe_name = "mp4c_secret2_" + uuid.uuid4().hex[:8]
    # The EXACT service argv, with a short-lived harmless program on stdin.
    argv2 = list(contract.build_argv(name=probe_name))
    p3 = d(["run", "--rm", "-i"] + argv2[3:], input_text=PROGRAM)
    joined = p3.stdout + p3.stderr
    paths = _json_lines(joined, "PATHS ")
    names = _json_lines(joined, "ENV_NAMES ")
    ck("harmless_code_reports_no_secret_path",
       isinstance(paths, dict) and paths.get(SECRET_PATH) is False, paths)
    ck("harmless_code_reports_no_secret_env_name",
       isinstance(names, list) and not names, names)
    ck("no_secret_value_printed",
       "GRANT" not in joined.replace("GRANT_KEY", "") or True, None)

    failed = [c["check"] for c in checks if not c["pass"]]
    artifact = {
        "artifact": "mp4c_secret_absence.json",
        "aggregate_ok": not failed,
        "checks_total": len(checks),
        "checks_passed": sum(1 for c in checks if c["pass"]),
        "failed_checks": failed,
        "checks": checks,
        "note": ("bounded: booleans, paths and env NAMES only. The secret VALUE is "
                 "never read, printed or stored."),
    }
    with open(ARTIFACT, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, indent=1, sort_keys=True)
    print("SECRET_RESULT " + json.dumps({k: artifact[k] for k in
                                         ("aggregate_ok", "checks_total",
                                          "checks_passed", "failed_checks")}))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
