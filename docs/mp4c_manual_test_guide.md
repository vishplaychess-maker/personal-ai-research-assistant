# MP-4C - Manual user test guide: approval-gated isolated Python execution

Phase 1B.2R MP-4C. This guide is written from the actual repository state and is
verified against the rendered Compose configuration before anything is started.

## 0. NEVER run these plain commands

The repo Compose file fixes `container_name: research-assistant-*` and publishes
`8080` / `5173`, and the existing Thunder AI stack IS the Compose project
`personal-ai-research-assistant` built from that same file. Therefore:

```
# DO NOT RUN - this operates on the REAL stack and its real app_data volume
docker compose up -d
docker compose -f docker-compose.yml -f docker-compose.optin.yml up -d --build
```

Adding `-p` alone is NOT sufficient either, because the fixed container names and
published ports still collide. Use the isolation overlay in section 3, which
also renames every container, remaps every port, renames every volume, swaps in a
separate grant-key file and supplies disposable secrets.

## 1. Honesty boundary (read first)

* The **fail-closed default** in section 5 is proven by automated tests against
  the real approval gateway and durable rows.
* The **opt-in execution path** is proven end to end by a DISPOSABLE automated
  journey that runs the real `RunWorker`, the real approval gateway, the real
  broker client over HTTP, the real executor service and a real Python
  container: `tests/smoke/mp4c_runner_e2e.py` and the artifact
  `tests/smoke/mp4c_runner_e2e.json` (`aggregate_ok: true`). That journey
  replaces **only the model** with a deterministic scripted chat provider.
* The **browser click-through** is a separate exercise. If a small local model
  cannot emit a valid `[PYTHON_CODE: ...]` marker, a clearly labelled
  deterministic model-only stub may be used for the browser run; the worker,
  gateway, broker, executor service and Python container stay real, and the stub
  run is reported SEPARATELY because it does not prove that a real local model
  proposes Python reliably.

## 2. What is OFF by default

All eight durable-engine flags default to `false`, and the executor operator
opt-in defaults to unset (off):

```
RUN_ENGINE_ENABLED                    run_engine_enabled
RUN_WORKER_ENABLED                    run_worker_enabled
RUN_MESSAGES_STREAM_DURABLE_ENABLED   run_messages_stream_durable_enabled
RUN_DEEP_RESEARCH_ENABLED             run_deep_research_enabled
RUN_MULTI_AGENT_ENABLED               run_multi_agent_enabled
RUN_PUBLIC_API_ENABLED                run_public_api_enabled
RUN_CHAT_STREAM_ENABLED               run_chat_stream_enabled
RUN_ENGINE_SANDBOX                    run_engine_sandbox
--- the executor operator opt-in (env only, no Settings field) ---
EXECUTOR_OPERATOR_OPT_IN
EXECUTOR_SERVICE_URL
```

`real_python_execution_available()` becomes `True` only when the operator opt-in
is set, an executor endpoint is configured, the signing secret is valid, and the
broker probe reports the executor live-ready RIGHT NOW. Anything else is `False`,
and no flag can unlock an in-process host subprocess.

The disposable overlay enables the durable stream flag. That flag is required for
the browser to create a python proposal at all, and it is enabled ONLY inside the
disposable project, never in the base file.

## 3. Disposable stack: prerequisites and isolation

### 3.1 Docker and the pinned image

Docker Desktop with a Linux engine (verified here with engine 29.8.0 on WSL2).
There is no gVisor, Kata or microVM boundary: containment is a hardened
container, not a virtual machine.

```
docker buildx imagetools inspect python:3.12-slim
docker pull python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b
docker image inspect python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b --format "{{.Id}} {{json .RepoDigests}}"
```

While the pin is absent the executor refuses to start with the bounded reason
`executor_image_not_provisioned`.

### 3.2 Disposable secrets (never the real ones)

The disposable stack must NOT read the repo `.env` and must NOT reuse
`secrets/executor_grant_key`. Supply its own, using `--env-file` (which stops
`.env` from being consulted) plus a separate key file:

```
# secrets/mp4c_uitest.env      - disposable LLM_PROVIDER / ENCRYPTION_KEY /
#                                JWT_SECRET / RUN_IDEMPOTENCY_HMAC_KEY and the
#                                empty MP4C_UITEST_OPT_IN default
# secrets/executor_grant_key.mp4c_uitest - disposable signing key
```

Both live under `secrets/`, which is gitignored. Generate fresh values; never
copy the real key. Proof that `.env` is not consulted: the rendered config shows
`ENABLE_TERMINAL_TOOL=false` (the Compose default) even though the real `.env`
sets it.

### 3.3 The isolation overlay

`docker-compose.isolation.yml` gives every service a distinct container name, an
unused localhost port, its own explicitly named volume, a disposable signing key,
a unique executor owner label, and NO frontend host bind mount (the frontend image
is built from the working tree, so it shares no host source directory).

Verify the FULLY RENDERED configuration and compare it with the existing stack
BEFORE starting anything:

```
docker compose --env-file secrets/mp4c_uitest.env -p mp4c-uitest -f docker-compose.yml -f docker-compose.isolation.yml config
```

Expected: project `mp4c-uitest`; containers `mp4c-uitest-*`; backend published on
`127.0.0.1:18080`, frontend on `127.0.0.1:15173`; volumes `mp4cuitest_*`; secret
file `secrets/executor_grant_key.mp4c_uitest`; `EXECUTOR_SERVICE_OWNER=mp4c-uitest-executor`;
`EXECUTOR_OPERATOR_OPT_IN` empty. Nothing may mention `research-assistant-*` or
`personal-ai-research-assistant_*`.

```
# the owner sweep can only ever match containers carrying THIS label:
docker ps -a --filter "label=mp4c.owner" --format "{{.Names}}"          # expected: empty
docker ps -a --filter "label=mp4c.owner=mp4c-uitest-executor" --format "{{.Names}}"   # expected: empty
```

Record the existing containers and volumes before starting, and confirm they are
unchanged after teardown.

### 3.4 Local model and the two REQUIRED UI configuration steps

Ollama on the host (`OLLAMA_HOST=host.docker.internal`, port 11434). Use
`llama3.2:3b` (verified to emit a valid marker); `dolphin-mistral` also works. No
model download and no cloud model substitution.

Two UI steps are REQUIRED before a Python proposal can happen. Without them the run
fails closed with `provider_error` (the session model is `None`):

1. **Settings -> Providers -> Add Provider**: provider `Ollama`, leave the API key
   blank, Default Model `llama3.2:3b`, tick `Set as active provider`, Save.
2. **Per session**: pick `llama3.2:3b` in the session-model button, and use
   `Edit system prompt` to set a prompt that contains the marker instruction:

   ```
   You are a helpful research assistant. When the user asks you to perform
   calculations, data analysis, or to write or run code, you MUST run Python. To
   run code, wrap it EXACTLY in this format:
   [PYTHON_CODE: <your python code>]
   The code will be executed and the output returned to you so you can report the
   result.
   Use AT MOST ONE [PYTHON_CODE: ...] marker per answer. If a Python result has
   ALREADY been returned in this conversation, do NOT emit another marker: answer
   the user directly and state the result.
   ```

The durable chat submission carries `system_prompt = <session system prompt>` and
`model = <session model>`. The base tool contexts are NOT injected on the durable
path, so an empty session prompt means the model is never told the marker grammar.

## 4. Start the disposable stack (phase 1: opt-in OFF)

```
docker compose --env-file secrets/mp4c_uitest.env -p mp4c-uitest -f docker-compose.yml -f docker-compose.isolation.yml up -d --build
docker compose --env-file secrets/mp4c_uitest.env -p mp4c-uitest -f docker-compose.yml -f docker-compose.isolation.yml ps
docker compose --env-file secrets/mp4c_uitest.env -p mp4c-uitest -f docker-compose.yml -f docker-compose.isolation.yml logs -f executor
```

* UI:          http://localhost:15173
* Backend API: http://127.0.0.1:18080

The executor log line `executor_readiness ready=True ... probe_status=ok` is the
gate: daemon reachable, the EXACT pinned image present, and the full isolation
probe passed (33 checks, about 2 minutes here).

## 5. Journey 1 - default off (durable on, opt-in OFF)

Register/sign in through the UI, start a NEW chat, and send:

```
Please compute 2 + 2 with Python and show the output.
```

Because the durable flag is on and `run_engine_sandbox` is on while the operator
opt-in is OFF, the durable path refuses BEFORE creating any approval: no
`PendingApproval` row, no `DispatchRecord.started`, no executor call, no container.
The run terminalises `failed` with the bounded reason
`python_host_isolation_unavailable`. That is the fail-closed contract: an approval
is never created when it could not be honoured.

## 6. Journey 2 - opt-in approve

Enable the opt-in for THIS disposable project only:

```
$env:MP4C_UITEST_OPT_IN = "1"
docker compose --env-file secrets/mp4c_uitest.env -p mp4c-uitest -f docker-compose.yml -f docker-compose.isolation.yml up -d
docker compose --env-file secrets/mp4c_uitest.env -p mp4c-uitest -f docker-compose.yml -f docker-compose.isolation.yml ps
```

Start a NEW chat and send the same prompt. Expected flow:

1. The run parks at waiting-for-approval and the approval dialog opens for the
   `python_sandbox` tool.
2. The dialog shows the EXACT bound code that would run, bound to a server-owned
   approval identity.
3. Click **Approve** in the UI.
4. The worker reserves executor capacity, commits the dispatch ledger row, derives
   a short-lived signed grant for exactly that code, and sends it once.
5. The executor verifies the grant, consumes its durable nonce, and launches ONE
   hardened container: `--network none`, read-only root, `--user 65534:65534`,
   `--cap-drop ALL`, `--pids-limit 64`, `--memory 128m`, `--cpus 0.5`, source via
   stdin. It has no Docker socket and no signing secret.
6. The output is sealed as the tool output and the SAME run continues with one
   assistant message.
7. Reload / re-advance: NO second execution (durable nonce store + dispatch ledger).

## 7. Journey 3 - opt-in deny

With the opt-in still on, start another NEW chat and send a second harmless
```print(...)``` proposal, then click **Deny** in the dialog. Expected: zero
executor calls, zero `DispatchRecord.started` rows, zero assistant success
messages for that proposal.

## 8. Journey 4 - opt-in off again

```
Remove-Item Env:MP4C_UITEST_OPT_IN
docker compose --env-file secrets/mp4c_uitest.env -p mp4c-uitest -f docker-compose.yml -f docker-compose.isolation.yml up -d
```

Send the same prompt once more: it must fail closed exactly as in section 5.

## 9. Teardown

```
docker compose --env-file secrets/mp4c_uitest.env -p mp4c-uitest -f docker-compose.yml -f docker-compose.isolation.yml down -v --remove-orphans
Remove-Item secrets/mp4c_uitest.env, secrets/executor_grant_key.mp4c_uitest
```

`-v` removes ONLY the `mp4cuitest_*` volumes of this project. Then re-check that
the pre-start container and volume baseline is unchanged and that no disposable
resource remains.

## 10. Evidence pointers

| Artifact | What it proves |
|---|---|
| `tests/smoke/mp4c_runner_e2e.json` | one RunWorker to gateway to executor to container journey: exactly one send, the exact approved code, one sealed output, one continuation, no duplicate after reload |
| `tests/smoke/mp4c_live_verdict.json` | the executor refuses unsigned, forged and malformed grants and executes each signed grant once |
| `tests/smoke/mp4c_secret_absence.json` | the signing secret is absent from the executed code container (mounts, env names, in-container paths) |
| `tests/smoke/mp4c_isolation_probe.py` | 33 checks, including `docker_socket_absent`, a SEPARATE assertion from the secret check |

## 11. Known limits (do not over-claim)

* Containment is a hardened container on a shared WSL2 kernel, not a virtual
  machine, and not suitable for untrusted multi-tenant execution.
* Executor quotas (1 active per user, 2 globally) are PROCESS-LOCAL: keep exactly
  ONE executor replica.
* There is no atomic transaction across the application database and the executor
  nonce store. The pre-dispatch reservation plus the durable nonce is the
  at-most-once mechanism, and the boundary is stated honestly.
* The Docker socket is mounted ONLY into the disposable executor service, under
  the trusted self-host assumption. Its startup sweep is label-scoped
  (`mp4c.owner=mp4c-uitest-executor`) and cannot match any existing container.
* Approvals expire about 5 minutes after they are requested; an approval left
  undecided terminalises its run as `failed` / `approval_expired`.

## 12. Defect fixed in this slice: Vite dev-proxy starvation (READ THIS)

`frontend/vite.config.ts` configured the `/api` development proxy with
`http.Agent({ keepAlive: false, maxSockets: 1 })`. While a durable SSE stream was
open - exactly the state of a run parked at `waiting_for_approval` - that single
socket was held, so EVERY other proxied request (approval detail, exact-code
preview, health) queued indefinitely. The symptom: the approval dialog stayed at
"Loading approval request..." with Approve/Deny disabled, and the journey could not
be completed in the browser.

Fixed by keeping the dedicated non-reusing agent but removing the socket cap:
`http.Agent({ keepAlive: false })`.

Regression test `frontend/src/__tests__/viteApiProxyConcurrency.test.ts` starts the
REAL Vite dev server against a stub backend that holds `/api/stream` open and
asserts that health, the approval detail and the exact-code preview still complete
within a bounded timeout - with one and with three simultaneous streams. It is RED
against `maxSockets: 1` (`starved: GET /api/health did not complete within 3000ms`)
and GREEN after the fix. If you ever see the dialog stuck loading again, run that
test first: it is the canary for this class of starvation.
