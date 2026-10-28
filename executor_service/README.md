# Python executor service (MP-4C) — internal, dedicated

A **separately runnable** service that executes model-emitted Python inside a
hardened, digest-pinned Docker container. It lives **outside `app/**`**: the
application never imports it, it is not mounted on the application's FastAPI
instance, and it is not wired into RunWorker, the approval gateway or the
frontend. Execution stays DISABLED at the application layer
(`run_engine_sandbox` and the other seven flags remain `False`, and
`real_python_execution_available()` remains `False`).

## Three verdicts (kept separate on purpose)

| # | Verdict | Status |
|---|---|---|
| 1 | **Local run** — `python -m executor_service` on a host with a Docker daemon | **WORKS.** Verified against the real daemon, the pinned image and the full isolation probe. |
| 2 | **Compose deployment** — the approved single-instance trusted SELF-HOST topology | **WORKS.** An image and a Compose service exist; live startup, readiness, service discovery and an authenticated request were verified in a disposable project. |
| 3 | **Production readiness** | **NOT READY.** Passing Compose does not complete MP-4C. Execution is still disabled at the app layer, nothing is wired, quotas are process-local, and the isolation boundary is a shared-kernel container (see below). |

## Topology boundaries

* **Only the executor service reaches the Docker daemon.** `docker-compose.yml`
  bind-mounts `/var/run/docker.sock` into the `executor` service alone. The
  backend, the frontend, ChromaDB and every executed Python container get no
  socket and no daemon credentials. (A hermetic test asserts exactly one service
  mounts it; the probe asserts the executed container sees no socket.)
* **The endpoint is reachable only by the backend**, on the dedicated
  `executor-internal` network, which is declared `internal: true` (no external
  route). The executor publishes **no host port** — only `expose: 8091`.
* **The backend keeps its existing network access**: it stays on `research-network`
  (ChromaDB + `host.docker.internal` for Ollama/GLM) and is merely ADDED to
  `executor-internal`. Nothing in the stack `depends_on` the executor, so a
  multi-minute readiness probe can never delay the application's startup.
* **Exactly one replica.** Quotas are PROCESS-LOCAL, so a second replica would
  each enforce its own caps and could exceed the intended global limit.
* The CLOUD topology (`docker-compose.prod.yml`, Render) is deliberately
  unchanged: there is no Docker daemon there, so this service cannot run on it.

### Socket access requires GID 0

Docker Desktop mounts the socket as `root:root`, mode `0660`, so a plain
non-root user is **denied**. The image therefore runs as `USER 10001:0`: the
process is non-root, but belongs to group 0 purely to use the socket. This is
daemon-equivalent privilege, which is exactly why the socket is mounted into this
one service only.

## Credential provisioning

ONE shared secret, `EXECUTOR_SERVICE_GRANT_KEY` (file secret at
`/run/secrets/executor_grant_key`), provisioned to **the backend and this service
only**. It is gitignored, operator-provided, never an inline value, and never
appears in `docker compose config`, `Config.Env`, `docker inspect` or a process
listing. See `secrets/README.md`.

**The credential IS a signed grant.** The request body carries no identity field
at all: `{grant, code, run_id}`. The grant is an HMAC-SHA256 over canonical
claims, so only a holder of the shared secret can produce one, and identity is
inside the signature — a browser or a model cannot choose user, tenant, Run, step
or action. The exact code bytes are bound by `code_sha256` + `code_len`.

The earlier static token map (`token -> one identity`) **could not represent
arbitrary authenticated users** without provisioning one token per user, so it is
now PROTOTYPE-ONLY and is not a production fallback.

Claims bound: `user_id, tenant_id, run_id, step_id, tool_call_id, approval_id,
action_hash, code_sha256, code_len, iat, exp, jti, v`. Lifetime is capped at
120 s with ±30 s skew; a far-future `iat` is refused. The backend-side derivation
(`app/run_engine/executor_grant.py`, UNWIRED) issues a grant only when the durable
Run / step / ToolCall / approval identities and an already committed
`DispatchRecord.started` all agree.

## Replay guard (durable)

A consumed `jti` is written to a **SQLite store on a named volume** and the insert
is atomic, so exactly one caller wins. The record survives a restart on the same
volume: the SAME signed request is refused after a restart too. An unavailable or
corrupt store raises, and the service fails **closed with zero starts**. Only a
SHA-256 of the jti is stored — never code, a token or output.

**Boundary, stated plainly:** the volume IS the durability. `docker compose down -v`
destroys it and with it the cross-restart guarantee; a fresh volume starts empty.
This is a replay guard only, NOT a second approval system — the application-side
`DispatchRecord` remains the execution authority.

The loader reads the FILE first and treats it as authoritative: if the configured
file exists but is malformed, the registry is empty and every request is
rejected — there is no silent fallback to the environment.

The request body carries **no** `user_id`/`tenant_id`. Identity is resolved
server-side from the token (constant-time comparison over the whole registry), so
a caller cannot impersonate another user or tenant: there is no field to set.

## Surface

| Method | Path | Notes |
|---|---|---|
| `GET`  | `/healthz` | 200 ready / 503 unready, bounded reason slugs |
| `POST` | `/v1/execute` | body `{grant, code, run_id}` ONLY; identity is inside the signed grant. The `Authorization` header is ignored. |

There is no field for an image, argv, mount, network mode, environment variable or
resource limit (`extra="forbid"`), and validation errors are re-rendered WITHOUT
echoing the caller's input. Loopback is the default bind; a non-loopback bind
requires the explicit `EXECUTOR_SERVICE_ALLOW_INTERNAL_BIND=1` opt-in, which the
Compose service sets because the backend connects from another container.

## Container contract (fixed)

Digest-pinned `python:3.12-slim@sha256:44ff437…eec9b` (`linux/amd64`):

```
--rm  --label mp4c.owner=<owner>  --interactive
--network none  --read-only  --tmpfs /scratch:rw,noexec,nosuid,nodev,size=16m
--user 65534:65534  --cap-drop ALL  --security-opt no-new-privileges
--pids-limit 64  --memory 128m --memory-swap 128m  --cpus 0.5
--ulimit nofile=64:64
python -I -B -u -
```

Source travels ONLY over stdin. No shell, no volume, no environment variable and
no temporary host file carries code.

## Readiness (fails closed, never pulls)

READY only when ALL hold, otherwise unready (503; `python -m executor_service`
refuses to start):

1. the Docker daemon is reachable,
2. the EXACT pinned image is present locally with the pinned digest,
3. the full adversarial isolation probe passes (**33 checks**).

Nothing pulls an image — not at startup, not on a request. Provisioning is a
separate, explicitly authorized manual step (`image.PROVISION_COMMANDS`).

Startup also performs **owned-container cleanup**: it removes only containers
carrying this service's stable `mp4c.owner` label (a crashed previous instance's
leftovers) and never touches unrelated containers.

Readiness is evaluated **exactly once per start**. The entrypoint validates it
and the SAME result is injected into the app, which never re-runs the probe. It
used to be evaluated twice (entrypoint + app lifecycle) and two independent runs
could disagree, leaving the service serving `/healthz` 503 after startup had
already been approved.

A failing start is diagnosable from bounded fields only: `/healthz` and the
startup log carry `reasons`, `failed_checks`, per-phase `timings_ms`, the probe
`status`, `checks`, `duration_ms` and `containerized`. Tokens, code and child
output are never included.

**What `/healthz` guarantees after startup.** The isolation posture proven ONCE at
startup is reported from the cached evaluation; availability that CAN change
(daemon reachable, exact pinned image present) is re-checked cheaply on EVERY call
and a live failure makes the endpoint 503. The full ~100 s probe is never re-run
per request or per health poll. A separate cheap pre-flight runs on every
`/v1/execute` BEFORE quota reservation or container creation, so a daemon or image
lost after startup yields a bounded refusal rather than a failed start.

Readiness answers *"is this service operational?"*. It is deliberately NOT a
second authority for `app/run_engine/sandbox_policy.real_python_execution_available()`,
which answers *"may model-emitted Python execute?"* and stays `False`. A test
pins that readiness can be `True` while that predicate remains `False`.

## Probe scope labelling (honest about what each check observes)

The probe runs both on a bare host and INSIDE the service container. Every check
carries a scope label, reported per check and summarised in `PROBE_RESULT`:

| Scope | Count | Meaning |
|---|---|---|
| `workload` | 22 | The executed Python container: uid, read-only rootfs, tmpfs, cgroup limits, capabilities, argv/cmdline, stdio result, and the isolation side of the network control. True wherever the probe runs. |
| `daemon` | 3 | The Docker daemon's own view (`docker ps`, `docker inspect`). |
| `contract` | 3 | The fixed broker argv/plan (purely local to the probe process). |
| `service` | 3 | **ONLY the filesystem/env of whatever container runs the probe.** Not a statement about the physical Docker host: `host_canary_not_readable`, `host_env_secret_not_inherited`, `canary_removed`. |
| `control` | 1 | The harness proving the OTHER side of a test is real: `network_tripwire_positive_control_reachable`. |
| `governance` | 1 | The labelling invariant itself (an unlabelled check is reported). |

The former `reduced` scope is **retired**: `network_tripwire_synchronised_and_blocked`
was upgraded to `workload` because the new positive control EARNS it, not because
it was renamed. A test asserts no check hides behind `reduced`.

**No check reads the physical Docker host's filesystem.** What the `service`-scoped
checks prove is the deployment-relevant claim: the executed container cannot see
the *service container's* filesystem or inherit its environment.

**The network test is now a paired control.** The probe FIRST runs a fixed,
harness-owned control container (no socket, read-only, no capabilities, no
model-supplied code) that dials the listener. Containerized, the control joins
one of the probe container's own networks and dials its IP, because
`host.docker.internal` names the Docker HOST and can never reach the service
container. The workload then dials the **same host:port** with `--network none`,
completes its attempt, and must produce ZERO listener hits.

Without the positive control, "blocked" was vacuous: an unbound or unreachable
listener also yields blocked. If no reachable target can be established, the
control check FAILS as `unproven` and readiness stays fail-closed.

## Run it

```bash
# Compose (single-instance self-host)
cp secrets/executor_service_tokens.json.example secrets/executor_service_tokens.json
#   ...replace the placeholder with a long random token
docker compose up -d --build executor
docker compose ps
docker compose down -v

# or directly, on a host that has a Docker daemon
export EXECUTOR_SERVICE_TOKENS='{"<token>": {"principal": "app", "user_id": 1}}'
python -m executor_service            # 127.0.0.1:8091
```

`docker compose up` reuses an existing image. **Rebuild after any service-code
change** (`--build`), or the deployment will run stale code.

## Known limits

* Containment is a hardened Linux container sharing Docker Desktop's **WSL2
  kernel**. No gVisor / Kata / microVM boundary is claimed, and approval does not
  make this safe for untrusted multi-tenant deployment.
* Quotas are process-local; multi-instance needs separately designed shared
  coordination (and a distinct owner label per instance).
* No token expiry or rotation beyond replacing the secret file and restarting.
* The executor container has no egress (the network is `internal`), which is
  sufficient because it reaches the daemon over a mounted socket.

## Tests

```bash
pytest tests/test_mp4c_executor_dedicated.py       # hermetic: contract, identity,
                                                  # caps under contention, readiness,
                                                  # logging policy
pytest tests/test_mp4c_executor_topology.py       # Compose/Dockerfile boundaries,
                                                  # secret loader, bind opt-in,
                                                  # probe scope labels
pytest tests/test_mp4c_executor_dedicated_e2e.py  # real containers + real probe
python tests/smoke/mp4c_isolation_probe.py        # 32-check adversarial probe
python tests/smoke/mp4c_compose_live.py           # disposable live Compose project
python tests/smoke/mp4c_compose_starts.py         # bounded series of fresh starts
```
