import json, os, subprocess, time
REPO = r"C:\Users\user\Desktop\personal-ai-research-assistant"
os.chdir(REPO)
CONTAINER = "research-assistant-executor"
NET = "mp4c-exec-test_executor-internal"
PINNED = "python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b"

def d(args, timeout=600, input_text=None):
    return subprocess.run(["docker"] + args, capture_output=True, text=True,
                          timeout=timeout, input=input_text)

token = next(iter(json.load(open(os.path.join(REPO, "secrets", "executor_service_tokens.json")))))
try:
    d(["rm", "-f", CONTAINER])
    p = d(["compose", "-p", "mp4c-exec-test", "-f", "docker-compose.yml",
           "up", "-d", "--no-deps", "executor"], timeout=900)
    print("UP", p.returncode, p.stderr[-200:], flush=True)
    deadline = time.time() + 420
    health = "?"
    while time.time() < deadline:
        q = d(["inspect", CONTAINER, "--format",
               "{{if .State.Health}}{{.State.Health.Status}}{{end}}"])
        health = q.stdout.strip()
        if health == "healthy":
            break
        time.sleep(10)
    print("HEALTH", health, flush=True)
    prog = ("import json, urllib.request, urllib.error\n"
            "def call(path, token=None, body=None):\n"
            "    req = urllib.request.Request('http://executor:8091' + path, method='POST' if body is not None else 'GET')\n"
            "    data = None\n"
            "    if body is not None:\n"
            "        req.add_header('Content-Type', 'application/json'); data = json.dumps(body).encode()\n"
            "    if token: req.add_header('Authorization', 'Bearer ' + token)\n"
            "    try:\n"
            "        with urllib.request.urlopen(req, data=data, timeout=30) as r: return r.status\n"
            "    except urllib.error.HTTPError as e: return e.code\n"
            "    except Exception: return 0\n"
            "print('UNAUTH', call('/v1/execute', body={'code': 'print(1)', 'run_id': 'lc-1'}))\n"
            "print('AUTH', call('/v1/execute', token='__T__', body={'code': \"print('LOGCODE')\", 'run_id': 'lc-2'}))\n"
            ).replace("__T__", token)
    c = d(["run", "--rm", "-i", "--network", NET, "--entrypoint", "python",
           PINNED, "-I", "-B", "-u", "-"], input_text=prog, timeout=300)
    print("CLIENT", c.returncode, "|", c.stdout.strip(), "|", c.stderr[-200:], flush=True)
    time.sleep(3)
    lg = d(["logs", "--tail", "40", CONTAINER])
    logs = lg.stdout
    print("LOG_RC", lg.returncode, "LOG_BYTES", len(logs), "ERR", lg.stderr[-200:], flush=True)
    print("LOG_HAS_TOKEN", token in logs, "LOG_HAS_CODE", "LOGCODE" in logs, flush=True)
    print("LOG_SAMPLE>>>", flush=True)
    print(logs[:1500], flush=True)
finally:
    p = d(["compose", "-p", "mp4c-exec-test", "-f", "docker-compose.yml",
           "down", "-v", "--remove-orphans"], timeout=300)
    print("DOWN", p.returncode, p.stderr[-200:], flush=True)
