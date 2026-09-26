# Phase 1B.2R MP-5 slice 1 - the synchronous wait-for-terminal adapter

Status: **slice 1 complete**; slices 2-4 not started (see `docs/superpowers/BACKLOG.md`).

## Why this slice

MP-5 (architecture.md:1296-1302) turns the non-streaming `/messages` route and
`scheduler_service` into callers of the durable engine. Both need the same
primitive: submit, then wait for the Run to become terminal without a browser.
That primitive is additive and dependency-free, so it is the safe first slice.

## What was built

`backend/app/run_engine/sync_wait.py::wait_for_terminal`

- read-only observer of the committed `Run` row; `db.expire_all()` per poll;
- reuses `state_machine.is_run_terminal` - no second terminal-state list;
- raises `SyncWaitError(code="sync_wait_timeout", run_id, status)` on the deadline,
  carrying the last OBSERVED status, so a timeout is never read as success;
- creates no Run, advances no Run, invokes no worker.

## Evidence

- `tests/test_mp5_messages_sync_adapter.py`: 6 passed, exit 0, first-run GREEN
  (this is a new primitive, so there is no RED to report).
- `import app.main` OK.
- Log: `secrets/mp5_slice1.log`.

## Deliberately NOT done

The flag-gated `/messages` cutover. It cannot be separated from retiring the
legacy terminal-approval encoding, which MP-4A gates on MP-4C + MP-4D, and two
approval authorities must not coexist. No flag was added, so there is no dead
configuration.