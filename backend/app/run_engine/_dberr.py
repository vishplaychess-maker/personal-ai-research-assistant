"""Narrow constraint-violation matching (Phase 1B.2R — MP-1A).

A RunEngine transactional path must never do a broad ``except IntegrityError``:
an unrelated NOT NULL / FK / other-unique failure would be silently swallowed
and misreported as an idempotency replay or a seq race. Every catch site names
the exact constraint it expects and re-raises anything else.

SQLite (pysqlite) surfaces the index / constraint name inside the error text —
for a partial or expression index (``uq_runs_idem_scope``, ``uq_run_signals_live``)
the message is ``UNIQUE constraint failed: index 'uq_runs_idem_scope'``; for a
plain table UNIQUE (``uq_run_events_run_seq``) it is
``UNIQUE constraint failed: run_events.run_id, run_events.run_seq``. PostgreSQL
(psycopg) attaches a structured ``.diag.constraint_name``.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

# table UNIQUE constraints whose SQLite message lists columns, not the name
_SQLITE_COLUMN_HINTS: dict[str, tuple[str, ...]] = {
    "uq_run_events_run_seq": ("run_events.run_id", "run_events.run_seq"),
}


def constraint_violation(exc: IntegrityError, name: str) -> bool:
    """True IFF ``exc`` is a unique/constraint violation for the constraint or
    index called ``name`` — on SQLite or PostgreSQL. Any other IntegrityError
    (or a violation of a different constraint) returns False so the caller
    re-raises it."""
    orig = getattr(exc, "orig", None)

    # PostgreSQL: structured diagnostics
    diag = getattr(orig, "diag", None)
    pg_name = getattr(diag, "constraint_name", None)
    if pg_name:
        return pg_name == name

    text = str(orig or exc)
    if "UNIQUE constraint failed" not in text and "constraint failed" not in text:
        return False
    if f"index '{name}'" in text or f'index "{name}"' in text:
        return True
    hints = _SQLITE_COLUMN_HINTS.get(name)
    if hints and all(h in text for h in hints):
        return True
    return False
