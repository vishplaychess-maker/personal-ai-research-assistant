# MCP Integration (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Thunder AI agent call tools exposed by user-configured local (stdio) MCP servers, via a `[MCP_CALL: …]` marker the LLM emits, with a tool-registry seam that a future `bind_tools` loop can reuse.

**Architecture:** Marker-based dispatch mirroring the existing `[PYTHON_CODE]` flow — no LLM function-calling. A new `mcp_service` spawns stdio MCP server subprocesses per operation (discover / call) using the `mcp` Python SDK; a `tool_registry` merges native tool descriptors with per-user MCP tool descriptors; a `mcp_tool` module parses markers and dispatches through the registry. Servers are Python-only, baked into the backend image. Feature is off by default (`ENABLE_MCP_TOOL`), servers are user-scoped CRUD rows sandboxed by their launch args, and each server has a tool allow-list enforced before any subprocess spawns.

**Tech Stack:** FastAPI, SQLAlchemy (SQLite), LangGraph 0.3.2, the `mcp` Python SDK (stdio client), React 18 + Vite + TypeScript, shadcn/ui, pytest 9.1.1 (run inside the backend container).

**Spec:** `docs/superpowers/specs/2026-08-29-mcp-integration-design.md` — read it alongside this plan.

## Global Constraints

- **Branch:** all work on `feat/mcp-integration` (already checked out, based on `4da24e1`).
- **Backend runs in Docker; no source bind-mount.** Every backend code change requires `docker compose up -d --build backend` before it takes effect. Frontend has a bind-mount + Vite HMR (`src/` and `index.html` hot-reload; `vite.config.ts` needs a container restart).
- **Tests run inside the container:** `docker compose exec -T backend pytest tests/<file> -v`. Test files live at repo-root `tests/`, not `backend/tests/`. `pytest-asyncio` is NOT installed — every test function is synchronous.
- **Migrations are hand-rolled** in `_migrate_database()` in `backend/app/main.py`, run on startup in `lifespan`. New tables get a guarded `CREATE TABLE … IF` block there (see the existing `scheduled_tasks` block, `backend/app/main.py:113-135`). `Base.metadata.create_all()` only creates *missing* tables.
- **Feature flag:** nothing MCP may load, appear in a system prompt, or dispatch unless `settings.enable_mcp_tool` is true. Default is `False`.
- **No shell strings:** MCP server `args`/`env` are JSON array/object → `StdioServerParameters(args=[...], env={...})`. Never `shell=True`, never string interpolation.
- **Namespaced tool name:** `"mcp__" + server_name + "__" + tool_name`. Server `name` is validated `^[a-z0-9]+(?:_[a-z0-9]+)*$` (≤40 chars) so it never contains `__`. Parsing is `name.split("__")` → exactly `["mcp", server, tool]`.
- **`mcp` SDK version:** pin an exact `mcp` version in `requirements.txt` in Task 1 and write client code against **that** version's API. This plan targets the **v1.x** client idiom (`from mcp import ClientSession, StdioServerParameters`; `from mcp.client.stdio import stdio_client`). If Task 1's spike shows the installed SDK is 2.x, pin `mcp<2` instead (v1 is fully supported) rather than rewriting to the v2 `Client` API.
- **Every new non-trivial module** carries a `_self_check()` / `__main__` block (existing repo convention, e.g. `backend/app/services/cache_service.py`, `backend/app/tools/web_scraper.py`).
- **Commit after every task.** Commit message trailers (from repo policy):
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_016QBfRnkoLi7oeZ1ZF37PdB
  ```

---

## File Structure

**New files**

| Path | Responsibility |
|---|---|
| `backend/app/services/async_bridge.py` | `run_coro_sync()` — run a coroutine to completion from sync OR already-async code. Extracted from `web_scraper`. |
| `backend/app/services/mcp_service.py` | Spawn stdio MCP servers, `initialize`, `list_tools`, `call_tool`. Async core + sync wrappers over `run_coro_sync`. `MCPServerCfg` dataclass. |
| `backend/app/services/tool_registry.py` | `RegisteredTool` descriptor; `list_tools(db, user_id)` merging native + MCP; `resolve_mcp(db, user_id, namespaced)`. |
| `backend/app/tools/mcp_tool.py` | `extract_mcp_calls()`, `format_mcp_result()`, `run_mcp_calls()` — marker parse + dispatch. |
| `backend/app/routes/mcp.py` | CRUD + `/discover` for `mcp_servers`, user-scoped, CSRF-protected. Pydantic schemas inline (matches `routes/providers.py`). |
| `tests/fixtures/__init__.py` | empty — make `tests/fixtures` a package. |
| `tests/fixtures/stub_mcp_server.py` | ~15-line real stdio MCP server exposing one `echo` tool, for `mcp_service` tests. |
| `tests/test_async_bridge.py` | `run_coro_sync` unit tests. |
| `tests/test_mcp_service.py` | `discover_tools` / `call_tool` against the stub server (real subprocess). |
| `tests/test_tool_registry.py` | flag on/off, allow-list filtering, `resolve_mcp` round-trip. |
| `tests/test_mcp_tool.py` | marker regex, result formatting, `run_mcp_calls` with a monkeypatched `mcp_service`. |
| `tests/test_mcp_routes.py` | CRUD + `/discover` via `TestClient`, auth + CSRF + user scoping. |
| `frontend/src/McpServers.tsx` | Settings tab: server list, add/edit dialog, discover button, master toggle. |

**Modified files**

| Path | Change |
|---|---|
| `backend/requirements.txt` | add `mcp==<pinned>` + 1 Python reference server. |
| `backend/app/tools/web_scraper.py` | `_scrape_sync` delegates to `async_bridge.run_coro_sync`; move its bridge self-check. |
| `backend/app/models/models.py` | new `MCPServer` model. |
| `backend/app/main.py` | `mcp_servers` migration block; import + `include_router` for the mcp router. |
| `backend/app/config.py` | `enable_mcp_tool: bool = False`; `mcp_call_timeout_s: int = 30`; `mcp_discovery_timeout_s: int = 20`. |
| `docker-compose.yml` | `ENABLE_MCP_TOOL=${ENABLE_MCP_TOOL:-false}` under `backend.environment`. |
| `backend/app/services/system_prompts.py` | `build_mcp_tools_block(tools) -> str`. |
| `backend/app/services/streaming_service.py` | `ChatContext.user_id`; append MCP block in `prepare_chat_context`; dispatch `[MCP_CALL]` in the `done` branch. |
| `backend/app/services/langgraph_workflow.py` | `WorkflowState.mcp_result`; append MCP block + `mcp_result` block in `_build_system_prompt`; dispatch `[MCP_CALL]` in `generate_answer`. |
| `frontend/src/api.ts` | `API` methods for `/api/mcp/servers*`. |
| `frontend/src/types.ts` | `McpServer`, `McpServerCreate`, `McpTool` types. |
| `frontend/src/Settings.tsx` | third tab `"mcp"` mounting `<McpServers/>`. |
| `THUNDER_AI_PRD.md` | Phase 1 status → done. |

---

## Task 1: Pin the `mcp` SDK + a reference server, verify in-container

**Files:**
- Modify: `backend/requirements.txt`

**Interfaces:**
- Produces: a rebuilt backend image where `import mcp` works and at least one Python MCP server module runs over stdio. Records the verified client API (v1 `ClientSession`/`stdio_client` vs v2 `Client`) for Task 4.

- [ ] **Step 1: Find the current `mcp` release**

Run:
```bash
docker compose exec -T backend pip index versions mcp 2>/dev/null || \
docker compose run --rm --no-deps backend python -m pip index versions mcp
```
Note the latest `1.x` version (e.g. `1.12.4`). Prefer the newest `1.x`. If only `2.x` exists, use `mcp<2` and still pin the resolved 1.x in a comment.

- [ ] **Step 2: Pick a reference server package**

Candidates, in order of preference (pick the first that installs cleanly and exposes `tools`):
- `mcp-server-fetch` — module `mcp_server_fetch`, no extra args, exposes a `fetch` tool. Good default (no DB path needed).
- `mcp-server-sqlite` — module `mcp_server_sqlite`, needs `--db-path`.
- `mcp-server-time` — module `mcp_server_time`.

Verify the exact PyPI name + import module before writing it down:
```bash
docker compose run --rm --no-deps backend python -m pip install mcp-server-fetch && \
docker compose run --rm --no-deps backend python -c "import mcp_server_fetch; print('ok')"
```

- [ ] **Step 3: Edit `backend/requirements.txt`**

Add after the existing `langchain-core` line (keep the file's alphabetical-ish grouping loose, matching current style):
```
# ── MCP (Model Context Protocol) — Phase 1 (stdio, Python servers only) ──
mcp==1.12.4                 # <- replace with the version resolved in Step 1
mcp-server-fetch==2025.4.7  # <- replace with the exact version resolved in Step 2
```

- [ ] **Step 4: Rebuild the backend image**

Run: `docker compose up -d --build backend`
Expected: build succeeds; `docker compose ps` shows `backend` healthy.

- [ ] **Step 5: Spike the client API (throwaway, do not commit the spike file)**

Create `backend/_mcp_spike.py` (temporary):
```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command="python", args=["-m", "mcp_server_fetch"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("protocol:", init.protocol_version if hasattr(init, "protocol_version") else init)
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            # Confirm the CallToolResult attribute name for the error flag:
            print("CallToolResult fields:", __import__("mcp").types.CallToolResult.model_fields.keys())

asyncio.run(main())
```
Run: `docker compose exec -T backend python /app/_mcp_spike.py`
Expected: prints a protocol version and a non-empty tool list (e.g. `['fetch']`), and the `CallToolResult` field names (note whether the error flag is `isError` or `is_error`).

- [ ] **Step 6: Delete the spike, record findings**

Run: `rm backend/_mcp_spike.py`
In `docs/superpowers/specs/2026-08-29-mcp-integration-design.md`, under §4, append a short "Verified <date>: mcp==X, error flag attribute = `isError|is_error`, tool listing shape = `result.tools[].{name,description,inputSchema}`." (One line.)

- [ ] **Step 7: Commit**

```bash
git add backend/requirements.txt docs/superpowers/specs/2026-08-29-mcp-integration-design.md
git commit -m "feat(mcp): pin mcp SDK + reference server, verify stdio client in-container"
```

---

## Task 2: `async_bridge.run_coro_sync()` (extract from web_scraper)

**Files:**
- Create: `backend/app/services/async_bridge.py`
- Modify: `backend/app/tools/web_scraper.py` (imports; `_scrape_sync` body; move the `__main__` bridge check)
- Test: `tests/test_async_bridge.py`

**Interfaces:**
- Produces: `run_coro_sync(make_coro: Callable[[], Awaitable[T]]) -> T` — runs `make_coro()` to completion. No running loop → `asyncio.run`. Running loop → one-shot `ThreadPoolExecutor` worker with its own loop; `Future.result()` re-raises.

- [ ] **Step 1: Write the failing test — `tests/test_async_bridge.py`**

```python
import asyncio

from app.services.async_bridge import run_coro_sync


async def _ok(x):
    await asyncio.sleep(0)
    return x * 2


async def _boom():
    await asyncio.sleep(0)
    raise ValueError("kaboom")


def test_runs_from_sync_context():
    assert run_coro_sync(lambda: _ok(21)) == 42


def test_runs_from_inside_running_loop():
    async def driver():
        # We are on a running loop here; run_coro_sync must offload to a thread.
        return await asyncio.get_running_loop().run_in_executor(
            None, run_coro_sync, lambda: _ok(5)
        )

    assert asyncio.run(driver()) == 10


def test_exception_propagates():
    try:
        run_coro_sync(lambda: _boom())
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert str(exc) == "kaboom"
```

- [ ] **Step 2: Run — expect failure**

Run: `docker compose exec -T backend pytest tests/test_async_bridge.py -v`
Expected: `ModuleNotFoundError: No module named 'app.services.async_bridge'`

- [ ] **Step 3: Create `backend/app/services/async_bridge.py`**

```python
"""Run an async coroutine to completion from sync OR already-async code.

No event loop running (sync route, threadpool, CLI, tests) -> asyncio.run().
A loop already running (async FastAPI route) -> asyncio.run() would raise, so
execute the coroutine in a one-shot worker thread that owns its own loop.
Future.result() re-raises any exception, so callers' try/except still works.
"""

import asyncio
import concurrent.futures
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


def run_coro_sync(make_coro: Callable[[], Awaitable[T]]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coro())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(make_coro())).result()


if __name__ == "__main__":
    async def _double(n):
        await asyncio.sleep(0)
        return n * 2

    assert run_coro_sync(lambda: _double(3)) == 6

    async def _from_loop():
        return await asyncio.get_running_loop().run_in_executor(
            None, run_coro_sync, lambda: _double(4)
        )

    assert asyncio.run(_from_loop()) == 8
    print("async_bridge self-check OK")
```

- [ ] **Step 4: Run — expect pass**

Run: `docker compose exec -T backend pytest tests/test_async_bridge.py -v` (after `docker compose up -d --build backend`)
Expected: 3 passed.

- [ ] **Step 5: Repoint `web_scraper._scrape_sync`**

In `backend/app/tools/web_scraper.py`:
- remove `import concurrent.futures` (no longer used there) — keep `import asyncio` (still used by `_scrape_async` and the `__main__` block).
- add `from app.services.async_bridge import run_coro_sync`.
- replace the `_scrape_sync` body:
```python
def _scrape_sync(url: str) -> str:
    """Run the async scraper from either a sync or an already-async context."""
    return run_coro_sync(lambda: _scrape_async(url))
```
- leave the existing `__main__` bridge self-check in `web_scraper.py` as-is (it still exercises `_scrape_sync` end to end).

- [ ] **Step 6: Rebuild + verify both self-checks and the scraper still imports**

Run:
```bash
docker compose up -d --build backend
docker compose exec -T backend python -m app.services.async_bridge
docker compose exec -T backend python -m app.tools.web_scraper
docker compose exec -T backend pytest tests/test_async_bridge.py -v
```
Expected: both print `… self-check OK`; pytest 3 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/async_bridge.py backend/app/tools/web_scraper.py tests/test_async_bridge.py
git commit -m "refactor(mcp): extract run_coro_sync into async_bridge, repoint web_scraper"
```

---

## Task 3: `MCPServer` model + migration

**Files:**
- Modify: `backend/app/models/models.py` (new `MCPServer` class after `ScheduledTask`)
- Modify: `backend/app/main.py` (`_migrate_database()` — new `mcp_servers` block)
- Test: `tests/test_mcp_routes.py` (create the file with just a schema/migration smoke test now; CRUD tests come in Task 9)

**Interfaces:**
- Produces: table `mcp_servers` and ORM model `MCPServer` with columns:
  `id:int pk`, `user_id:int fk users.id`, `name:str`, `command:str`, `args_json:str` (JSON array), `env_json:str|None` (JSON object), `enabled:bool=1`, `tool_allowlist_json:str|None` (JSON array), `tools_json:str|None` (JSON array of `{name,description,inputSchema}`), `last_discovered_at:datetime|None`, `last_error:str|None`, `created_at`, `updated_at`.

- [ ] **Step 1: Write the failing test — `tests/test_mcp_routes.py`**

```python
"""MCP server CRUD + discovery route tests. Run inside the backend container:
    docker compose exec -T backend pytest tests/test_mcp_routes.py -v
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.main import app
from app.database import engine, SessionLocal
from app.models.models import MCPServer, User


@pytest.fixture
def client():
    with TestClient(app) as c:      # context manager runs lifespan -> init_db + _migrate_database
        yield c


@pytest.fixture(autouse=True)
def _cleanup(client):
    db = SessionLocal()
    try:
        db.query(MCPServer).delete(synchronize_session=False)
        db.query(User).filter(User.username.like("mcpt_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    yield
    db = SessionLocal()
    try:
        db.query(MCPServer).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_mcp_servers_table_exists(client):
    cols = {c["name"] for c in inspect(engine).get_columns("mcp_servers")}
    assert {
        "id", "user_id", "name", "command", "args_json", "env_json", "enabled",
        "tool_allowlist_json", "tools_json", "last_discovered_at", "last_error",
        "created_at", "updated_at",
    } <= cols
```

- [ ] **Step 2: Run — expect failure**

Run: `docker compose exec -T backend pytest tests/test_mcp_routes.py -v`
Expected: `ImportError: cannot import name 'MCPServer'` (or table-missing error).

- [ ] **Step 3: Add the model — `backend/app/models/models.py`**

After the `ScheduledTask` class (end of file), add:
```python
class MCPServer(Base):
    """A user-configured stdio MCP server (Model Context Protocol)."""

    __tablename__ = "mcp_servers"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(40), nullable=False)          # ^[a-z0-9]+(?:_[a-z0-9]+)*$
    command = Column(String(255), nullable=False)
    args_json = Column(Text, nullable=False, default="[]")        # JSON array of strings
    env_json = Column(Text, nullable=True)                        # JSON object or null
    enabled = Column(Boolean, nullable=False, default=True)
    tool_allowlist_json = Column(Text, nullable=True)             # JSON array of bare tool names or null
    tools_json = Column(Text, nullable=True)                      # JSON array of {name,description,inputSchema}
    last_discovered_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    def __repr__(self):
        return f"<MCPServer(id={self.id}, name='{self.name}', enabled={self.enabled})>"
```

- [ ] **Step 4: Add the migration block — `backend/app/main.py`**

In `_migrate_database()`, immediately after the existing `scheduled_tasks` block (right after its `CREATE INDEX ix_scheduled_tasks_session_id …` line and before the trailing `conn.commit()` at the end of the function), add:
```python
        if "mcp_servers" not in existing_tables:
            conn.execute(sa_text("""
                CREATE TABLE mcp_servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name VARCHAR(40) NOT NULL,
                    command VARCHAR(255) NOT NULL,
                    args_json TEXT NOT NULL DEFAULT '[]',
                    env_json TEXT,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    tool_allowlist_json TEXT,
                    tools_json TEXT,
                    last_discovered_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """))
            conn.execute(sa_text("CREATE INDEX ix_mcp_servers_user_id ON mcp_servers (user_id)"))
```
`existing_tables` is already defined earlier in the function (`existing_tables = inspector.get_table_names()`); if the linter flags it as possibly-undefined at this point, re-fetch: `existing_tables = inspector.get_table_names()` on the line before the `if`.

- [ ] **Step 5: Rebuild + run the test**

Run:
```bash
docker compose up -d --build backend
docker compose exec -T backend pytest tests/test_mcp_routes.py -v
```
Expected: `test_mcp_servers_table_exists` passes.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/models.py backend/app/main.py tests/test_mcp_routes.py
git commit -m "feat(mcp): add MCPServer model and mcp_servers migration"
```

---

## Task 4: `mcp_service` — connect, discover, call

**Files:**
- Create: `backend/app/services/mcp_service.py`
- Create: `tests/fixtures/__init__.py` (empty)
- Create: `tests/fixtures/stub_mcp_server.py`
- Test: `tests/test_mcp_service.py`

**Interfaces:**
- Consumes: `async_bridge.run_coro_sync`.
- Produces:
  - `@dataclass(frozen=True) class MCPServerCfg: id:int; name:str; command:str; args:tuple[str,...]; env:Mapping[str,str]|None; tool_allowlist:frozenset[str]|None`
  - `MCPServerCfg.from_row(row) -> MCPServerCfg` (parses the JSON columns of an `MCPServer` ORM row)
  - `discover_tools(cfg: MCPServerCfg) -> list[dict]` — each dict `{"name": str, "description": str, "inputSchema": dict}`
  - `call_tool(cfg: MCPServerCfg, tool: str, arguments: dict) -> tuple[str, bool]` — `(text, is_error)`
  - raises `MCPError(str)` on connection/spawn failure.

- [ ] **Step 1: Create the stub server — `tests/fixtures/stub_mcp_server.py`**

```python
"""Minimal stdio MCP server for tests. One tool: echo(text) -> "echo: <text>".
Run indirectly by mcp_service via: python tests/fixtures/stub_mcp_server.py
"""
from mcp.server.mcpserver import MCPServer   # v1: `from mcp.server.fastmcp import FastMCP as MCPServer`

mcp = MCPServer("stub")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the input back with a prefix."""
    return f"echo: {text}"


@mcp.tool()
def always_fails() -> str:
    """Raises, to exercise the error path."""
    raise RuntimeError("intentional failure")


if __name__ == "__main__":
    mcp.run()   # stdio transport by default
```
> If Task 1's spike showed SDK v1.x, use `from mcp.server.fastmcp import FastMCP as MCPServer`. If v2.x, keep `from mcp.server.mcpserver import MCPServer`. Only this import line differs.

- [ ] **Step 2: Create `tests/fixtures/__init__.py`** (empty file).

- [ ] **Step 3: Write the failing test — `tests/test_mcp_service.py`**

```python
"""Run inside the backend container:
    docker compose exec -T backend pytest tests/test_mcp_service.py -v
"""
import os

import pytest

from app.services.mcp_service import MCPServerCfg, discover_tools, call_tool, MCPError

STUB = os.path.join(os.path.dirname(__file__), "fixtures", "stub_mcp_server.py")


def _cfg(**kw):
    base = dict(
        id=1, name="stub", command="python", args=(STUB,), env=None, tool_allowlist=None
    )
    base.update(kw)
    return MCPServerCfg(**base)


def test_discover_lists_stub_tools():
    tools = discover_tools(_cfg())
    names = {t["name"] for t in tools}
    assert "echo" in names
    echo = next(t for t in tools if t["name"] == "echo")
    assert "text" in echo["inputSchema"].get("properties", {})


def test_call_tool_round_trips():
    text, is_error = call_tool(_cfg(), "echo", {"text": "hi"})
    assert is_error is False
    assert "echo: hi" in text


def test_call_tool_reports_tool_error():
    text, is_error = call_tool(_cfg(), "always_fails", {})
    assert is_error is True
    assert text  # some error text is surfaced


def test_bad_command_raises_mcperror():
    with pytest.raises(MCPError):
        discover_tools(_cfg(command="definitely-not-a-real-binary-xyz"))
```

- [ ] **Step 4: Run — expect failure** (`No module named 'app.services.mcp_service'`).

Run: `docker compose exec -T backend pytest tests/test_mcp_service.py -v`

- [ ] **Step 5: Implement `backend/app/services/mcp_service.py`**

```python
"""Client for local (stdio) MCP servers: connect, discover tools, call a tool.

Spawn-per-operation: each discover / call opens a fresh subprocess via
stdio_client (a cancellation-shielded context manager) and closes it on exit.
No persistent connection pool this phase.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import settings
from app.services.async_bridge import run_coro_sync

logger = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """Raised when an MCP server cannot be spawned or the session fails to start."""


@dataclass(frozen=True)
class MCPServerCfg:
    id: int
    name: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str] | None
    tool_allowlist: frozenset[str] | None      # None => allow all discovered tools

    @classmethod
    def from_row(cls, row) -> "MCPServerCfg":
        try:
            args = tuple(json.loads(row.args_json or "[]"))
        except (json.JSONDecodeError, TypeError):
            args = ()
        env = None
        if row.env_json:
            try:
                env = dict(json.loads(row.env_json))
            except (json.JSONDecodeError, TypeError):
                env = None
        allow = None
        if row.tool_allowlist_json:
            try:
                allow = frozenset(json.loads(row.tool_allowlist_json))
            except (json.JSONDecodeError, TypeError):
                allow = None
        return cls(
            id=row.id, name=row.name, command=row.command,
            args=args, env=env, tool_allowlist=allow,
        )


def _params(cfg: MCPServerCfg) -> StdioServerParameters:
    return StdioServerParameters(
        command=cfg.command,
        args=list(cfg.args),
        env=dict(cfg.env) if cfg.env else None,
    )


async def _with_session(cfg: MCPServerCfg, fn):
    try:
        async with stdio_client(_params(cfg)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)
    except MCPError:
        raise
    except Exception as exc:  # spawn failure, protocol error, timeout during init
        raise MCPError(f"{type(exc).__name__}: {exc}") from exc


async def _discover_async(cfg: MCPServerCfg) -> list[dict]:
    async def go(session: ClientSession) -> list[dict]:
        res = await session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": dict(t.inputSchema) if t.inputSchema else {},
            }
            for t in res.tools
        ]

    return await _with_session(cfg, go)


async def _call_async(cfg: MCPServerCfg, tool: str, arguments: dict) -> tuple[str, bool]:
    async def go(session: ClientSession) -> tuple[str, bool]:
        res = await session.call_tool(
            tool, arguments or {},
            read_timeout_seconds=settings.mcp_call_timeout_s,
        )
        parts = []
        for block in getattr(res, "content", []) or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        # error flag attribute name confirmed in Task 1 spike (isError vs is_error)
        is_error = bool(getattr(res, "isError", getattr(res, "is_error", False)))
        return ("\n".join(parts).strip(), is_error)

    return await _with_session(cfg, go)


def discover_tools(cfg: MCPServerCfg) -> list[dict]:
    return run_coro_sync(lambda: _discover_async(cfg))


def call_tool(cfg: MCPServerCfg, tool: str, arguments: dict) -> tuple[str, bool]:
    return run_coro_sync(lambda: _call_async(cfg, tool, arguments))


if __name__ == "__main__":
    import os

    stub = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "tests", "fixtures", "stub_mcp_server.py"
    )
    cfg = MCPServerCfg(id=0, name="stub", command="python",
                       args=(os.path.abspath(stub),), env=None, tool_allowlist=None)
    tools = discover_tools(cfg)
    assert any(t["name"] == "echo" for t in tools), tools
    text, err = call_tool(cfg, "echo", {"text": "x"})
    assert not err and "echo: x" in text, (text, err)
    print("mcp_service self-check OK")
```
> The `read_timeout_seconds=` kwarg name was confirmed in Task 1. If the spike showed the SDK does not accept it on `call_tool`, drop the kwarg and wrap `go` in `asyncio.wait_for(..., settings.mcp_call_timeout_s)` inside `_call_async`.

- [ ] **Step 6: Add `mcp_call_timeout_s` / `mcp_discovery_timeout_s` to config now** (needed by the module import).

In `backend/app/config.py`, after the `enable_terminal_tool` line, add:
```python
    # ── MCP tools (Phase 1) ──────────────────────────────
    enable_mcp_tool: bool = False
    mcp_call_timeout_s: int = 30
    mcp_discovery_timeout_s: int = 20
```

- [ ] **Step 7: Rebuild + run**

Run:
```bash
docker compose up -d --build backend
docker compose exec -T backend pytest tests/test_mcp_service.py -v
docker compose exec -T backend python -m app.services.mcp_service
```
Expected: 4 passed; self-check prints OK.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/mcp_service.py backend/app/config.py tests/fixtures/ tests/test_mcp_service.py
git commit -m "feat(mcp): mcp_service — stdio connect, discover_tools, call_tool"
```

---

## Task 5: `tool_registry` — merge native + MCP tool descriptors

**Files:**
- Create: `backend/app/services/tool_registry.py`
- Test: `tests/test_tool_registry.py`

**Interfaces:**
- Consumes: `settings.enable_mcp_tool`; `mcp_service.MCPServerCfg`; `MCPServer` model.
- Produces:
  - `@dataclass(frozen=True) class RegisteredTool: name:str; description:str; input_schema:dict; source:str; server_id:int|None`
  - `list_tools(db, user_id: int) -> list[RegisteredTool]` — native descriptors always; MCP descriptors only when `enable_mcp_tool` and appended from each `enabled` server's `tools_json`, filtered by its allow-list. MCP names are `mcp__<server>__<tool>`.
  - `resolve_mcp(db, user_id: int, namespaced: str) -> tuple[MCPServerCfg, str] | None`
  - `_enabled_mcp_servers(db, user_id) -> list[MCPServer]` (monkeypatch point for tests)

- [ ] **Step 1: Write the failing test — `tests/test_tool_registry.py`**

```python
import json
import types

import pytest

from app.services import tool_registry
from app.services.tool_registry import RegisteredTool, list_tools, resolve_mcp


class _Row:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.name = kw.get("name", "stub")
        self.command = kw.get("command", "python")
        self.args_json = kw.get("args_json", '["-m","x"]')
        self.env_json = kw.get("env_json")
        self.enabled = kw.get("enabled", True)
        self.tool_allowlist_json = kw.get("tool_allowlist_json")
        self.tools_json = kw.get("tools_json", json.dumps([
            {"name": "echo", "description": "e", "inputSchema": {"type": "object"}},
            {"name": "danger", "description": "d", "inputSchema": {}},
        ]))


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(tool_registry, "_enabled_mcp_servers", lambda db, uid: [_Row()])


def test_flag_off_returns_native_only(monkeypatch):
    monkeypatch.setattr(tool_registry.settings, "enable_mcp_tool", False)
    tools = list_tools(db=None, user_id=1)
    assert tools and all(t.source == "native" for t in tools)


def test_flag_on_adds_namespaced_mcp_tools(monkeypatch):
    monkeypatch.setattr(tool_registry.settings, "enable_mcp_tool", True)
    names = {t.name for t in list_tools(db=None, user_id=1)}
    assert "mcp__stub__echo" in names
    assert "mcp__stub__danger" in names


def test_allowlist_filters(monkeypatch):
    monkeypatch.setattr(tool_registry.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(
        tool_registry, "_enabled_mcp_servers",
        lambda db, uid: [_Row(tool_allowlist_json='["echo"]')],
    )
    names = {t.name for t in list_tools(db=None, user_id=1)}
    assert "mcp__stub__echo" in names
    assert "mcp__stub__danger" not in names


def test_resolve_mcp_round_trip(monkeypatch):
    monkeypatch.setattr(tool_registry.settings, "enable_mcp_tool", True)
    hit = resolve_mcp(db=None, user_id=1, namespaced="mcp__stub__echo")
    assert hit is not None
    cfg, bare = hit
    assert bare == "echo" and cfg.name == "stub"
    assert resolve_mcp(db=None, user_id=1, namespaced="mcp__stub__danger__x") is None
    assert resolve_mcp(db=None, user_id=1, namespaced="not_mcp") is None
```

- [ ] **Step 2: Run — expect failure**.

- [ ] **Step 3: Implement `backend/app/services/tool_registry.py`**

```python
"""Unified catalog of tools the agent could call: native + per-user MCP.

Phase 1: only MCP tools are dispatched (via the [MCP_CALL] marker). Native
tools keep their existing triggers; they are listed here so a future
bind_tools loop has one authoritative source (the "seam").
"""

import json
import logging
from dataclasses import dataclass

from app.config import settings
from app.models.models import MCPServer
from app.services.mcp_service import MCPServerCfg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    input_schema: dict
    source: str            # "native" | "mcp"
    server_id: int | None


_NATIVE_DESCRIPTORS: list[RegisteredTool] = [
    RegisteredTool("web_scraper", "Fetch and read the visible text of a web page.", {}, "native", None),
    RegisteredTool("youtube_summarizer", "Fetch and summarize a YouTube transcript.", {}, "native", None),
    RegisteredTool("python_sandbox", "Execute a short Python snippet and return its output.", {}, "native", None),
    RegisteredTool("terminal", "Propose a shell command for human approval.", {}, "native", None),
]


def _enabled_mcp_servers(db, user_id: int) -> list[MCPServer]:
    return (
        db.query(MCPServer)
        .filter(MCPServer.user_id == user_id, MCPServer.enabled.is_(True))
        .all()
    )


def _allowed(row: MCPServer, tool_name: str) -> bool:
    if not row.tool_allowlist_json:
        return True
    try:
        allow = set(json.loads(row.tool_allowlist_json))
    except (json.JSONDecodeError, TypeError):
        return True
    return not allow or tool_name in allow


def list_tools(db, user_id: int) -> list[RegisteredTool]:
    tools = list(_NATIVE_DESCRIPTORS)
    if not settings.enable_mcp_tool:
        return tools
    for row in _enabled_mcp_servers(db, user_id):
        try:
            discovered = json.loads(row.tools_json or "[]")
        except (json.JSONDecodeError, TypeError):
            discovered = []
        for t in discovered:
            tname = t.get("name")
            if not tname or "__" in tname:
                if tname and "__" in tname:
                    logger.warning("MCP tool %r on server %r skipped: name contains '__'", tname, row.name)
                continue
            if not _allowed(row, tname):
                continue
            tools.append(RegisteredTool(
                name=f"mcp__{row.name}__{tname}",
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}) or {},
                source="mcp",
                server_id=row.id,
            ))
    return tools


def resolve_mcp(db, user_id: int, namespaced: str) -> tuple[MCPServerCfg, str] | None:
    parts = namespaced.split("__")
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    _, server_name, tool = parts
    for row in _enabled_mcp_servers(db, user_id):
        if row.name != server_name:
            continue
        try:
            discovered = {t.get("name") for t in json.loads(row.tools_json or "[]")}
        except (json.JSONDecodeError, TypeError):
            discovered = set()
        if tool not in discovered or not _allowed(row, tool):
            return None
        return MCPServerCfg.from_row(row), tool
    return None


if __name__ == "__main__":
    # Pure structural self-check (no DB): namespacing + split rules.
    assert ("mcp__a__b".split("__")) == ["mcp", "a", "b"]
    assert len("mcp__a__b__c".split("__")) == 4
    print("tool_registry self-check OK")
```

- [ ] **Step 4: Rebuild + run** — `pytest tests/test_tool_registry.py -v` → 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/tool_registry.py tests/test_tool_registry.py
git commit -m "feat(mcp): tool_registry — native + MCP descriptor catalog and resolver"
```

---

## Task 6: `mcp_tool` — marker parsing + dispatch

**Files:**
- Create: `backend/app/tools/mcp_tool.py`
- Test: `tests/test_mcp_tool.py`

**Interfaces:**
- Consumes: `tool_registry.resolve_mcp`; `mcp_service.call_tool`.
- Produces:
  - `MAX_MCP_CALLS_PER_TURN = 3`
  - `extract_mcp_calls(text: str) -> list[tuple[str, dict]]` — namespaced name + parsed args (`{}` if absent/blank), malformed entries skipped, capped at 3.
  - `format_mcp_result(name: str, text: str, is_error: bool) -> str`
  - `run_mcp_calls(calls: list[tuple[str, dict]], db, user_id: int) -> str` — joined result blocks; rejects unknown/disabled/not-allow-listed **before** any spawn.

- [ ] **Step 1: Write the failing test — `tests/test_mcp_tool.py`**

```python
import pytest

from app.tools import mcp_tool
from app.tools.mcp_tool import extract_mcp_calls, format_mcp_result, run_mcp_calls


def test_extract_basic():
    calls = extract_mcp_calls('before\n[MCP_CALL: mcp__srv__echo {"text": "hi"}]\nafter')
    assert calls == [("mcp__srv__echo", {"text": "hi"})]


def test_extract_no_args():
    assert extract_mcp_calls("[MCP_CALL: mcp__srv__list]") == [("mcp__srv__list", {})]


def test_extract_multiple_capped_at_3():
    text = "\n".join(f'[MCP_CALL: mcp__s__t{i} {{}}]' for i in range(5))
    assert len(extract_mcp_calls(text)) == 3


def test_extract_skips_malformed_json():
    assert extract_mcp_calls('[MCP_CALL: mcp__s__t {not json}]') == []


def test_format_result_error_marker():
    out = format_mcp_result("mcp__s__t", "boom", True)
    assert "mcp__s__t" in out and "[error]" in out


def test_run_rejects_unknown_without_spawn(monkeypatch):
    monkeypatch.setattr(mcp_tool.tool_registry, "resolve_mcp", lambda db, uid, n: None)
    called = {"n": 0}
    monkeypatch.setattr(mcp_tool.mcp_service, "call_tool",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ("", False))
    out = run_mcp_calls([("mcp__x__y", {})], db=None, user_id=1)
    assert called["n"] == 0
    assert "unknown" in out.lower() or "not available" in out.lower()


def test_run_dispatches_known(monkeypatch):
    from app.services.mcp_service import MCPServerCfg
    cfg = MCPServerCfg(id=1, name="s", command="python", args=(), env=None, tool_allowlist=None)
    monkeypatch.setattr(mcp_tool.tool_registry, "resolve_mcp", lambda db, uid, n: (cfg, "t"))
    monkeypatch.setattr(mcp_tool.mcp_service, "call_tool", lambda c, t, a: ("RESULT", False))
    out = run_mcp_calls([("mcp__s__t", {"a": 1})], db=None, user_id=1)
    assert "RESULT" in out
```

- [ ] **Step 2: Run — expect failure.**

- [ ] **Step 3: Implement `backend/app/tools/mcp_tool.py`**

```python
"""Parse and dispatch [MCP_CALL: mcp__<server>__<tool> {json args}] markers.

Mirrors the single-pass [PYTHON_CODE] flow: the LLM emits a marker, we run the
call, and the result is injected back (regenerate in the non-streaming path,
appended in the streaming path).
"""

import json
import logging
import re

from app.services import mcp_service, tool_registry

logger = logging.getLogger(__name__)

MAX_MCP_CALLS_PER_TURN = 3

# name must look like mcp__<server>__<tool...>; args group is greedy to the last
# '}' on the line. resolve_mcp() is the authoritative validator of the name.
_MARKER_RE = re.compile(
    r"\[MCP_CALL:\s*(mcp__[a-z0-9_]+(?:__[A-Za-z0-9_-]+)+)\s*(\{.*\})?\s*\]"
)


def extract_mcp_calls(text: str) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    for line in (text or "").splitlines():
        m = _MARKER_RE.search(line)
        if not m:
            continue
        name, raw_args = m.group(1), (m.group(2) or "").strip()
        if raw_args:
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                logger.warning("MCP marker had unparseable args: %s", raw_args)
                continue
            if not isinstance(args, dict):
                continue
        else:
            args = {}
        calls.append((name, args))
        if len(calls) >= MAX_MCP_CALLS_PER_TURN:
            break
    return calls


def format_mcp_result(name: str, text: str, is_error: bool) -> str:
    body = f"[error] {text}" if is_error else text
    return (
        f"=== MCP Tool Result ({name}) ===\n"
        f"{body}\n"
        f"=== End of MCP Tool Result ==="
    )


def run_mcp_calls(calls: list[tuple[str, dict]], db, user_id: int) -> str:
    blocks: list[str] = []
    for name, args in calls:
        hit = tool_registry.resolve_mcp(db, user_id, name)
        if hit is None:
            blocks.append(format_mcp_result(
                name, "unknown tool, server disabled, or tool not allow-listed", True))
            continue
        cfg, bare = hit
        try:
            text, is_error = mcp_service.call_tool(cfg, bare, args)
        except Exception as exc:  # MCPError or anything unexpected
            logger.warning("MCP call %s failed: %s", name, exc)
            text, is_error = f"{type(exc).__name__}: {exc}", True
        blocks.append(format_mcp_result(name, text, is_error))
    return "\n\n".join(blocks)


if __name__ == "__main__":
    assert extract_mcp_calls('[MCP_CALL: mcp__s__t {"x": 1}]') == [("mcp__s__t", {"x": 1})]
    assert extract_mcp_calls("[MCP_CALL: mcp__s__t]") == [("mcp__s__t", {})]
    assert extract_mcp_calls("[MCP_CALL: mcp__s__t {bad}]") == []
    assert "[error]" in format_mcp_result("n", "e", True)
    print("mcp_tool self-check OK")
```

- [ ] **Step 4: Rebuild + run** — `pytest tests/test_mcp_tool.py -v` → 7 passed; `python -m app.tools.mcp_tool` → OK.

- [ ] **Step 5: Commit**

```bash
git add backend/app/tools/mcp_tool.py tests/test_mcp_tool.py
git commit -m "feat(mcp): mcp_tool — [MCP_CALL] marker parsing and dispatch"
```

---

## Task 7: `docker-compose.yml` — expose `ENABLE_MCP_TOOL`

**Files:**
- Modify: `docker-compose.yml`

(`config.py` already gained the fields in Task 4 Step 6.)

- [ ] **Step 1: Edit `docker-compose.yml`**

Under `services.backend.environment`, right after the `ENABLE_TERMINAL_TOOL` line, add:
```yaml
      - ENABLE_MCP_TOOL=${ENABLE_MCP_TOOL:-false}
      - MCP_CALL_TIMEOUT_S=${MCP_CALL_TIMEOUT_S:-30}
```

- [ ] **Step 2: Recreate backend, confirm the setting is visible**

Run:
```bash
docker compose up -d backend
docker compose exec -T backend python -c "from app.config import settings; print(settings.enable_mcp_tool, settings.mcp_call_timeout_s)"
```
Expected: `False 30`

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(mcp): add ENABLE_MCP_TOOL / MCP_CALL_TIMEOUT_S compose env (default off)"
```

---

## Task 8: System-prompt block for MCP tools

**Files:**
- Modify: `backend/app/services/system_prompts.py`
- Test: `tests/test_mcp_tool.py` (append a block-format test) — or a new `tests/test_system_prompts.py`; use the new file.

**Interfaces:**
- Consumes: `tool_registry.RegisteredTool`.
- Produces: `build_mcp_tools_block(tools: list[RegisteredTool]) -> str` — empty string when `tools` is empty; otherwise a `## MCP Tools` section listing each tool name, description, and (compact) input schema, plus the exact `[MCP_CALL: …]` instruction.

- [ ] **Step 1: Write the failing test — `tests/test_system_prompts.py`**

```python
from app.services.system_prompts import build_mcp_tools_block
from app.services.tool_registry import RegisteredTool


def test_empty_when_no_tools():
    assert build_mcp_tools_block([]) == ""


def test_lists_tools_and_marker_format():
    tools = [RegisteredTool("mcp__fs__read", "Read a file", {"type": "object",
             "properties": {"path": {"type": "string"}}}, "mcp", 1)]
    block = build_mcp_tools_block(tools)
    assert "## MCP Tools" in block
    assert "[MCP_CALL:" in block
    assert "mcp__fs__read" in block
    assert "path" in block
```

- [ ] **Step 2: Run — expect failure** (`ImportError: cannot import name 'build_mcp_tools_block'`).

- [ ] **Step 3: Implement in `backend/app/services/system_prompts.py`**

Add at the end of the file:
```python
def build_mcp_tools_block(tools) -> str:
    """Render the '## MCP Tools' system-prompt section for the given RegisteredTools.

    `tools` is a list of tool_registry.RegisteredTool (source == "mcp").
    Returns "" when the list is empty.
    """
    mcp = [t for t in tools if getattr(t, "source", None) == "mcp"]
    if not mcp:
        return ""

    lines = [
        "## MCP Tools",
        "You may call these external tools. To call one, output EXACTLY this, "
        "alone on its own line:",
        '[MCP_CALL: <tool_name> {"arg": "value"}]',
        "The tool result is returned to you; then continue your answer. "
        "Emit at most 3 calls per reply.",
        "",
        "Available tools:",
    ]
    for t in mcp:
        props = ""
        schema = t.input_schema or {}
        if isinstance(schema, dict) and schema.get("properties"):
            props = " Input keys: " + ", ".join(sorted(schema["properties"].keys()))
        desc = (t.description or "").strip().replace("\n", " ")
        lines.append(f"- {t.name} — {desc}{props}")
    return "\n".join(lines)
```

- [ ] **Step 4: Rebuild + run** — `pytest tests/test_system_prompts.py -v` → 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/system_prompts.py tests/test_system_prompts.py
git commit -m "feat(mcp): build_mcp_tools_block system-prompt section"
```

---

## Task 9: `routes/mcp.py` — CRUD + `/discover`

**Files:**
- Create: `backend/app/routes/mcp.py`
- Modify: `backend/app/main.py` (import + `include_router`)
- Test: `tests/test_mcp_routes.py` (extend the file from Task 3)

**Interfaces:**
- Consumes: `get_db`, `get_current_user`, `require_csrf`, `MCPServer`, `mcp_service`.
- Produces HTTP:
  - `GET /api/mcp/servers` → `list[McpServerOut]`
  - `POST /api/mcp/servers` (`McpServerIn`) → `McpServerOut` (201)
  - `PUT /api/mcp/servers/{id}` (`McpServerUpdate`) → `McpServerOut`
  - `DELETE /api/mcp/servers/{id}` → 204
  - `POST /api/mcp/servers/{id}/discover` → `{ "tools": [...], "error": str | null }`
  - `McpServerOut` fields: `id, name, command, args, env, enabled, tool_allowlist, tools, last_discovered_at, last_error, created_at`.

- [ ] **Step 1: Write failing tests — append to `tests/test_mcp_routes.py`**

```python
from tests.auth_helpers import register_and_login, auth_headers


def _auth(client):
    uid, token = register_and_login(client, username=None)  # helper makes a unique user
    return auth_headers(token)


def _csrf(client):
    # CSRF cookie is set on login; mirror how other route tests read it.
    tok = client.cookies.get("research_assistant_csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


def test_crud_flow(client):
    h = {**_auth(client), **_csrf(client)}
    # create
    r = client.post("/api/mcp/servers", headers=h, json={
        "name": "local_fetch", "command": "python", "args": ["-m", "mcp_server_fetch"],
        "env": None, "enabled": True, "tool_allowlist": None,
    })
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    # list
    assert any(s["id"] == sid for s in client.get("/api/mcp/servers", headers=h).json())
    # update
    r = client.put(f"/api/mcp/servers/{sid}", headers=h, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    # delete
    assert client.delete(f"/api/mcp/servers/{sid}", headers=h).status_code == 204


def test_name_validation_rejects_double_underscore(client):
    h = {**_auth(client), **_csrf(client)}
    r = client.post("/api/mcp/servers", headers=h, json={
        "name": "bad__name", "command": "python", "args": [],
    })
    assert r.status_code == 400


def test_requires_auth(client):
    assert client.get("/api/mcp/servers").status_code in (401, 403)


def test_discover_against_stub(client):
    import os
    stub = os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures", "stub_mcp_server.py"))
    h = {**_auth(client), **_csrf(client)}
    r = client.post("/api/mcp/servers", headers=h, json={
        "name": "stub", "command": "python", "args": [stub],
    })
    sid = r.json()["id"]
    r = client.post(f"/api/mcp/servers/{sid}/discover", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is None
    assert any(t["name"] == "echo" for t in body["tools"])
```
> If `register_and_login` / CSRF handling differs from the snippet, copy the exact pattern from `tests/test_auth.py` (it exercises CSRF-protected POSTs).

- [ ] **Step 2: Run — expect failure** (404s: router not mounted).

- [ ] **Step 3: Implement `backend/app/routes/mcp.py`**

```python
"""MCP server manager routes (Phase 1 — stdio, Python servers only).

GET    /api/mcp/servers
POST   /api/mcp/servers
PUT    /api/mcp/servers/{id}
DELETE /api/mcp/servers/{id}
POST   /api/mcp/servers/{id}/discover
"""

import json
import re
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import MCPServer, User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.mcp_service import MCPServerCfg, MCPError, discover_tools

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

_NAME_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


# ── Schemas ──────────────────────────────────

class McpServerIn(BaseModel):
    name: str
    command: str
    args: List[str] = []
    env: Optional[dict] = None
    enabled: bool = True
    tool_allowlist: Optional[List[str]] = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v or "") or len(v) > 40:
            raise ValueError(
                "name must match ^[a-z0-9]+(?:_[a-z0-9]+)*$ (lowercase, digits, "
                "single underscores) and be at most 40 chars"
            )
        return v


class McpServerUpdate(BaseModel):
    name: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[dict] = None
    enabled: Optional[bool] = None
    tool_allowlist: Optional[List[str]] = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v):
        if v is None:
            return v
        if not _NAME_RE.match(v) or len(v) > 40:
            raise ValueError("invalid name")
        return v


class McpServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=False)
    id: int
    name: str
    command: str
    args: List[str]
    env: Optional[dict]
    enabled: bool
    tool_allowlist: Optional[List[str]]
    tools: list
    last_discovered_at: Optional[datetime]
    last_error: Optional[str]
    created_at: datetime


def _to_out(row: MCPServer) -> McpServerOut:
    def _j(s, default):
        if not s:
            return default
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return default

    return McpServerOut(
        id=row.id, name=row.name, command=row.command,
        args=_j(row.args_json, []), env=_j(row.env_json, None),
        enabled=row.enabled, tool_allowlist=_j(row.tool_allowlist_json, None),
        tools=_j(row.tools_json, []),
        last_discovered_at=row.last_discovered_at, last_error=row.last_error,
        created_at=row.created_at,
    )


def _owned(db: Session, user_id: int, server_id: int) -> MCPServer:
    row = db.query(MCPServer).filter(
        MCPServer.id == server_id, MCPServer.user_id == user_id
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return row


# ── Endpoints ────────────────────────────────

@router.get("/servers", response_model=List[McpServerOut])
def list_servers(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(MCPServer).filter(MCPServer.user_id == user.id).order_by(MCPServer.id).all()
    return [_to_out(r) for r in rows]


@router.post("/servers", response_model=McpServerOut, status_code=201)
def create_server(
    payload: McpServerIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    if db.query(MCPServer).filter(
        MCPServer.user_id == user.id, MCPServer.name == payload.name
    ).first():
        raise HTTPException(status_code=400, detail=f"A server named '{payload.name}' already exists")
    row = MCPServer(
        user_id=user.id, name=payload.name, command=payload.command,
        args_json=json.dumps(payload.args or []),
        env_json=json.dumps(payload.env) if payload.env else None,
        enabled=payload.enabled,
        tool_allowlist_json=json.dumps(payload.tool_allowlist) if payload.tool_allowlist is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.put("/servers/{server_id}", response_model=McpServerOut)
def update_server(
    server_id: int,
    payload: McpServerUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    row = _owned(db, user.id, server_id)
    if payload.name is not None:
        row.name = payload.name
    if payload.command is not None:
        row.command = payload.command
    if payload.args is not None:
        row.args_json = json.dumps(payload.args)
    if payload.env is not None:
        row.env_json = json.dumps(payload.env) if payload.env else None
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.tool_allowlist is not None:
        row.tool_allowlist_json = json.dumps(payload.tool_allowlist)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.delete("/servers/{server_id}", status_code=204)
def delete_server(
    server_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    db.delete(_owned(db, user.id, server_id))
    db.commit()
    return None


@router.post("/servers/{server_id}/discover")
def discover(
    server_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    row = _owned(db, user.id, server_id)
    cfg = MCPServerCfg.from_row(row)
    try:
        tools = discover_tools(cfg)
    except MCPError as exc:
        row.last_error = str(exc)
        db.commit()
        return {"tools": [], "error": str(exc)}
    row.tools_json = json.dumps(tools)
    row.last_discovered_at = datetime.utcnow()
    row.last_error = None
    db.commit()
    return {"tools": tools, "error": None}
```

- [ ] **Step 4: Register the router — `backend/app/main.py`**

- Add with the other route imports (near `backend/app/main.py:25`):
  ```python
  from app.routes.mcp import router as mcp_router
  ```
- Add with the other `include_router` calls (near `backend/app/main.py:238`):
  ```python
  app.include_router(mcp_router)
  ```

- [ ] **Step 5: Rebuild + run**

Run:
```bash
docker compose up -d --build backend
docker compose exec -T backend pytest tests/test_mcp_routes.py -v
```
Expected: all tests pass (table test from Task 3 + the 4 CRUD/discover tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/mcp.py backend/app/main.py tests/test_mcp_routes.py
git commit -m "feat(mcp): /api/mcp/servers CRUD + /discover routes"
```

---

## Task 10: Wire the MCP prompt block into both chat paths

**Files:**
- Modify: `backend/app/services/streaming_service.py` (`ChatContext`, `prepare_chat_context`)
- Modify: `backend/app/services/langgraph_workflow.py` (`WorkflowState`, `_build_system_prompt`)
- Test: `tests/test_system_prompts.py` (add an integration-ish assertion is optional; the wiring is covered end-to-end in Task 13). Add a focused unit test below.

**Interfaces:**
- Consumes: `tool_registry.list_tools`, `system_prompts.build_mcp_tools_block`.
- Produces: when `settings.enable_mcp_tool` and the user has enabled MCP servers with discovered tools, the assembled system prompt (both paths) contains the `## MCP Tools` block. `ChatContext` gains `user_id: int`. `WorkflowState` gains `mcp_result: Optional[str]`.

- [ ] **Step 1: Write the failing test — `tests/test_mcp_prompt_wiring.py`**

```python
import backend  # noqa  (ensure path) -- if this fails, drop it; container runs from /app

from app.services import tool_registry, system_prompts
from app.services.tool_registry import RegisteredTool


def test_langgraph_prompt_includes_mcp_block(monkeypatch):
    from app.services import langgraph_workflow as lw

    monkeypatch.setattr(lw.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(
        tool_registry, "list_tools",
        lambda db, uid: [RegisteredTool("mcp__s__echo", "Echo", {}, "mcp", 1)],
    )
    state = {
        "system_prompt": None, "memory_context": "", "retrieved_context": "",
        "web_context": "", "command_result": "", "code_result": "",
        "mcp_result": "", "db": object(), "user_id": 1,
    }
    prompt = lw._build_system_prompt(state)
    assert "## MCP Tools" in prompt
    assert "mcp__s__echo" in prompt


def test_mcp_result_block_rendered(monkeypatch):
    from app.services import langgraph_workflow as lw
    monkeypatch.setattr(lw.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(tool_registry, "list_tools", lambda db, uid: [])
    state = {
        "system_prompt": None, "memory_context": "", "retrieved_context": "",
        "web_context": "", "command_result": "", "code_result": "",
        "mcp_result": "=== MCP Tool Result (mcp__s__t) ===\nX\n=== End of MCP Tool Result ===",
        "db": object(), "user_id": 1,
    }
    assert "MCP Tool Result" in lw._build_system_prompt(state)
```

- [ ] **Step 2: Run — expect failure** (`KeyError`/`AttributeError` — `mcp_result` not handled, block absent).

- [ ] **Step 3: `langgraph_workflow.py` changes**

- `WorkflowState` (`backend/app/services/langgraph_workflow.py` ~line 88): add
  ```python
      mcp_result: Optional[str]
  ```
  next to `code_result`.
- Top of file, with the other `from app.services...` imports (~line 52):
  ```python
  from app.services.tool_registry import list_tools as _registry_list_tools
  from app.services.system_prompts import build_base_prompt, build_mcp_tools_block
  ```
  (there is already `from app.services.system_prompts import build_base_prompt` — merge the names).
- In `_build_system_prompt`, after the `web_context` append block and before the `command_result` block:
  ```python
      # MCP tools catalog
      if settings.enable_mcp_tool and state.get("db") is not None:
          try:
              mcp_block = build_mcp_tools_block(
                  _registry_list_tools(state["db"], state.get("user_id", 1))
              )
              if mcp_block:
                  system_parts.append(mcp_block)
          except Exception as exc:                       # never break generation
              logger.warning("MCP prompt block failed (non-fatal): %s", exc)
  ```
- In `_build_system_prompt`, after the `code_result` block:
  ```python
      mcp_result = state.get("mcp_result", "")
      if mcp_result:
          system_parts.append(mcp_result)
  ```
- In `run_research_workflow`'s `initial_state` dict (~line 834): add `"mcp_result": "",`.

- [ ] **Step 4: `streaming_service.py` changes**

- `ChatContext.__init__` (`backend/app/services/streaming_service.py:67`): add `user_id: int = 1` parameter and `self.user_id = user_id`.
- `prepare_chat_context` signature already receives `user_id: int = 1`. Where it builds `ChatContext(...)` at the end (~line 258), pass `user_id=user_id`.
- Add import near the top (~line 34): `from app.services.tool_registry import list_tools as _registry_list_tools` and extend the existing `from app.services.system_prompts import build_base_prompt` to also import `build_mcp_tools_block`.
- In `prepare_chat_context`, after the RAG `system_parts.append(...)` block and before the web-scraping try/except (~line 201):
  ```python
      # MCP tools catalog
      if settings.enable_mcp_tool:
          try:
              mcp_block = build_mcp_tools_block(_registry_list_tools(db, user_id))
              if mcp_block:
                  system_parts.append(mcp_block)
          except Exception as exc:
              logger.warning("MCP prompt block failed (non-fatal): %s", exc)
  ```

- [ ] **Step 5: Rebuild + run**

Run:
```bash
docker compose up -d --build backend
docker compose exec -T backend pytest tests/test_mcp_prompt_wiring.py -v
```
Expected: 2 passed. If the `import backend` line errors, delete it (the container's working dir is `/app`, `app.*` is importable directly).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/streaming_service.py backend/app/services/langgraph_workflow.py tests/test_mcp_prompt_wiring.py
git commit -m "feat(mcp): inject MCP tools block into streaming + langgraph system prompts"
```

---

## Task 11: Dispatch `[MCP_CALL]` in both chat paths

**Files:**
- Modify: `backend/app/services/langgraph_workflow.py` (`generate_answer`)
- Modify: `backend/app/services/streaming_service.py` (`stream_chat_response`, `done` branch)
- Test: `tests/test_mcp_dispatch.py`

**Interfaces:**
- Consumes: `mcp_tool.extract_mcp_calls`, `mcp_tool.run_mcp_calls`.
- Produces: when `settings.enable_mcp_tool` and the model's reply contains `[MCP_CALL: …]`:
  - **non-streaming:** `state["mcp_result"]` is set and `state["regenerate"] = True` (one more LLM pass sees the result).
  - **streaming:** the formatted result is appended to `full_response_text` (single pass; the user sees it, the model does not re-run this turn — matches `[PYTHON_CODE]`).

- [ ] **Step 1: Write failing tests — `tests/test_mcp_dispatch.py`**

```python
import pytest


def test_generate_answer_dispatches_mcp(monkeypatch):
    from app.services import langgraph_workflow as lw
    from app.tools import mcp_tool

    monkeypatch.setattr(lw.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(lw, "generate_response",
                        lambda **kw: '[MCP_CALL: mcp__s__echo {"text": "hi"}]')
    monkeypatch.setattr(mcp_tool, "run_mcp_calls",
                        lambda calls, db, user_id: "=== MCP Tool Result (mcp__s__echo) ===\necho: hi\n=== End of MCP Tool Result ===")

    state = _minimal_state()
    out = lw.generate_answer(state)
    assert out.get("regenerate") is True
    assert "echo: hi" in out.get("mcp_result", "")


def test_generate_answer_ignores_marker_when_flag_off(monkeypatch):
    from app.services import langgraph_workflow as lw
    monkeypatch.setattr(lw.settings, "enable_mcp_tool", False)
    monkeypatch.setattr(lw, "generate_response",
                        lambda **kw: '[MCP_CALL: mcp__s__echo {}]')
    state = _minimal_state()
    out = lw.generate_answer(state)
    assert not out.get("mcp_result")


def _minimal_state():
    return {
        "error": None, "messages": [], "user_input": "q", "image_url": None,
        "model_name": None, "db": None, "user_id": 1, "session_id": 1,
        "regenerate": False, "pending_command": None, "system_prompt": None,
        "memory_context": "", "retrieved_context": "", "web_context": "",
        "command_result": "", "code_result": "", "mcp_result": "",
    }
```
> `generate_answer` calls `process_memory_markers` when `db is not None`; passing `db=None` in `_minimal_state` skips that. If `generate_answer` requires a real `db` for another reason, set `"db"` to a `unittest.mock.MagicMock()` and also monkeypatch `lw.process_memory_markers` import site.

- [ ] **Step 2: Run — expect failure.**

- [ ] **Step 3: `langgraph_workflow.generate_answer` change**

Near the top of the file, extend the tools import:
```python
from app.tools.mcp_tool import extract_mcp_calls as _extract_mcp_calls, run_mcp_calls as _run_mcp_calls
```
In `generate_answer`, **after** the `[SAVE_MEMORY]` processing (`response = cleaned`) and the existing `state["response"] = response`, and **before** the `# ── Detect proposed command ──` block:
```python
    # ── MCP tool calls ([MCP_CALL: …]) — single-pass, mirrors [PYTHON_CODE] ──
    if (
        settings.enable_mcp_tool
        and not state.get("regenerate")
        and not state.get("pending_command")
    ):
        mcp_calls = _extract_mcp_calls(response)
        if mcp_calls:
            state["mcp_result"] = _run_mcp_calls(
                mcp_calls, state.get("db"), state.get("user_id", 1)
            )
            state["regenerate"] = True
            db = state.get("db")
            if db is not None:
                placeholder = Message(
                    session_id=state["session_id"],
                    role=MessageRole.assistant,
                    content=response,
                )
                db.add(placeholder)
                db.commit()
                db.refresh(placeholder)
                state["assistant_message_id"] = placeholder.id
```

- [ ] **Step 4: `streaming_service.stream_chat_response` change**

Import near the top (~line 37):
```python
from app.tools.mcp_tool import extract_mcp_calls, run_mcp_calls
```
In the `elif chunk["type"] == "done":` branch, right after the existing `[PYTHON_CODE]` block that appends `format_code_result(...)`:
```python
                # [MCP_CALL: …] — single pass, appended like [PYTHON_CODE]
                if settings.enable_mcp_tool:
                    mcp_calls = extract_mcp_calls(full_response_text)
                    if mcp_calls:
                        full_response_text += "\n\n" + run_mcp_calls(
                            mcp_calls, db_for_mcp, context.user_id
                        )
```
`stream_chat_response` does not currently receive a `db`. Two options — pick the one that matches the surrounding code after reading it:
  - **(a)** If `context` already carries something usable, add `ChatContext.db` set in `prepare_chat_context` — but the `ChatContext` docstring says "no ORM objects / scalars only". A `Session` is not an ORM instance and `prepare_chat_context` already holds `db`; storing it is acceptable for this single synchronous use. Add `db` param to `ChatContext.__init__`, pass it, and use `context.db` as `db_for_mcp`.
  - **(b)** Cleaner: `run_mcp_calls` only needs `db` for `tool_registry.resolve_mcp` → which reads `MCPServer` rows. Open a short-lived session: `from app.database import SessionLocal` and
    ```python
                        _db = SessionLocal()
                        try:
                            full_response_text += "\n\n" + run_mcp_calls(mcp_calls, _db, context.user_id)
                        finally:
                            _db.close()
    ```
  Use **(b)** — no `ChatContext` shape change, and the streaming generator already opens its own sessions elsewhere for memory markers.

- [ ] **Step 5: Rebuild + run**

Run:
```bash
docker compose up -d --build backend
docker compose exec -T backend pytest tests/test_mcp_dispatch.py tests/test_mcp_prompt_wiring.py -v
```
Expected: all pass.

- [ ] **Step 6: Full backend test sweep (regression check)**

Run: `docker compose exec -T backend pytest tests/ -v -k "mcp or async_bridge or system_prompt"`
Expected: all MCP-related tests green. Then a broader smoke: `docker compose exec -T backend pytest tests/test_health.py -v`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/langgraph_workflow.py backend/app/services/streaming_service.py tests/test_mcp_dispatch.py
git commit -m "feat(mcp): dispatch [MCP_CALL] in langgraph (regenerate) and streaming (append)"
```

---

## Task 12: Frontend — MCP servers Settings tab

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/McpServers.tsx`
- Modify: `frontend/src/Settings.tsx`

**Interfaces:**
- Consumes: `/api/mcp/servers*` routes from Task 9.
- Produces: a `"mcp"` tab in Settings with a server list, add/edit dialog (name, command, args JSON textarea, env JSON textarea, enabled, allow-list), "Test / Refresh tools" button, and a note that `ENABLE_MCP_TOOL` must be set for tools to reach the agent.

- [ ] **Step 1: Add types — `frontend/src/types.ts`**

```typescript
export interface McpTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface McpServer {
  id: number;
  name: string;
  command: string;
  args: string[];
  env: Record<string, string> | null;
  enabled: boolean;
  tool_allowlist: string[] | null;
  tools: McpTool[];
  last_discovered_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface McpServerCreate {
  name: string;
  command: string;
  args: string[];
  env?: Record<string, string> | null;
  enabled?: boolean;
  tool_allowlist?: string[] | null;
}
```

- [ ] **Step 2: Add API methods — `frontend/src/api.ts`**

Add to the `import type { … }` list: `McpServer, McpServerCreate, McpTool`.
Add a new group inside the `API` object (after the Scheduler group):
```typescript
  // ── MCP Servers ──────────────────────────────────────────
  listMcpServers() {
    return request<McpServer[]>("/api/mcp/servers");
  },
  createMcpServer(data: McpServerCreate) {
    return request<McpServer>("/api/mcp/servers", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },
  updateMcpServer(id: number, data: Partial<McpServerCreate>) {
    return request<McpServer>(`/api/mcp/servers/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    });
  },
  deleteMcpServer(id: number) {
    return request<void>(`/api/mcp/servers/${id}`, { method: "DELETE" });
  },
  discoverMcpServer(id: number) {
    return request<{ tools: McpTool[]; error: string | null }>(
      `/api/mcp/servers/${id}/discover`,
      { method: "POST" }
    );
  },
```

- [ ] **Step 3: Create `frontend/src/McpServers.tsx`**

```tsx
import { useCallback, useEffect, useState } from "react";
import { Plus, Pencil, Trash2, RefreshCw } from "lucide-react";
import { API } from "./api";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "./components/ui/dialog";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import type { McpServer, McpServerCreate } from "./types";

const EMPTY: McpServerCreate = {
  name: "", command: "python", args: [], env: null, enabled: true, tool_allowlist: null,
};

export function McpServers() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<McpServer | null>(null);
  const [form, setForm] = useState<McpServerCreate>(EMPTY);
  const [argsText, setArgsText] = useState("[]");
  const [envText, setEnvText] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setServers(await API.listMcpServers());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load MCP servers");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  function openAdd() {
    setEditing(null); setForm(EMPTY); setArgsText("[]"); setEnvText(""); setDialogOpen(true);
  }
  function openEdit(s: McpServer) {
    setEditing(s);
    setForm({ name: s.name, command: s.command, args: s.args, env: s.env,
      enabled: s.enabled, tool_allowlist: s.tool_allowlist });
    setArgsText(JSON.stringify(s.args));
    setEnvText(s.env ? JSON.stringify(s.env, null, 2) : "");
    setDialogOpen(true);
  }

  async function save() {
    let args: string[];
    let env: Record<string, string> | null = null;
    try {
      args = JSON.parse(argsText);
      if (!Array.isArray(args)) throw new Error("args must be a JSON array");
      if (envText.trim()) env = JSON.parse(envText);
    } catch (e) {
      setError(`Invalid JSON: ${e instanceof Error ? e.message : e}`);
      return;
    }
    try {
      const payload = { ...form, args, env };
      if (editing) await API.updateMcpServer(editing.id, payload);
      else await API.createMcpServer(payload);
      setDialogOpen(false);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    }
  }

  async function remove(id: number) {
    await API.deleteMcpServer(id);
    await load();
  }

  async function discover(id: number) {
    try {
      const res = await API.discoverMcpServer(id);
      if (res.error) setError(`Discovery failed: ${res.error}`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Discovery failed");
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Local (stdio) MCP servers. The agent can call their tools via{" "}
        <code>[MCP_CALL: …]</code> only when the backend is started with{" "}
        <code>ENABLE_MCP_TOOL=true</code>.
      </p>

      {error && <div className="text-sm text-red-500">{error}</div>}

      <Button size="sm" onClick={openAdd}>
        <Plus className="h-4 w-4 mr-1" /> Add server
      </Button>

      {loading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : (
        <ul className="space-y-2">
          {servers.map((s) => (
            <li key={s.id} className="rounded-lg border p-3 text-sm">
              <div className="flex items-center justify-between">
                <div>
                  <span className="font-medium">{s.name}</span>{" "}
                  <span className="text-muted-foreground">
                    {s.command} {s.args.join(" ")}
                  </span>
                  {!s.enabled && <span className="ml-2 text-xs text-muted-foreground">(disabled)</span>}
                </div>
                <div className="flex gap-1">
                  <Button size="icon" variant="ghost" onClick={() => discover(s.id)} title="Test / refresh tools">
                    <RefreshCw className="h-4 w-4" />
                  </Button>
                  <Button size="icon" variant="ghost" onClick={() => openEdit(s)} title="Edit">
                    <Pencil className="h-4 w-4" />
                  </Button>
                  <Button size="icon" variant="ghost" onClick={() => remove(s.id)} title="Delete">
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
              {s.last_error && <div className="mt-1 text-xs text-red-500">last error: {s.last_error}</div>}
              {s.tools.length > 0 && (
                <div className="mt-1 text-xs text-muted-foreground">
                  tools: {s.tools.map((t) => t.name).join(", ")}
                </div>
              )}
            </li>
          ))}
          {servers.length === 0 && (
            <li className="text-sm text-muted-foreground">No MCP servers configured.</li>
          )}
        </ul>
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit MCP server" : "Add MCP server"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label>Name (lowercase, digits, single underscores)</Label>
              <Input value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="local_fetch" />
            </div>
            <div>
              <Label>Command</Label>
              <Input value={form.command}
                onChange={(e) => setForm({ ...form, command: e.target.value })} />
            </div>
            <div>
              <Label>Args (JSON array)</Label>
              <textarea className="w-full rounded-md border bg-transparent p-2 text-sm font-mono"
                rows={2} value={argsText} onChange={(e) => setArgsText(e.target.value)}
                placeholder='["-m", "mcp_server_fetch"]' />
            </div>
            <div>
              <Label>Env (JSON object, optional)</Label>
              <textarea className="w-full rounded-md border bg-transparent p-2 text-sm font-mono"
                rows={2} value={envText} onChange={(e) => setEnvText(e.target.value)} />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.enabled ?? true}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
              Enabled
            </label>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDialogOpen(false)}>Cancel</Button>
            <Button onClick={save}>{editing ? "Save" : "Add"}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
```
> Verify the shadcn component import paths against `frontend/src/Settings.tsx` (it imports from `"./components/ui/dialog"`, `"./components/ui/button"`, etc.) — copy them exactly. If `Settings.tsx` uses a `<select>` with `selectClass` rather than a checkbox for booleans, match that style; the logic is unchanged.

- [ ] **Step 4: Mount in `frontend/src/Settings.tsx`**

- Extend the tab state type:
  ```tsx
  const [activeTab, setActiveTab] = useState<"providers" | "scheduler" | "mcp">("providers");
  ```
- Add `import { McpServers } from "./McpServers";` near the `ScheduledTasks` import.
- In the `<TabsList>`, add a trigger: `<TabsTrigger value="mcp">MCP</TabsTrigger>`.
- Add a matching `<TabsContent value="mcp"><McpServers /></TabsContent>`.

- [ ] **Step 5: Verify in the browser**

Frontend hot-reloads (`src/` bind-mount). Run: `docker compose ps` (frontend `Up`), open `http://localhost:5173`, log in, open Settings → **MCP** tab. Add a server: name `local_fetch`, command `python`, args `["-m","mcp_server_fetch"]`. Click "Test / refresh tools" → the `fetch` tool should appear (backend was rebuilt with `mcp-server-fetch` in Task 1).
Check `frontend/src` typecheck: `docker compose exec -T frontend npx tsc --noEmit` (expected: no errors).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types.ts frontend/src/api.ts frontend/src/McpServers.tsx frontend/src/Settings.tsx
git commit -m "feat(mcp): Settings MCP tab — server CRUD + tool discovery UI"
```

---

## Task 13: End-to-end verification + docs

**Files:**
- Modify: `THUNDER_AI_PRD.md`

- [ ] **Step 1: Clean rebuild**

Run: `docker compose up -d --build`
Expected: all three containers healthy.

- [ ] **Step 2: Backend test sweep**

Run:
```bash
docker compose exec -T backend pytest tests/test_async_bridge.py tests/test_mcp_service.py tests/test_tool_registry.py tests/test_mcp_tool.py tests/test_system_prompts.py tests/test_mcp_prompt_wiring.py tests/test_mcp_dispatch.py tests/test_mcp_routes.py -v
```
Expected: all pass.

- [ ] **Step 3: Manual end-to-end (feature OFF → ON)**

1. With `ENABLE_MCP_TOOL` unset (default), open the app, start a chat, ask a question containing a literal `[MCP_CALL: mcp__x__y {}]` — confirm the backend does **not** dispatch it (marker passes through as text) and no error.
2. Stop backend, set the env and restart:
   ```bash
   ENABLE_MCP_TOOL=true docker compose up -d backend
   ```
   (or add `ENABLE_MCP_TOOL=true` to a local `.env`).
3. Settings → MCP → add the stub or `mcp_server_fetch` server → "Test / refresh tools" → tools listed, `last_error` empty.
4. In a chat, ask something that makes the model call the tool (e.g. with the fetch server: *"Use your MCP fetch tool to get https://example.com and quote its first line."*). Confirm in `docker compose logs backend` that a subprocess spawns, the `=== MCP Tool Result (…) ===` block is injected, and (non-streaming path) the model regenerates using it. In the streaming UI, confirm the result block is appended to the reply.
5. Set the server's allow-list to exclude the tool → repeat → confirm the injected block says the tool is not allow-listed and **no subprocess spawns** (check logs).

- [ ] **Step 4: Update `THUNDER_AI_PRD.md`**

Change the `## Status` block:
```markdown
## Status

- **Phase 1 (MCP Integration): DONE** (branch `feat/mcp-integration`).
  Marker-based `[MCP_CALL]` dispatch, stdio Python MCP servers, Settings CRUD,
  tool-registry seam, default-off `ENABLE_MCP_TOOL`. Spec:
  `docs/superpowers/specs/2026-08-29-mcp-integration-design.md`.
- **Next:** Phase 2 — True Browser Automation & Multi-Agent UI.
```

- [ ] **Step 5: Commit + summarize**

```bash
git add THUNDER_AI_PRD.md
git commit -m "docs(mcp): mark Phase 1 (MCP integration) complete"
```
Then report: tests run + results, the manual e2e outcome, and any deviations from this plan.

---

## Self-Review

**1. Spec coverage**

| Spec section | Task(s) |
|---|---|
| §2 data model (`mcp_servers`, `MCPServer`) | 3 |
| §3 `async_bridge` extraction | 2 |
| §4 `mcp_service` (discover/call, sync bridge, verified SDK surface) | 1 (verify), 4 |
| §5 `tool_registry` (`RegisteredTool`, `list_tools`, `resolve_mcp`, namespacing) | 5 |
| §6 marker protocol + dispatch (`mcp_tool`, both paths) | 6 (parse), 11 (wire) |
| §7 system-prompt block | 8 (build), 10 (wire) |
| §8 Settings API + UI | 9 (API), 12 (UI) |
| §9 config & safety (`ENABLE_MCP_TOOL`, timeout, allow-list before spawn) | 4 (config), 7 (compose), 5/6 (allow-list enforcement) |
| §10 error handling (never 500; injected error blocks) | 4 (`MCPError`), 6 (`run_mcp_calls` try/except), 9 (`/discover` returns `{error}`) |
| §11 testing (stub server, unit + route + dispatch) | 2,4,5,6,8,9,10,11 |
| §12 build sequence | Tasks 1–13 in order |
| §13 files | File Structure section above |
| §14 out of scope | not implemented, by design |

No gaps.

**2. Placeholder scan** — no "TBD"/"handle edge cases"/"similar to Task N". Every code step has real code. The two "verify against the installed SDK" notes (Task 1 spike; `read_timeout_seconds` kwarg; `isError`/`is_error`) are explicit verification steps with concrete fallbacks, not deferrals.

**3. Type consistency**

- `MCPServerCfg` fields (`id, name, command, args: tuple, env, tool_allowlist: frozenset|None`) — identical in Task 4 (def), Task 5 (`from_row`, `resolve_mcp` return), Task 6 (`cfg` usage), Task 9 (`MCPServerCfg.from_row(row)`).
- `RegisteredTool(name, description, input_schema, source, server_id)` — identical in Task 5 (def), Task 8 (`build_mcp_tools_block` reads `.source`, `.input_schema`, `.description`, `.name`), Task 10 (test constructs it positionally).
- `discover_tools(cfg) -> list[dict]` with `{"name","description","inputSchema"}` — Task 4 def, Task 5 consumes `tools_json` of that shape, Task 9 `/discover` stores it, Task 12 `McpTool` type mirrors it (`inputSchema`).
- `call_tool(cfg, tool, arguments) -> tuple[str, bool]` — Task 4 def, Task 6 `run_mcp_calls` unpacks `(text, is_error)`.
- `extract_mcp_calls(text) -> list[tuple[str, dict]]` / `run_mcp_calls(calls, db, user_id) -> str` / `format_mcp_result(name, text, is_error) -> str` — consistent across Task 6 (def) and Task 11 (callers pass exactly these).
- `build_mcp_tools_block(tools) -> str` — Task 8 def, Task 10 callers pass `list_tools(...)` output.
- `list_tools(db, user_id)` / `resolve_mcp(db, user_id, namespaced)` — Task 5 def; Task 6, 10, 11 callers match arg order.
- `ChatContext.user_id` (Task 10) and the streaming dispatch uses `context.user_id` (Task 11) — consistent. `WorkflowState.mcp_result` (Task 10) set by `generate_answer` (Task 11), read by `_build_system_prompt` (Task 10) — consistent.

No mismatches found.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-08-29-mcp-integration.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
