# D6B deterministic real-browser durable-chat smoke

Phase 1B.2R MP-3C3D6B. Test-only. Nothing here is imported by `app/**`, and
nothing here is reachable from a production startup path.

## What it proves

One deterministic, offline, free vertical slice, driven through a real browser:

```
Browser UI -> real login/auth/CSRF -> real /messages/stream durable routing
  -> real durable Run submission -> real lifespan-started RunWorkerSupervisor
  -> real RunWorker with an INJECTED deterministic provider
  -> real durable text RunEvents -> real SSE delivery
  -> progressive rendering -> persisted terminal run
  -> exactly one final assistant Message
```

## Running it

```powershell
PYTHONPATH=backend .venv/Scripts/python.exe tests/smoke/run_smoke.py
```

The orchestrator starts the real backend and the real Vite dev server on
loopback, seeds the account through the real registration route, writes a
credential-free manifest, and then waits for the external Chromium controller.

Because the browser is driven by the harness MCP controller (not by this
process), the controller must:

1. read the manifest at `<smoke_root>/manifest.json`;
2. perform the journey against `frontend_url`;
3. write `<smoke_root>/browser_result.json` (see the shape below);
4. create the empty file `<smoke_root>/browser_done` (or `browser_abort` to fail).

The orchestrator then runs the direct-database assertions, tears everything
down in a `finally`, and exits non-zero if any invariant failed.

## Non-secret test fixture

The smoke account exists **only** in a throwaway SQLite database under the
temporary smoke root, which is deleted on exit. Its credentials are a fixed,
intentionally non-secret placeholder:

| Field | Value |
|---|---|
| username | `d6b_smoke_user` |
| password | `d6b-smoke-password-1` |

This is `SMOKE_PASSWORD` in `run_smoke.py`. It is deliberately NOT written to the
manifest, so the manifest stays credential-free as required. It is not a secret
for any real system and grants access to nothing outside the temp database.

## `browser_result.json` shape

```json
{
  "rendered_text": "<exact text content of the final assistant bubble>",
  "saw_partial_render": true,
  "completed": true,
  "console_errors": [],
  "page_errors": []
}
```

`rendered_text` is compared **exactly** against the deterministic fake response.
The fake response is deliberately markdown-neutral (no `* _ # > | ~` backticks, no
brackets, no newlines, no repeated spaces, no leading/trailing whitespace), so
browser whitespace normalisation is a no-op and the comparison stays exact.

## Why there is no production backdoor

The fake provider is installed by mutating `app.state.run_worker_supervisor`
in-process, before the real FastAPI lifespan starts. That works because of three
seams that already existed in shipped code:

1. `main.py` already adopts a pre-set `app.state.run_worker_supervisor`;
2. `RunWorkerSupervisor(worker_factory=...)`;
3. `RunWorker(chat_provider=...)`.

None of these is reachable from an environment variable, a config field, a route,
a header, a request body, or any production startup path. `durable_smoke_app.py`
additionally fails closed unless `THUNDER_SMOKE_ROOT` is set AND the configured
SQLite database lives inside it, so it cannot be pointed at real user data.

## Offline guarantee

`fakes.NetworkTripwire` is installed in the backend process. It **blocks and
records** every non-loopback connection and every connection to a blocked local
service port (Ollama 11434/11435, ChromaDB 8000). A non-empty `tripwire.jsonl` is
a hard failure. Loopback traffic needed by the browser, backend and Vite dev
server is unaffected.

The backend child is started with `cwd` set to the smoke root, so the repository
`.env` is never loaded and the developer real provider keys are never read.
