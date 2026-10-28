# Design Spec — MCP Integration (Phase 1, Option A: marker-based dispatch)

- **Date:** 2026-08-29
- **Branch:** `feat/mcp-integration`
- **Status:** Approved design → implementation planning
- **Source of truth:** `THUNDER_AI_PRD.md` (repo root), Feature 2, Phase 1
- **PRD rules in force:** skill selection & consistency; no hallucinated APIs; ask, don't assume.

---

## 1. Context & decisions already made

The existing "agent" is **not** an LLM tool-calling loop. There is no `bind_tools`,
no `ToolNode`, no ReAct agent. The `LLMProvider` interface
(`app/services/llm_providers/base.py`) is text-in / text-out only across all 7
providers. Tools are triggered two ways today:

1. **URL regex** in the user message → `browse_web` node runs `web_scraper` /
   `youtube_summarizer` unconditionally.
2. **Marker strings the LLM writes into its reply** — `[PROPOSED_COMMAND: …]`
   (terminal, human-approved, **non-streaming path only**), `[PYTHON_CODE: …]`
   (sandbox, auto-executed, **both** paths), `[SAVE_MEMORY: …]`.

Decisions (agreed with the user during brainstorming):

| # | Decision |
|---|---|
| D1 | **Option A — marker-based dispatch.** New `[MCP_CALL: …]` marker, parsed and dispatched by a node, mirroring the `[PYTHON_CODE]` single-pass flow. No provider changes. |
| D2 | **stdio transport only**, **Python MCP servers only**, baked into the backend image via `requirements.txt`. No Node / `uv` / SSE this phase. |
| D3 | **Auto-execute**, single-pass, in **both** chat paths. No human approval this phase (streaming HITL is a separate future project). |
| D4 | Safety envelope: master toggle `ENABLE_MCP_TOOL` **default off**; per-server `enabled` flag; per-server tool allow-list enforced **before** any subprocess spawn; servers sandboxed by their launch args; JSON args/env only (no shell string). |
| D5 | A **tool registry** module is introduced as the unified catalog seam so a future Option B (`bind_tools`) loop has one authoritative source. Native tool wiring is **not** refactored this phase. |

---

## 2. Data model

New table **`mcp_servers`**, created by a guarded block in `_migrate_database()`
in `backend/app/main.py` (same style as the existing `scheduled_tasks` block).
New SQLAlchemy model `MCPServer` in `app/models/models.py`. Pydantic schemas
alongside the existing provider schemas.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER FK `users(id)` | scoping, like `user_providers` |
| `name` | VARCHAR | user label; **validated `^[a-z0-9]+(?:_[a-z0-9]+)*$`, max 40 chars** — single underscores only, no leading/trailing/double `_`. It becomes part of the namespaced tool name `mcp__<name>__<tool>`, which is parsed by an unambiguous `split("__")` (see §5), so `__` must never appear inside a name. |
| `command` | VARCHAR | e.g. `python` |
| `args_json` | TEXT | JSON array of strings, e.g. `["-m","mcp_server_sqlite","--db-path","/data/mcp.db"]` |
| `env_json` | TEXT nullable | JSON object of extra env vars |
| `enabled` | BOOLEAN NOT NULL default 1 | per-server on/off |
| `tool_allowlist_json` | TEXT nullable | JSON array of bare tool names; null/empty ⇒ all discovered tools allowed |
| `tools_json` | TEXT nullable | cached discovery: `[{"name","description","inputSchema"}]` |
| `last_discovered_at` | TIMESTAMP nullable | |
| `last_error` | TEXT nullable | last spawn/discovery error string, surfaced in the UI |
| `created_at` / `updated_at` | TIMESTAMP NOT NULL default now | |

Uniqueness: (`user_id`, `name`) unique — enforced in the route layer (mirrors how
`user_providers` handles it) to keep the migration simple.

---

## 3. Async bridge — `backend/app/services/async_bridge.py` (housekeeping, in scope)

Extract the sync/async bridge already written for `web_scraper._scrape_sync` into
a shared helper and repoint `web_scraper` at it:

```python
def run_coro_sync(make_coro: Callable[[], Awaitable[T]]) -> T:
    """Run an async coroutine to completion from sync OR already-async code.

    No loop running -> asyncio.run(). Loop already running (async route) ->
    execute in a one-shot ThreadPoolExecutor worker that owns its own loop;
    Future.result() re-raises so callers' try/except still works.
    """
```

`web_scraper._scrape_sync` becomes a thin call to `run_coro_sync`. Its existing
`__main__` self-check moves/adapts accordingly. No behaviour change.

---

## 4. MCP client service — `backend/app/services/mcp_service.py`

Async core + sync wrappers over `run_coro_sync`. No ORM object crosses into async
code — callers pass a plain dataclass.

```python
@dataclass(frozen=True)
class MCPServerCfg:
    id: int
    name: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str] | None
    tool_allowlist: frozenset[str] | None   # None ⇒ allow all

DISCOVERY_TIMEOUT_S = 20
CALL_TIMEOUT_S = 30            # overridable via settings.mcp_call_timeout_s

async def _discover_async(cfg: MCPServerCfg) -> list[dict]:
    # StdioServerParameters(command=cfg.command, args=list(cfg.args), env=dict(cfg.env or {}))
    # async with stdio_client(params) as (r, w):
    #   async with ClientSession(r, w) as s:
    #     await s.initialize()
    #     res = await s.list_tools()
    #     return [{"name": t.name, "description": t.description or "",
    #              "inputSchema": t.inputSchema or {}} for t in res.tools]

async def _call_async(cfg: MCPServerCfg, tool: str, arguments: dict) -> tuple[str, bool]:
    # same connect/initialise, then:
    #   res = await s.call_tool(tool, arguments, read_timeout_seconds=CALL_TIMEOUT_S)
    #   text = "\n".join(b.text for b in res.content if getattr(b, "type", "") == "text")
    #   return text, bool(res.is_error)

def discover_tools(cfg: MCPServerCfg) -> list[dict]:            # run_coro_sync(lambda: _discover_async(cfg))
def call_tool(cfg: MCPServerCfg, tool: str, arguments: dict) -> tuple[str, bool]
```

**Connection lifecycle:** spawn-per-operation. `stdio_client` is a context
manager that spawns the subprocess and is cancellation-shielded on exit — no
persistent pool, no lifecycle bugs. Cost: one subprocess spawn per discovery and
per tool call. Acceptable for this phase.

**Verified SDK surface (context7, `/modelcontextprotocol/python-sdk`):**
`from mcp import ClientSession, StdioServerParameters`,
`from mcp.client.stdio import stdio_client`,
`stdio_client(params) -> async ctx yielding (read, write)`,
`ClientSession(read, write)` async ctx, `await session.initialize()`,
`await session.list_tools() -> ListToolsResult` (`.tools`: list of `Tool` with
`.name`, `.description`, `.inputSchema`),
`await session.call_tool(name, arguments, read_timeout_seconds=…) -> CallToolResult`
(`.content`: list of content blocks, `TextContent.text`; `.is_error`: bool).

**Verified 2026-08-29 (in-container stdio spike):** `mcp==1.12.4` + `mcp-server-fetch==2025.4.7` (tool `fetch`, protocol `2025-06-18`); error flag attribute is **`isError`** — `CallToolResult` fields: `meta, content, structuredContent, isError` (so use `res.isError`, not `res.is_error`); `list_tools()` `Tool` fields: `name, title, description, inputSchema, outputSchema, annotations, meta`.

---

## 5. Tool registry — `backend/app/services/tool_registry.py` (the seam)

```python
@dataclass(frozen=True)
class RegisteredTool:
    name: str            # native: "web_scraper"; mcp: "mcp__<server>__<tool>"
    description: str
    input_schema: dict
    source: str          # "native" | "mcp"
    server_id: int | None

_NATIVE_DESCRIPTORS: list[RegisteredTool]   # static: web_scraper, youtube_summarizer, python_sandbox, terminal

def list_tools(db, user_id: int) -> list[RegisteredTool]:
    tools = list(_NATIVE_DESCRIPTORS)
    if settings.enable_mcp_tool:
        for row in _enabled_mcp_servers(db, user_id):        # enabled == True, tools_json present
            for t in json.loads(row.tools_json or "[]"):
                if _allowed(row, t["name"]):
                    tools.append(RegisteredTool(
                        name=f"mcp__{row.name}__{t['name']}",
                        description=t.get("description", ""),
                        input_schema=t.get("inputSchema", {}),
                        source="mcp", server_id=row.id))
    return tools

def resolve_mcp(db, user_id: int, namespaced: str) -> tuple[MCPServerCfg, str] | None:
    # parts = namespaced.split("__")  ->  ["mcp", "<server>", "<tool>"]
    # if len(parts) != 3 or parts[0] != "mcp": return None
    # look up enabled row for (user_id, parts[1]); tool = parts[2] must be in
    # tools_json and allow-listed  ->  (MCPServerCfg, tool)  else None
```

Namespacing rule: `"mcp__" + server_name + "__" + tool_name`. Parsing is an
unambiguous `namespaced.split("__")` that must yield exactly
`["mcp", server, tool]`. Server `name` is constrained (see §2) to single
underscores only, so it can never contain `__`. **Known limitation (accepted):**
an MCP tool whose own name contains `__` is not addressable this phase (rare in
practice) — the registry skips it with a logged warning. Single `_` and `-`
inside a tool name are fine.

**Phase 1 reality:** only `source=="mcp"` tools are dispatched via the new
`[MCP_CALL]` marker. Native tools keep their current triggers untouched; the
registry lists them only so the future Option B loop has one source.

---

## 6. Marker protocol & dispatch

### 6.1 Marker

```
[MCP_CALL: mcp__<server>__<tool> {"arg": "value"}]
```

- The model is instructed (system prompt, §7) to emit the marker **alone on its own line**.
- Parser: `re.compile(r"\[MCP_CALL:\s*(mcp__[a-z0-9_]+(?:__[A-Za-z0-9_-]+)+)\s*(\{.*\})?\s*\]")`,
  applied **per line**, the args group greedy to the last `}` on that line.
  Missing `{…}` ⇒ `{}`. The captured name is then validated by
  `resolve_mcp` via `split("__")` (§5), which is the authoritative check.
  **Known limitation (accepted):** one call per line; the JSON args object must
  fit entirely on the marker's line (nesting within that line is fine).
- Cap: `MAX_MCP_CALLS_PER_TURN = 3` (mirrors `browse_web`'s 3-URL cap).

### 6.2 `backend/app/tools/mcp_tool.py`

```python
def extract_mcp_calls(text: str) -> list[tuple[str, dict]]        # [(namespaced_name, args), ...], capped, malformed skipped
def format_mcp_result(name: str, text: str, is_error: bool) -> str
    # "=== MCP Tool Result (<name>) ===\n<text or [error] msg>\n=== End of MCP Tool Result ==="
def run_mcp_calls(calls, db, user_id: int) -> str:
    # for each (name, args):
    #   hit = tool_registry.resolve_mcp(db, user_id, name)
    #   if hit is None: append error block "unknown / disabled / not allow-listed", continue
    #   cfg, bare = hit
    #   try: text, is_err = mcp_service.call_tool(cfg, bare, args)
    #   except Exception as e: text, is_err = f"{type(e).__name__}: {e}", True
    #   append format_mcp_result(name, text, is_err)
    # return "\n\n".join(blocks)
```

Allow-list / disabled / unknown are all rejected inside `resolve_mcp`
**before** `mcp_service.call_tool` — no subprocess spawns for a rejected call.

### 6.3 Wiring — mirrors `[PYTHON_CODE]` exactly

**Non-streaming** (`langgraph_workflow.generate_answer`): after the response is
produced and `[SAVE_MEMORY]` processed, if `settings.enable_mcp_tool` and
`extract_mcp_calls(response)` is non-empty and not already regenerating and no
`pending_command`:
- `state["mcp_result"] = run_mcp_calls(calls, db, user_id)`
- `state["regenerate"] = True`
- save a placeholder assistant message (same as the `[PYTHON_CODE]` branch does)

`_build_system_prompt` gains an `mcp_result` block (rendered when
`state.get("mcp_result")`), so the regenerate pass shows the model the tool output.
`WorkflowState` gains `mcp_result: Optional[str]`.

**Streaming** (`streaming_service.stream_chat_response`, `done` branch, right after
the existing `extract_python_code` block): if `settings.enable_mcp_tool` and
`extract_mcp_calls(full_response_text)`:
- `full_response_text += "\n\n" + run_mcp_calls(calls, db, context_user_id)`

`prepare_chat_context` must stash `user_id` on `ChatContext` (currently the
streaming route passes `user_id` separately) — add `ChatContext.user_id`.

**Streaming limitation (explicit, accepted):** exactly like `[PYTHON_CODE]` today,
the streaming path *appends* the tool result to the assistant message but does
**not** re-invoke the model within the same turn. The user sees the result; the
model acts on it only on the next message. The non-streaming path *does* give the
model the result via `regenerate`. Unifying this is out of scope (needs streaming
HITL / multi-pass streaming — a later phase).

---

## 7. System-prompt integration

`build_base_prompt()` in `app/services/system_prompts.py` gains an optional
`mcp_tools: list[RegisteredTool] | None = None`. When non-empty, append:

```
## MCP Tools
You may call these external tools. To call one, output EXACTLY this, alone on its own line:
[MCP_CALL: <tool_name> {"arg": "value"}]
The tool result is returned to you; then continue your answer.

Available tools:
- mcp__local_sqlite__read_query — Run a read-only SQL query. Input: {"query": string}
- mcp__local_sqlite__list_tables — List tables. Input: {}
- ...
```

Built from `tool_registry.list_tools(db, user_id)` filtered to `source=="mcp"`.
Callers: `streaming_service.prepare_chat_context` and
`langgraph_workflow._build_system_prompt`. Native-tool prompt text unchanged.
When `settings.enable_mcp_tool` is false or no MCP tools are available, the block
is omitted entirely.

---

## 8. Settings API + UI

### 8.1 API — `backend/app/routes/mcp.py` (mirrors `routes/providers.py`), registered in `main.py`

| method | path | purpose |
|---|---|---|
| GET | `/api/mcp/servers` | list current user's servers (incl. cached tool names, `enabled`, `last_error`) |
| POST | `/api/mcp/servers` | create (validates `name` regex, `args_json` is a JSON string array, `env_json` a JSON object) |
| PUT | `/api/mcp/servers/{id}` | update name / command / args / env / enabled / allow-list |
| DELETE | `/api/mcp/servers/{id}` | delete |
| POST | `/api/mcp/servers/{id}/discover` | spawn now, refresh `tools_json` + `last_discovered_at` or set `last_error`; return the tool list |

All routes: `Depends(get_current_user)` + `Depends(require_csrf)` + user scoping,
exactly like `routes/providers.py`. `/discover` is synchronous from the client's
POV; internally uses `mcp_service.discover_tools` (which bridges to async).

### 8.2 UI — `frontend/src/McpServers.tsx`, mounted in the existing Settings screen

Styled like the provider manager:
- Master **"Enable MCP tools"** switch (bound to the `ENABLE_MCP_TOOL` setting via
  the existing settings API — see §9 note).
- List of servers with `enabled` toggle, discovered-tool count, `last_error` badge.
- Add/edit form: `name`, `command`, `args` as a **JSON array textarea** (stored
  verbatim as `args_json`; validated as a JSON string array on submit — no
  whitespace/quote parsing), optional `env` as a JSON object textarea, `enabled`
  toggle, allow-list multi-select populated from discovered tools.
- **"Test / Refresh tools"** button → `POST /discover`, shows resulting tools or
  `last_error`.
- New calls in `frontend/src/api.ts`.

---

## 9. Config & safety

- `settings.enable_mcp_tool` (env `ENABLE_MCP_TOOL`, **default `false`**) added to
  `app/config.py` and `docker-compose.yml` `backend.environment`, mirroring
  `ENABLE_TERMINAL_TOOL`. When false: registry adds no MCP tools, system prompt
  block omitted, `extract_mcp_calls` dispatch guarded off (markers left as text).
- `settings.mcp_call_timeout_s` (env, default 30).
- Per-server `enabled` and `tool_allowlist_json` enforced in `resolve_mcp` before
  any spawn.
- Servers sandboxed via launch args; UI helper text steers filesystem/sqlite
  paths to `/data`.
- `args`/`env` are JSON array/object → passed as `StdioServerParameters(args=[...],
  env={...})`; **never** a shell string, no `shell=True`, no interpolation.
- **Master-toggle note:** `ENABLE_MCP_TOOL` is an env-var/process setting. The UI
  switch reads/writes it through whatever mechanism the existing settings system
  uses for such flags; if `ENABLE_TERMINAL_TOOL` is env-only with no runtime
  toggle, the MCP switch is display-only + docs, and enabling is a compose env
  change. **Confirm the existing pattern during implementation step 8**; do not
  invent a settings-persistence mechanism.

---

## 10. Error handling — every failure returns text to the model, never a 500

| failure | behaviour |
|---|---|
| spawn / `initialize` fails during `/discover` | store `last_error`, return 200 with `{error}`; UI shows badge |
| spawn / `initialize` fails during dispatch | inject `=== MCP Tool Result (x) === [error] <msg> === End ===` |
| tool unknown / server disabled / not allow-listed | injected error block, **no spawn** |
| malformed JSON args | injected error block asking the model to re-emit the marker |
| `result.is_error` true | inject the server's error `content` text as-is, marked `[error]` |
| `call_tool` timeout | injected timeout block |
| `ENABLE_MCP_TOOL` false | markers ignored, left verbatim in the reply (documented) |

---

## 11. Testing

| file | covers |
|---|---|
| `tests/fixtures/stub_mcp_server.py` | ~15-line real stdio MCP server exposing one `echo` tool (uses the `mcp` SDK server API) |
| `tests/test_async_bridge.py` | `run_coro_sync` from no-loop and in-loop contexts; exception propagation |
| `tests/test_mcp_service.py` | real subprocess: `discover_tools` returns `echo`; `call_tool` round-trips; bad command → error; timeout |
| `tests/test_tool_registry.py` | flag off ⇒ native only; flag on ⇒ native + mcp; allow-list filters; `resolve_mcp` round-trip incl. tool names with `-`/`_`; unknown/disabled ⇒ None |
| `tests/test_mcp_tool.py` | marker regex (whitespace, no-args, multiple lines, malformed, cap of 3); `format_mcp_result`; `run_mcp_calls` with monkeypatched `mcp_service.call_tool` |
| `tests/test_mcp_routes.py` | CRUD + `/discover`, auth + CSRF, user scoping, `name` validation (mirrors the providers route tests) |
| dispatch | graph test: `generate_answer` sets `regenerate` + `mcp_result` when a marker is present (monkeypatch `mcp_service`); streaming test: result appended to `full_response_text` |

Each new non-trivial module also carries a `__main__` / `_self_check()` (project convention).

---

## 12. Build sequence

1. `requirements.txt`: add `mcp` + reference Python servers; **verify exact
   package + module names on PyPI**; rebuild backend; confirm `python -m <module>`
   launches each server.
2. `async_bridge.py` + `tests/test_async_bridge.py`; repoint `web_scraper`.
3. `MCPServer` model + `_migrate_database()` block + Pydantic schemas.
4. `mcp_service.py` + `tests/fixtures/stub_mcp_server.py` + `tests/test_mcp_service.py`.
5. `tool_registry.py` + `tests/test_tool_registry.py`.
6. `tools/mcp_tool.py` + `tests/test_mcp_tool.py`.
7. `routes/mcp.py` + register in `main.py` + `tests/test_mcp_routes.py`.
8. `config.py` + `docker-compose.yml`: `ENABLE_MCP_TOOL`, `MCP_CALL_TIMEOUT_S`;
   **confirm the settings-toggle pattern** (§9 note).
9. `system_prompts.py` block; wire `prepare_chat_context` + `_build_system_prompt`;
   add `ChatContext.user_id`, `WorkflowState.mcp_result`.
10. Dispatch wiring in `generate_answer` + `stream_chat_response` +
    `_build_system_prompt` regenerate block + dispatch tests.
11. Frontend: `api.ts` calls, `McpServers.tsx`, mount in Settings.
12. Full `docker compose up -d --build`; manual end-to-end with the baked sqlite
    server (default off → enable → add server → discover → ask a question that
    triggers `[MCP_CALL]`); update `THUNDER_AI_PRD.md` Phase 1 status.

---

## 13. Files

**New:** `services/mcp_service.py`, `services/tool_registry.py`,
`services/async_bridge.py`, `tools/mcp_tool.py`, `routes/mcp.py`,
`tests/fixtures/stub_mcp_server.py`, `tests/test_async_bridge.py`,
`tests/test_mcp_service.py`, `tests/test_tool_registry.py`,
`tests/test_mcp_tool.py`, `tests/test_mcp_routes.py`,
`frontend/src/McpServers.tsx`.

**Touched:** `models/models.py`, `main.py` (migration + router include),
`config.py`, `docker-compose.yml`, `requirements.txt`,
`services/system_prompts.py`, `services/streaming_service.py`,
`services/langgraph_workflow.py`, `tools/web_scraper.py`,
`frontend/src/api.ts`, the Settings screen component, `THUNDER_AI_PRD.md`.

---

## 14. Out of scope / deferred

Remote / SSE transport · Node / `uv` servers · MCP resources & prompts (tools
only) · streaming HITL approval for `[MCP_CALL]` · multi-pass streaming so the
model acts on tool output mid-stream · real `bind_tools` function-calling
(Option B) · persistent MCP connection pool · per-tool rate limiting.
