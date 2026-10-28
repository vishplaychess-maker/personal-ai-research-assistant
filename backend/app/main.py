import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text as sa_text

from app.config import settings
from app.database import init_db, engine, SessionLocal
from app.models.models import User

logger = logging.getLogger(__name__)


# ── Structured logging (Phase 5 — cloud deploy) ────────────
# When LOG_FORMAT=json, emit one JSON object per log line (stdlib only, no
# external dependency) and override uvicorn's handlers as well. Otherwise
# current default logging is kept untouched.
class _JsonLogFormatter(logging.Formatter):
    """Minimal JSON log formatter: timestamp, level, logger, message."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging():
    """Apply JSON logging when LOG_FORMAT=json; no-op otherwise.

    Runs at import time — uvicorn configures its loggers *before* importing
    the app, so overriding the uvicorn.* handlers here is safe and final.
    """
    if settings.log_format.strip().lower() != "json":
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False


_configure_logging()

from app.routes.sessions import router as sessions_router
from app.routes.messages import router as messages_router
from app.routes.documents import router as documents_router
from app.routes.memories import router as memories_router
from app.routes.settings import router as settings_router
from app.routes.models import router as models_router
from app.routes.providers import router as providers_router
from app.routes.directives import router as directives_router
from app.routes.skills import router as skills_router
from app.routes.capabilities import router as capabilities_router
from app.routes.search import router as search_router
from app.routes.auth import router as auth_router
from app.routes.scheduler import router as scheduler_router
from app.routes.mcp import router as mcp_router
from app.routes.mcp_registry import router as mcp_registry_router
from app.routes.share import router as share_router
from app.routes.knowledge_graph import router as knowledge_graph_router
from app.routes.export import router as export_router
from app.routes.terminal import router as terminal_router
from app.routes.media import router as media_router
from app.routes.approvals import router as approvals_router
from app.routes.runs import router as runs_router
from app.routes.vision import router as vision_router
from app.routes.setup import router as setup_router
from app.routes.oauth import router as oauth_router
from app.routes.desktop import router as desktop_router
from app.routes.openbot import router as openbot_router  # Phase 1 — Autonomous Research Agent queue
from app.routes.browser import router as browser_router  # Built-in browser — GET /api/browser/embeddable
from app.routes.custom_skills import router as custom_skills_router  # Custom skill marketplace - POST /api/skills/custom/{id}
from app.routes.open_interpreter import router as open_interpreter_router  # Open Interpreter - /api/interpreter/* (503 unless installed)
from app.services import access_guard, setup_service
from app.static_mount import mount_frontend


def _migrate_provider_encryption():
    """Ensure ``user_providers.encrypted_api_key`` exists and holds all keys.

    Dialect-agnostic (runs for SQLite and PostgreSQL alike — only plain
    ADD COLUMN / UPDATE statements). Moves any key stored in the legacy
    ``api_key`` column into ``encrypted_api_key``:

      - Already-encrypted Fernet tokens are copied as-is.
      - Legacy plaintext values are encrypted first (requires
        ENCRYPTION_KEY; if it is unset the row is left untouched and the
        migration retries on the next startup instead of crashing).
      - The legacy column is blanked after a successful move so no
        plaintext (or duplicate ciphertext) remains at rest.
    """
    from app.services.encryption import FERNET_PREFIX
    from app.services.encryption_service import encrypt_key

    inspector = inspect(engine)
    if "user_providers" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("user_providers")}
    with engine.connect() as conn:
        if "encrypted_api_key" not in existing_cols:
            conn.execute(sa_text("ALTER TABLE user_providers ADD COLUMN encrypted_api_key TEXT"))
            conn.commit()
        rows = conn.execute(
            sa_text("SELECT id, api_key, encrypted_api_key FROM user_providers")
        ).fetchall()
        for row_id, legacy, encrypted in rows:
            if (encrypted or "") or not (legacy or "").strip():
                continue
            value = legacy.strip()
            if not value.startswith(FERNET_PREFIX):
                try:
                    value = encrypt_key(value)
                except RuntimeError:
                    logger.warning(
                        "ENCRYPTION_KEY is not set - cannot encrypt the plaintext "
                        "API key stored for provider id=%s. The legacy value is "
                        "kept and encryption will be retried on the next start.",
                        row_id,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Could not encrypt the stored API key for provider "
                        "id=%s (%s); the legacy value is kept unchanged.",
                        row_id,
                        exc,
                    )
                    continue
            conn.execute(
                sa_text(
                    "UPDATE user_providers SET encrypted_api_key = :v, api_key = '' "
                    "WHERE id = :i"
                ),
                {"v": value, "i": row_id},
            )
        conn.commit()


def _migrate_knowledge_graph():
    """Drop the legacy *unscoped* knowledge-graph tables so init_db() rebuilds
    them user-scoped.

    graph_entities / graph_relations shipped briefly without a ``user_id``
    column (and with a globally-unique entity name). They hold only derived,
    re-extractable data and SQLite can't ALTER away the old UNIQUE constraint,
    so the clean fix is drop + recreate. Runs BEFORE init_db(); a no-op on a
    fresh database or one already carrying the ``user_id`` column.
    """
    inspector = inspect(engine)
    if "graph_entities" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("graph_entities")}
    if "user_id" in cols:
        return
    with engine.begin() as conn:
        conn.execute(sa_text("DROP TABLE IF EXISTS graph_relations"))
        conn.execute(sa_text("DROP TABLE IF EXISTS graph_entities"))
    logger.info(
        "Dropped legacy unscoped knowledge-graph tables; "
        "init_db() will recreate them user-scoped"
    )


def _migrate_user_skills():
    """Create the ``user_skills`` table if it does not exist yet.

    Dialect-agnostic and idempotent: a plain CREATE TABLE IF NOT EXISTS that
    runs for SQLite and PostgreSQL alike (fresh installs are also covered by
    init_db() -> Base.metadata.create_all(); this guards databases created
    before the table was added to the metadata).
    """
    inspector = inspect(engine)
    if "user_skills" in inspector.get_table_names():
        return
    with engine.connect() as conn:
        conn.execute(
            sa_text(
                """
                CREATE TABLE IF NOT EXISTS user_skills (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
                    name VARCHAR(50) NOT NULL,
                    description VARCHAR(200),
                    body TEXT,
                    trigger_keywords VARCHAR(400),
                    enabled BOOLEAN,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            sa_text("CREATE INDEX IF NOT EXISTS ix_user_skills_id ON user_skills (id)")
        )
        conn.execute(
            sa_text("CREATE INDEX IF NOT EXISTS ix_user_skills_user_id ON user_skills (user_id)")
        )
        conn.execute(
            sa_text("CREATE INDEX IF NOT EXISTS ix_user_skills_name ON user_skills (name)")
        )
        conn.commit()
    logger.info("Created user_skills table (custom skill creator)")


def _migrate_media_video_tasks():
    """Create the ``media_video_tasks`` table if it does not exist yet.

    Dialect-agnostic, idempotent CREATE TABLE IF NOT EXISTS (fresh installs are
    also covered by init_db() -> Base.metadata.create_all(); this guards
    databases created before the table was added to the metadata).
    """
    inspector = inspect(engine)
    if "media_video_tasks" in inspector.get_table_names():
        return
    with engine.connect() as conn:
        conn.execute(
            sa_text(
                """
                CREATE TABLE IF NOT EXISTS media_video_tasks (
                    task_id VARCHAR(200) PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
                    provider VARCHAR(40) NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )
                """
            )
        )
        conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_media_video_tasks_user_id "
                "ON media_video_tasks (user_id)"
            )
        )
        conn.commit()
    logger.info("Created media_video_tasks table (video task ownership)")


def _migrate_approval_domain():
    """Create the Phase 1B.1 approval-domain tables if they do not exist yet.

    Dialect-agnostic, idempotent CREATE TABLE IF NOT EXISTS (fresh installs are
    also covered by init_db() -> Base.metadata.create_all() once the models are
    imported). Additive only — no ALTER, no DROP. A code rollback leaves these
    tables in place and inert, retaining their audit data.
    """
    inspector = inspect(engine)
    have = set(inspector.get_table_names())
    if {"pending_approvals", "session_grants", "action_log", "action_log_heads"} <= have:
        return
    with engine.connect() as conn:
        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id VARCHAR(36) PRIMARY KEY,
                action_hash VARCHAR(64) NOT NULL,
                args_hash VARCHAR(64) NOT NULL,
                action_envelope_version VARCHAR(8) NOT NULL DEFAULT '1',
                tool_id VARCHAR(200) NOT NULL,
                server_fingerprint VARCHAR(64) NOT NULL,
                tool_schema_hash VARCHAR(64) NOT NULL,
                source_type VARCHAR(20) NOT NULL DEFAULT 'unknown',
                tenant_id VARCHAR(64),
                user_id INTEGER NOT NULL,
                session_id INTEGER,
                run_id VARCHAR(64),
                action_nonce VARCHAR(64) NOT NULL,
                policy_version VARCHAR(32) NOT NULL,
                risk_level VARCHAR(16) NOT NULL DEFAULT 'low',
                state VARCHAR(16) NOT NULL DEFAULT 'requested',
                display_summary TEXT NOT NULL DEFAULT '{}',
                purpose_redacted VARCHAR(512) NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL,
                decided_at TIMESTAMP,
                consumed_at TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_pending_approvals_nonce UNIQUE (action_nonce)
            )
            """
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_action_hash ON pending_approvals (action_hash)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_user_id ON pending_approvals (user_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_tenant_id ON pending_approvals (tenant_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_session_id ON pending_approvals (session_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_state ON pending_approvals (state)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_user_state ON pending_approvals (user_id, state)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_tenant_state ON pending_approvals (tenant_id, state)"
        ))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS session_grants (
                id VARCHAR(36) PRIMARY KEY,
                grant_nonce VARCHAR(64) NOT NULL,
                grant_scope_version VARCHAR(8) NOT NULL DEFAULT '1',
                tenant_id VARCHAR(64),
                user_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                tool_id VARCHAR(200) NOT NULL,
                server_fingerprint VARCHAR(64) NOT NULL,
                tool_schema_hash VARCHAR(64) NOT NULL,
                grant_scope_hash VARCHAR(64) NOT NULL,
                policy_version VARCHAR(32) NOT NULL,
                creating_approval_id VARCHAR(36) NOT NULL,
                created_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                revoked_at TIMESTAMP,
                CONSTRAINT uq_session_grants_nonce UNIQUE (grant_nonce)
            )
            """
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_session_grants_user_id ON session_grants (user_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_session_grants_tenant_id ON session_grants (tenant_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_session_grants_session_id ON session_grants (session_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_session_grants_lookup ON session_grants (user_id, session_id, tool_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_session_grants_scope ON session_grants (grant_scope_hash)"
        ))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS action_log (
                id VARCHAR(36) PRIMARY KEY,
                chain_scope_id VARCHAR(80) NOT NULL,
                seq BIGINT NOT NULL,
                event_type VARCHAR(40) NOT NULL,
                approval_id VARCHAR(36),
                grant_id VARCHAR(36),
                action_hash VARCHAR(64),
                event_metadata TEXT NOT NULL DEFAULT '{}',
                prev_row_hash VARCHAR(64) NOT NULL,
                row_hash VARCHAR(64) NOT NULL,
                created_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_action_log_scope_seq UNIQUE (chain_scope_id, seq),
                CONSTRAINT uq_action_log_row_hash UNIQUE (row_hash)
            )
            """
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_action_log_chain_scope_id ON action_log (chain_scope_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_action_log_approval_id ON action_log (approval_id)"
        ))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS action_log_heads (
                chain_scope_id VARCHAR(80) PRIMARY KEY,
                seq BIGINT NOT NULL DEFAULT 0,
                row_hash VARCHAR(64) NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            """
        ))
        conn.commit()
    logger.info("Created approval-domain tables (Phase 1B.1: pending_approvals, "
                "session_grants, action_log, action_log_heads)")


# Current `session_grants` schema (post Phase 1B.1 close-out). Kept next to the
# 1B.2 reconciliation so an empty-legacy rebuild uses exactly this shape.
_SESSION_GRANTS_DDL_CURRENT = """
    CREATE TABLE IF NOT EXISTS session_grants (
        id VARCHAR(36) PRIMARY KEY,
        grant_nonce VARCHAR(64) NOT NULL,
        grant_scope_version VARCHAR(8) NOT NULL DEFAULT '1',
        tenant_id VARCHAR(64),
        user_id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        tool_id VARCHAR(200) NOT NULL,
        server_fingerprint VARCHAR(64) NOT NULL,
        tool_schema_hash VARCHAR(64) NOT NULL,
        grant_scope_hash VARCHAR(64) NOT NULL,
        policy_version VARCHAR(32) NOT NULL,
        creating_approval_id VARCHAR(36) NOT NULL,
        created_at TIMESTAMP NOT NULL,
        expires_at TIMESTAMP NOT NULL,
        revoked_at TIMESTAMP,
        CONSTRAINT uq_session_grants_nonce UNIQUE (grant_nonce)
    )
"""
_SESSION_GRANTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_session_grants_user_id ON session_grants (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_grants_tenant_id ON session_grants (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_grants_session_id ON session_grants (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_grants_lookup ON session_grants (user_id, session_id, tool_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_grants_scope ON session_grants (grant_scope_hash)",
)
_SESSION_GRANTS_REQUIRED_COLUMNS = frozenset({
    "id", "grant_nonce", "grant_scope_version", "tenant_id", "user_id",
    "session_id", "tool_id", "server_fingerprint", "tool_schema_hash",
    "grant_scope_hash", "policy_version", "creating_approval_id",
    "created_at", "expires_at", "revoked_at",
})


def _create_session_grants_table_current():
    """(Re)create `session_grants` with the current DDL + all indexes."""
    with engine.connect() as conn:
        conn.execute(sa_text(_SESSION_GRANTS_DDL_CURRENT))
        for ddl in _SESSION_GRANTS_INDEXES:
            conn.execute(sa_text(ddl))
        conn.commit()


def _verify_session_grants_schema():
    """Assert the rebuilt `session_grants` carries every required column, the
    grant-nonce uniqueness constraint, and no obsolete column. Raises
    RuntimeError (halting startup) on any discrepancy."""
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("session_grants")}
    missing = _SESSION_GRANTS_REQUIRED_COLUMNS - cols
    if missing:
        raise RuntimeError(
            f"session_grants schema verification failed: missing columns {sorted(missing)}"
        )
    if "allowed_action_hash" in cols:
        raise RuntimeError(
            "session_grants schema verification failed: obsolete column "
            "`allowed_action_hash` is still present after rebuild"
        )
    uniques = insp.get_unique_constraints("session_grants")
    unique_cols = {tuple(u.get("column_names") or []) for u in uniques}
    idx_unique = {
        tuple(i.get("column_names") or [])
        for i in insp.get_indexes("session_grants") if i.get("unique")
    }
    if ("grant_nonce",) not in unique_cols and ("grant_nonce",) not in idx_unique:
        raise RuntimeError(
            "session_grants schema verification failed: grant_nonce uniqueness "
            "constraint/index is missing after rebuild"
        )
    index_names = {i["name"] for i in insp.get_indexes("session_grants")}
    for want in ("ix_session_grants_lookup", "ix_session_grants_scope"):
        if want not in index_names:
            raise RuntimeError(
                f"session_grants schema verification failed: index {want!r} missing"
            )
    logger.info("session_grants schema verified after 1B.2 reconciliation rebuild")


def _migrate_approval_domain_1b2():
    """Phase 1B.2 — trusted registry, execution identity, proposal dedupe,
    dispatch idempotency. Additive, idempotent, SQLite + PostgreSQL.

    Adds 4 nullable columns to ``pending_approvals`` (guarded ALTER for a DB
    already carrying the Phase 1B.1 tables), 4 new tables, and a partial-unique
    index enforcing one ACTIVE PendingApproval per logical proposal. Never
    rewrites data.

    ``session_grants`` reconciliation (Decision 1): a straddling DB that still
    has the obsolete ``allowed_action_hash`` column is handled as — EMPTY: a
    guarded DROP + recreate with the current DDL, followed by schema
    verification; NON-EMPTY: startup is HALTED with an actionable error (those
    rows have no safe mapping and are never transformed or dropped).
    """
    inspector = inspect(engine)
    have = set(inspector.get_table_names())
    if "pending_approvals" in have:
        cols = {c["name"] for c in inspector.get_columns("pending_approvals")}
        with engine.begin() as conn:
            for col, ddl in (
                ("proposal_dedupe_key", "ALTER TABLE pending_approvals ADD COLUMN proposal_dedupe_key VARCHAR(64)"),
                ("execution_turn_id", "ALTER TABLE pending_approvals ADD COLUMN execution_turn_id VARCHAR(36)"),
                ("tool_call_id", "ALTER TABLE pending_approvals ADD COLUMN tool_call_id VARCHAR(36)"),
                ("trusted_envelope_json", "ALTER TABLE pending_approvals ADD COLUMN trusted_envelope_json TEXT"),
            ):
                if col not in cols:
                    conn.execute(sa_text(ddl))
    # session_grants: reconcile a table created before the Phase 1B.1 close-out
    # (which renamed allowed_action_hash -> grant_scope_hash and added
    # grant_nonce / grant_scope_version). The 1B.1 helper uses
    # CREATE TABLE IF NOT EXISTS, so a DB that straddled the close-out keeps the
    # old, broken shape and new SessionGrant creation would fail later.
    #
    # Reconciliation (Phase 1B.2 correction — Decision 1):
    #   * legacy `allowed_action_hash` column present + table EMPTY  -> guarded
    #     DROP + recreate with the current DDL, then verify the rebuilt schema.
    #   * legacy column present + table NON-EMPTY                    -> FAIL FAST
    #     (halt startup with an actionable error). `allowed_action_hash` (a
    #     one-time action hash) has NO safe mapping to `grant_scope_hash` (a
    #     reusable scope template); those pre-close-out rows were never
    #     resolvable grants. We never transform or silently drop them.
    #   * legacy column absent                                       -> no-op
    #     (already the current shape; idempotent).
    if "session_grants" in have and "allowed_action_hash" in {
        c["name"] for c in inspector.get_columns("session_grants")
    }:
        with engine.connect() as conn:
            n = conn.execute(sa_text("SELECT COUNT(*) FROM session_grants")).scalar() or 0
        if n:
            raise RuntimeError(
                "startup halted: table `session_grants` still has the obsolete "
                f"`allowed_action_hash` column AND {n} row(s). These rows predate "
                "the Phase 1B.1 approval-domain close-out and were never resolvable "
                "grants; `allowed_action_hash` (a one-time action hash) has no safe "
                "mapping to `grant_scope_hash` (a reusable scope template), so this "
                "migration will not transform or silently discard them. Back up the "
                "database, then manually `DROP TABLE session_grants;` (or delete its "
                "rows) and restart — the additive migration will recreate it with "
                "the current schema and verify it."
            )
        with engine.begin() as conn:
            conn.execute(sa_text("DROP TABLE session_grants"))
        _create_session_grants_table_current()
        _verify_session_grants_schema()
        have.add("session_grants")

    with engine.connect() as conn:
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_dedupe "
            "ON pending_approvals (tenant_id, proposal_dedupe_key)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_turn "
            "ON pending_approvals (execution_turn_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_call "
            "ON pending_approvals (tool_call_id)"
        ))
        # One ACTIVE proposal per (tenant, dedupe key). COALESCE keeps NULL
        # tenant (self-host) rows deduping; partial-index syntax is identical on
        # SQLite (>=3.8) and PostgreSQL.
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_approvals_active_dedupe "
            "ON pending_approvals (COALESCE(tenant_id, ''), proposal_dedupe_key) "
            "WHERE proposal_dedupe_key IS NOT NULL AND state IN ('requested', 'approved')"
        ))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS trusted_tools (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(64),
                owner_user_id INTEGER NOT NULL,
                tool_id VARCHAR(200) NOT NULL,
                tool_version VARCHAR(64) NOT NULL DEFAULT '',
                source_type VARCHAR(20) NOT NULL DEFAULT 'mcp',
                mcp_server_id INTEGER,
                mcp_server_name VARCHAR(64),
                input_schema_json TEXT NOT NULL DEFAULT '{}',
                tool_schema_hash VARCHAR(64) NOT NULL,
                server_fingerprint VARCHAR(64) NOT NULL,
                config_ref_id VARCHAR(120),
                enabled BOOLEAN NOT NULL DEFAULT 1,
                locked BOOLEAN NOT NULL DEFAULT 1,
                lock_reason VARCHAR(120),
                risk_metadata_json TEXT NOT NULL DEFAULT '{}',
                policy_version VARCHAR(32) NOT NULL DEFAULT '1b1.1',
                discovered_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_trusted_tools_owner_tool UNIQUE (owner_user_id, tool_id)
            )
            """
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_trusted_tools_lookup "
            "ON trusted_tools (owner_user_id, tool_id, enabled, locked)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_trusted_tools_tenant ON trusted_tools (tenant_id)"
        ))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS execution_turns (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(64),
                user_id INTEGER NOT NULL,
                session_id INTEGER,
                run_id VARCHAR(64),
                assistant_message_id INTEGER,
                kind VARCHAR(20) NOT NULL DEFAULT 'reference',
                created_at TIMESTAMP NOT NULL
            )
            """
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_execution_turns_owner "
            "ON execution_turns (user_id, session_id)"
        ))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS tool_calls (
                id VARCHAR(36) PRIMARY KEY,
                execution_turn_id VARCHAR(36) NOT NULL,
                tenant_id VARCHAR(64),
                user_id INTEGER NOT NULL,
                session_id INTEGER,
                call_index INTEGER NOT NULL DEFAULT 0,
                tool_id VARCHAR(200) NOT NULL,
                args_hash VARCHAR(64) NOT NULL,
                created_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_tool_calls_turn_index UNIQUE (execution_turn_id, call_index)
            )
            """
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_tool_calls_owner ON tool_calls (user_id, session_id)"
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_tool_calls_turn ON tool_calls (execution_turn_id)"
        ))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS dispatch_records (
                id VARCHAR(36) PRIMARY KEY,
                tool_call_id VARCHAR(36) NOT NULL,
                tenant_id VARCHAR(64),
                user_id INTEGER NOT NULL,
                approval_id VARCHAR(36),
                grant_id VARCHAR(36),
                action_hash VARCHAR(64) NOT NULL,
                outcome VARCHAR(16) NOT NULL DEFAULT 'started',
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                CONSTRAINT uq_dispatch_records_tool_call UNIQUE (tool_call_id)
            )
            """
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_dispatch_records_owner ON dispatch_records (user_id)"
        ))
        conn.commit()
    logger.info("Created approval-gateway tables (Phase 1B.2: trusted_tools, "
                "execution_turns, tool_calls, dispatch_records; pending_approvals "
                "+proposal_dedupe_key/+execution_turn_id/+tool_call_id)")


# ── Durable run engine (Phase 1B.2R — MP-0 schema foundation) ─────────

_RUN_ENGINE_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "runs": frozenset({
        "id", "tenant_id", "user_id", "session_id", "thread_id", "kind",
        "parent_run_id", "status", "status_reason", "idempotency_key",
        "idempotency_identity_hash", "auth_session_ref", "version",
        "lease_owner", "lease_expires_at", "run_deadline_at",
        "pending_approval_id", "last_event_seq", "reconciled_by",
        "reconciled_at", "created_at", "updated_at", "started_at", "finished_at",
    }),
    "run_steps": frozenset({
        "id", "run_id", "tenant_id", "user_id", "session_id", "seq", "kind",
        "engine", "status", "execution_turn_id", "tool_call_id", "approval_id",
        "attempt", "input_ref", "output_ref", "input_payload_id",
        "output_payload_id", "error_code", "version", "lease_owner",
        "lease_expires_at", "created_at", "started_at", "finished_at",
    }),
    "run_events": frozenset({
        "id", "run_id", "run_seq", "tenant_id", "user_id", "category", "type",
        "data", "created_at",
    }),
    "run_signals": frozenset({
        "id", "run_id", "kind", "payload", "created_by_user_id",
        "created_by_tenant_id", "consumed_at", "created_at",
    }),
    "run_payloads": frozenset({
        "id", "tenant_id", "user_id", "run_id", "step_id", "payload_type",
        "enc_scheme", "ciphertext", "envelope_action_hash",
        "byte_len", "expires_at", "deleted_at", "created_at",
    }),
}

# Columns an earlier (round-1) MP-0 iteration created that the authoritative
# schema no longer creates or requires. If such a column is present it is NOT
# inert — a NOT NULL obsolete column rejects authoritative RunPayload inserts.
# `_reconcile_run_payloads_obsolete_column` repairs an EMPTY stale table and
# FAILS FAST on a non-empty one; `_verify_run_engine_schema` then hard-fails if
# any obsolete column somehow survived.
_RUN_ENGINE_OBSOLETE_COLUMNS: dict[str, frozenset[str]] = {
    "run_payloads": frozenset({"plaintext_sha256"}),
}

# The authoritative `run_payloads` shape — ONE source of truth, used by both the
# normal create path and the empty-stale-table rebuild. No `plaintext_sha256`:
# the confidentiality model is a context-bound encrypted envelope
# (app.run_engine.payloads), not an adjacent unkeyed digest.
_RUN_PAYLOADS_DDL = """
    CREATE TABLE IF NOT EXISTS run_payloads (
        id VARCHAR(36) PRIMARY KEY,
        tenant_id VARCHAR(64),
        user_id INTEGER NOT NULL,
        run_id VARCHAR(36) NOT NULL,
        step_id VARCHAR(36),
        payload_type VARCHAR(32) NOT NULL,
        enc_scheme VARCHAR(24) NOT NULL DEFAULT 'fernet.v1',
        ciphertext TEXT NOT NULL,
        envelope_action_hash VARCHAR(64),
        byte_len INTEGER NOT NULL,
        expires_at TIMESTAMP,
        deleted_at TIMESTAMP,
        created_at TIMESTAMP NOT NULL
    )
"""
_RUN_PAYLOADS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_run_payloads_run ON run_payloads (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_run_payloads_step ON run_payloads (step_id)",
    "CREATE INDEX IF NOT EXISTS ix_run_payloads_owner ON run_payloads (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_run_payloads_expiry ON run_payloads (expires_at)",
)


def _stale_run_payloads_nonempty_error(obsolete, row_count: int) -> "RuntimeError":
    """A non-secret, actionable fail-fast message. Never names a value from the
    table — only the obsolete column name(s) and the row count."""
    return RuntimeError(
        "startup halted: table `run_payloads` is the obsolete round-1 Phase 1B.2R "
        f"MP-0 development schema (column(s) {sorted(obsolete)}) and contains "
        f"{row_count} row(s). Those rows' ciphertext predates the context-bound "
        "encrypted-envelope format and cannot be read or transformed by the current "
        "code; MP-0 will NOT delete, rewrite, or copy them. Make an explicit "
        "migration/recovery decision — e.g. back up the database and "
        "`DROP TABLE run_payloads` (local dev payload data is transient), or restore "
        "a pre-round-1 backup — then restart. No data was modified. (No deployed / "
        "persisted run_payloads data exists; this only affects a local dev DB that "
        "ran the earlier MP-0 iteration.)"
    )


def _repair_or_fail_run_payloads_sqlite(obsolete) -> None:
    """SQLite: rebuild an EMPTY stale `run_payloads` to the authoritative shape
    atomically; fail fast on a non-empty one.

    Atomicity: an explicit ``BEGIN IMMEDIATE`` takes a RESERVED lock (no
    concurrent insert can slip between the row-count check and the drop), and —
    unlike SQLAlchemy's auto-commit-DDL path — DDL inside an explicit BEGIN is
    fully rolled back on error, so a crash/exception leaves the stale table and
    every row untouched. Idempotent: a second run sees no obsolete column and
    returns.
    """
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row_count = cur.execute("SELECT COUNT(*) FROM run_payloads").fetchone()[0]
        if row_count:
            raw.rollback()
            raise _stale_run_payloads_nonempty_error(obsolete, row_count)
        cur.execute("DROP TABLE run_payloads")
        cur.execute(_RUN_PAYLOADS_DDL)
        for ix in _RUN_PAYLOADS_INDEXES:
            cur.execute(ix)
        raw.commit()
    except RuntimeError:
        raise
    except Exception:
        try:
            raw.rollback()
        except Exception:
            pass
        raise
    finally:
        raw.close()
    logger.info("run-engine: repaired obsolete run_payloads schema on SQLite "
                "(empty table; dropped %s, rebuilt authoritative shape)", sorted(obsolete))


def _repair_or_fail_run_payloads_postgres(obsolete) -> None:
    """PostgreSQL: DDL is fully transactional — the whole repair (or the
    fail-fast) is one atomic ``engine.begin()``. NOT verified in this
    environment (see _verify_run_engine_schema / the migration tests)."""
    with engine.begin() as conn:
        row_count = conn.execute(sa_text("SELECT COUNT(*) FROM run_payloads")).scalar() or 0
        if row_count:
            raise _stale_run_payloads_nonempty_error(obsolete, row_count)
        conn.execute(sa_text("DROP TABLE run_payloads"))
        conn.execute(sa_text(_RUN_PAYLOADS_DDL))
        for ix in _RUN_PAYLOADS_INDEXES:
            conn.execute(sa_text(ix))
    logger.info("run-engine: repaired obsolete run_payloads schema on PostgreSQL "
                "(empty table; dropped %s)", sorted(obsolete))


def _reconcile_run_payloads_obsolete_column() -> None:
    """Before the authoritative `run_payloads` is (re)created: if a stale
    round-1 MP-0 dev schema is present (any obsolete column, NOT NULL or not),
    repair it when the table is EMPTY, else FAIL FAST. Never assumes an obsolete
    column is harmless. Runs on every startup; idempotent."""
    insp = inspect(engine)
    if "run_payloads" not in insp.get_table_names():
        return  # clean install — the CREATE below builds the authoritative shape
    cols = {c["name"] for c in insp.get_columns("run_payloads")}
    obsolete = _RUN_ENGINE_OBSOLETE_COLUMNS.get("run_payloads", frozenset()) & cols
    if not obsolete:
        return  # already authoritative
    dialect = engine.dialect.name
    if dialect == "sqlite":
        _repair_or_fail_run_payloads_sqlite(obsolete)
    elif dialect == "postgresql":
        _repair_or_fail_run_payloads_postgres(obsolete)
    else:
        raise RuntimeError(
            f"startup halted: `run_payloads` carries the obsolete round-1 MP-0 "
            f"column(s) {sorted(obsolete)} and automatic repair is implemented only "
            f"for SQLite and PostgreSQL, not {dialect!r}. Recreate `run_payloads` "
            "with the current 13-column schema and restart. No data was modified."
        )

# partial-UNIQUE indexes (expression / WHERE — SQLite >= 3.8 + PostgreSQL);
# reflected by neither SQLAlchemy's get_indexes nor get_unique_constraints, so
# _verify_run_engine_schema checks the catalogue text directly on SQLite.
_RUN_ENGINE_PARTIAL_UNIQUE = {
    "uq_runs_idem_scope": (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_idem_scope ON runs "
        "(COALESCE(tenant_id, ''), user_id, COALESCE(session_id, -1), idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    ),
    "uq_run_signals_live": (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_signals_live ON run_signals "
        "(run_id, kind) WHERE consumed_at IS NULL"
    ),
}


def _migrate_run_engine():
    """Phase 1B.2R MP-0 — create the durable run-engine tables.

    Additive, idempotent, dialect-agnostic (``CREATE TABLE / INDEX IF NOT
    EXISTS``). Creates 5 new tables + their indexes + two partial-UNIQUE
    indexes. Touches NO existing table — no ``ALTER``, no ``DROP``, no data
    move. A code rollback leaves these tables inert, retaining their data.

    ``execution_turns.run_id`` (an existing Phase 1B.2 ``VARCHAR(64)`` column)
    is REUSED as the link to ``runs.id`` in the run-engine era — no new column
    is added to ``execution_turns``.

    ``run_payloads`` exception: a stale round-1 dev schema (with the obsolete
    ``plaintext_sha256`` column) is reconciled first — an EMPTY table is rebuilt
    to the authoritative shape atomically; a NON-EMPTY one fails startup fast
    without touching data (see ``_reconcile_run_payloads_obsolete_column``).
    """
    _reconcile_run_payloads_obsolete_column()
    with engine.connect() as conn:
        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id VARCHAR(36) PRIMARY KEY,
                tenant_id VARCHAR(64),
                user_id INTEGER NOT NULL,
                session_id INTEGER,
                thread_id VARCHAR(64) NOT NULL,
                kind VARCHAR(24) NOT NULL DEFAULT 'chat_stream',
                parent_run_id VARCHAR(36),
                status VARCHAR(24) NOT NULL DEFAULT 'created',
                status_reason VARCHAR(64),
                idempotency_key VARCHAR(80),
                idempotency_identity_hash VARCHAR(64),
                auth_session_ref VARCHAR(64),
                version INTEGER NOT NULL DEFAULT 0,
                lease_owner VARCHAR(64),
                lease_expires_at TIMESTAMP,
                run_deadline_at TIMESTAMP,
                pending_approval_id VARCHAR(36),
                last_event_seq INTEGER NOT NULL DEFAULT 0,
                reconciled_by VARCHAR(64),
                reconciled_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        ))
        for ddl in (
            "CREATE INDEX IF NOT EXISTS ix_runs_status_lease ON runs (status, lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS ix_runs_owner_created ON runs (user_id, session_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_runs_pending_approval ON runs (pending_approval_id)",
            "CREATE INDEX IF NOT EXISTS ix_runs_parent ON runs (parent_run_id)",
            "CREATE INDEX IF NOT EXISTS ix_runs_tenant_status ON runs (tenant_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_runs_idem ON runs (tenant_id, user_id, session_id, idempotency_key)",
        ):
            conn.execute(sa_text(ddl))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS run_steps (
                id VARCHAR(36) PRIMARY KEY,
                run_id VARCHAR(36) NOT NULL,
                tenant_id VARCHAR(64),
                user_id INTEGER NOT NULL,
                session_id INTEGER,
                seq INTEGER NOT NULL,
                kind VARCHAR(32) NOT NULL,
                engine VARCHAR(16) NOT NULL DEFAULT 'native',
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                execution_turn_id VARCHAR(36),
                tool_call_id VARCHAR(36),
                approval_id VARCHAR(36),
                attempt INTEGER NOT NULL DEFAULT 0,
                input_ref TEXT,
                output_ref TEXT,
                input_payload_id VARCHAR(36),
                output_payload_id VARCHAR(36),
                error_code VARCHAR(48),
                version INTEGER NOT NULL DEFAULT 0,
                lease_owner VARCHAR(64),
                lease_expires_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                CONSTRAINT uq_run_steps_run_seq UNIQUE (run_id, seq)
            )
            """
        ))
        for ddl in (
            "CREATE INDEX IF NOT EXISTS ix_run_steps_status_lease ON run_steps (status, lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS ix_run_steps_tool_call ON run_steps (tool_call_id)",
            "CREATE INDEX IF NOT EXISTS ix_run_steps_owner ON run_steps (user_id, session_id)",
        ):
            conn.execute(sa_text(ddl))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                id VARCHAR(36) PRIMARY KEY,
                run_id VARCHAR(36) NOT NULL,
                run_seq INTEGER NOT NULL,
                tenant_id VARCHAR(64),
                user_id INTEGER NOT NULL,
                category VARCHAR(16) NOT NULL DEFAULT 'control',
                type VARCHAR(32) NOT NULL,
                data TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_run_events_run_seq UNIQUE (run_id, run_seq)
            )
            """
        ))
        for ddl in (
            "CREATE INDEX IF NOT EXISTS ix_run_events_run ON run_events (run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_run_events_owner ON run_events (user_id)",
        ):
            conn.execute(sa_text(ddl))

        conn.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS run_signals (
                id VARCHAR(36) PRIMARY KEY,
                run_id VARCHAR(36) NOT NULL,
                kind VARCHAR(24) NOT NULL,
                payload TEXT,
                created_by_user_id INTEGER NOT NULL,
                created_by_tenant_id VARCHAR(64),
                consumed_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL
            )
            """
        ))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_run_signals_run_kind ON run_signals (run_id, kind)"
        ))

        conn.execute(sa_text(_RUN_PAYLOADS_DDL))   # authoritative 13-col shape (§ constants)
        for ddl in _RUN_PAYLOADS_INDEXES:
            conn.execute(sa_text(ddl))

        for ddl in _RUN_ENGINE_PARTIAL_UNIQUE.values():
            conn.execute(sa_text(ddl))

        conn.commit()
    logger.info("Created durable run-engine tables (Phase 1B.2R MP-0: runs, "
                "run_steps, run_events, run_signals, run_payloads)")


def _verify_run_engine_schema():
    """Assert the run-engine tables carry every required column, the row/seq
    uniqueness constraints, and the two partial-UNIQUE indexes. Raises
    RuntimeError (halting startup) on any discrepancy."""
    insp = inspect(engine)
    have = set(insp.get_table_names())
    missing_tables = set(_RUN_ENGINE_REQUIRED_COLUMNS) - have
    if missing_tables:
        raise RuntimeError(
            f"run-engine schema verification failed: missing tables {sorted(missing_tables)}"
        )
    for table, required in _RUN_ENGINE_REQUIRED_COLUMNS.items():
        cols = {c["name"] for c in insp.get_columns(table)}
        missing = required - cols
        if missing:
            raise RuntimeError(
                f"run-engine schema verification failed: {table} missing columns {sorted(missing)}"
            )
        # An obsolete round-1 column must NOT survive to here: it would reject
        # authoritative inserts (verification success would be a lie).
        # `_reconcile_run_payloads_obsolete_column` repairs an empty stale table
        # and fails fast on a non-empty one, so reaching this branch means the
        # reconcile did not run (bug / unsupported dialect) — hard fail.
        stale = (_RUN_ENGINE_OBSOLETE_COLUMNS.get(table, frozenset())) & cols
        if stale:
            raise RuntimeError(
                f"run-engine schema verification failed: {table} still carries "
                f"obsolete column(s) {sorted(stale)} that reject authoritative "
                "inserts — the guarded empty-table repair did not run "
                "(_reconcile_run_payloads_obsolete_column)"
            )

    def _has_unique(table: str, cols: tuple[str, ...]) -> bool:
        uq = {tuple(u.get("column_names") or []) for u in insp.get_unique_constraints(table)}
        ix = {
            tuple(i.get("column_names") or [])
            for i in insp.get_indexes(table) if i.get("unique")
        }
        return cols in uq or cols in ix

    if not _has_unique("run_steps", ("run_id", "seq")):
        raise RuntimeError(
            "run-engine schema verification failed: run_steps UNIQUE(run_id, seq) missing"
        )
    if not _has_unique("run_events", ("run_id", "run_seq")):
        raise RuntimeError(
            "run-engine schema verification failed: run_events UNIQUE(run_id, run_seq) missing"
        )

    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            names = {
                r[0] for r in conn.execute(sa_text(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )).fetchall()
            }
        for want in _RUN_ENGINE_PARTIAL_UNIQUE:
            if want not in names:
                raise RuntimeError(
                    f"run-engine schema verification failed: partial-unique index "
                    f"{want!r} missing"
                )
    logger.info("run-engine schema verified (Phase 1B.2R MP-0)")


def _migrate_scheduled_tasks_mp5():
    """MP-5 scheduler-on-engine: bind a scheduled task to the login family that
    created it. Additive, idempotent, dialect-agnostic (no data rewrite).

    Nullable on purpose: rows created before this slice keep NULL and the engine
    path FAILS CLOSED for them instead of inventing a binding. Guarded ALTER
    mirrors ``_migrate_approval_domain_1b2``.
    """
    inspector = inspect(engine)
    if "scheduled_tasks" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("scheduled_tasks")}
    with engine.begin() as conn:
        for col, ddl in (
            ("auth_session_ref",
             "ALTER TABLE scheduled_tasks ADD COLUMN auth_session_ref VARCHAR(64)"),
            ("tenant_id",
             "ALTER TABLE scheduled_tasks ADD COLUMN tenant_id VARCHAR(64)"),
            ("pending_fire_key",
             "ALTER TABLE scheduled_tasks ADD COLUMN pending_fire_key VARCHAR(80)"),
        ):
            if col not in cols:
                conn.execute(sa_text(ddl))


def _migrate_pending_approvals_content_hash():
    """MP-4C decision 2 follow-up - nullable, indexed content hash on
    ``pending_approvals`` for duplicate-marker suppression. Additive,
    idempotent, dialect-agnostic. Stores a sha256 hex DIGEST only (never the
    marker content)."""
    inspector = inspect(engine)
    if "pending_approvals" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("pending_approvals")}
    with engine.begin() as conn:
        if "content_hash" not in cols:
            conn.execute(sa_text(
                "ALTER TABLE pending_approvals ADD COLUMN content_hash VARCHAR(80)"))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_pending_approvals_content_hash "
            "ON pending_approvals (content_hash)"))


def _migrate_memories_source_columns():
    """YouTube Knowledge Ingestion (Phase 1) - nullable, additive source
    attribution on ``memories``. Additive, idempotent, dialect-agnostic.

    Stores the SOURCE identity only (url / kind / title / bounded non-secret
    provenance). Transcript and extracted-fact CONTENT live in the existing
    ``content`` column; nothing here duplicates or widens content storage."""
    inspector = inspect(engine)
    if "memories" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("memories")}
    with engine.begin() as conn:
        if "source_url" not in cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN source_url TEXT"))
        if "source_title" not in cols:
            conn.execute(sa_text(
                "ALTER TABLE memories ADD COLUMN source_title VARCHAR(500)"))
        if "source_kind" not in cols:
            conn.execute(sa_text(
                "ALTER TABLE memories ADD COLUMN source_kind VARCHAR(50)"))
        if "source_meta" not in cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN source_meta TEXT"))
        if "content_hash" not in cols:
            conn.execute(sa_text(
                "ALTER TABLE memories ADD COLUMN content_hash VARCHAR(80)"))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_memories_source_url "
            "ON memories (source_url)"))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_memories_source_kind "
            "ON memories (source_kind)"))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_memories_content_hash "
            "ON memories (content_hash)"))


def _migrate_database():
    """Add new columns to existing tables without losing data.

    Dialect guard (Phase 5 — cloud deploy): the raw DDL below is historical
    SQLite migration SQL (AUTOINCREMENT, DROP COLUMN fallbacks). It only runs
    when the engine dialect is sqlite. On PostgreSQL a fresh install is fully
    covered by init_db() -> Base.metadata.create_all(); the historical SQLite
    migrations are deliberately NOT translated.
    """
    # API-key encryption at rest: dialect-agnostic column + data move.
    _migrate_provider_encryption()

    # Custom skill creator: dialect-agnostic table creation (idempotent).
    _migrate_user_skills()

    # Video task ownership: dialect-agnostic table creation (idempotent).
    _migrate_media_video_tasks()

    # Approval domain (Phase 1B.1): dialect-agnostic table creation (idempotent).
    _migrate_approval_domain()

    # Approval gateway (Phase 1B.2): dialect-agnostic, additive, idempotent.
    _migrate_approval_domain_1b2()

    # MP-4C decision 2 follow-up: duplicate-marker suppression hash.
    _migrate_pending_approvals_content_hash()

    # YouTube Knowledge Ingestion (Phase 1): nullable source attribution.
    _migrate_memories_source_columns()

    # Durable run engine (Phase 1B.2R MP-0): dialect-agnostic, additive,
    # idempotent — 5 new tables only, no ALTER of any existing table.
    _migrate_run_engine()
    _verify_run_engine_schema()

    # MP-5 scheduler-on-engine (first slice): nullable binding columns on
    # scheduled_tasks. Additive, idempotent, dialect-agnostic.
    _migrate_scheduled_tasks_mp5()

    if engine.dialect.name != "sqlite":
        logger.info(
            "Skipping SQLite migrations: engine dialect is '%s' - schema comes "
            "from Base.metadata.create_all() (fresh PostgreSQL install).",
            engine.dialect.name,
        )
        return

    inspector = inspect(engine)
    with engine.connect() as conn:
        # Add columns to documents table if missing
        existing_doc_cols = {c["name"] for c in inspector.get_columns("documents")}
        if "file_size" not in existing_doc_cols:
            conn.execute(sa_text("ALTER TABLE documents ADD COLUMN file_size INTEGER"))
        if "status" not in existing_doc_cols:
            conn.execute(sa_text("ALTER TABLE documents ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'processing'"))
        if "chunk_count" not in existing_doc_cols:
            conn.execute(sa_text("ALTER TABLE documents ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0"))
        if "error_message" not in existing_doc_cols:
            conn.execute(sa_text("ALTER TABLE documents ADD COLUMN error_message TEXT"))

        # Add columns to document_chunks table if missing
        existing_chunk_cols = {c["name"] for c in inspector.get_columns("document_chunks")}
        if "page_number" not in existing_chunk_cols:
            conn.execute(sa_text("ALTER TABLE document_chunks ADD COLUMN page_number INTEGER"))
        if "chroma_id" not in existing_chunk_cols:
            conn.execute(sa_text("ALTER TABLE document_chunks ADD COLUMN chroma_id VARCHAR(100)"))

        # Add column to messages table if missing
        existing_msg_cols = {c["name"] for c in inspector.get_columns("messages")}
        if "citations" not in existing_msg_cols:
            conn.execute(sa_text("ALTER TABLE messages ADD COLUMN citations TEXT"))
        if "image_url" not in existing_msg_cols:
            conn.execute(sa_text("ALTER TABLE messages ADD COLUMN image_url TEXT"))
        # F6 Cap 2: self-evaluation confidence score on assistant messages
        if "confidence" not in existing_msg_cols:
            conn.execute(sa_text("ALTER TABLE messages ADD COLUMN confidence INTEGER"))
        if "confidence_reason" not in existing_msg_cols:
            conn.execute(sa_text("ALTER TABLE messages ADD COLUMN confidence_reason TEXT"))

        # Migrate memories table columns
        existing_mem_cols = {c["name"] for c in inspector.get_columns("memories")}
        if "session_id" not in existing_mem_cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN session_id INTEGER REFERENCES research_sessions(id)"))
        if "category" not in existing_mem_cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN category VARCHAR(30) NOT NULL DEFAULT 'fact'"))
        if "last_used_at" not in existing_mem_cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN last_used_at TIMESTAMP"))
        # Update existing memories' last_used_at
        conn.execute(sa_text("UPDATE memories SET last_used_at = created_at WHERE last_used_at IS NULL"))
        # Phase 2: memory access metrics & decay
        if "last_accessed_at" not in existing_mem_cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN last_accessed_at TIMESTAMP"))
        if "access_count" not in existing_mem_cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0"))
        # Refresh column info after ALTER TABLE ADD COLUMN
        existing_mem_cols = {c["name"] for c in inspector.get_columns("memories")}
        # Migrate old 'type' column → new 'category' column
        if "type" in existing_mem_cols and "category" in existing_mem_cols:
            # Copy existing type values to category where possible
            try:
                conn.execute(sa_text(
                    "UPDATE memories SET category = 'fact' WHERE type IN ('fact', 'note', 'tag')"
                ))
            except Exception:
                pass
            # Drop the old type column (SQLite 3.35.0+ supports this)
            try:
                conn.execute(sa_text("ALTER TABLE memories DROP COLUMN type"))
            except Exception:
                pass  # If drop fails, column remains but won't be used

        # Add Phase 5B columns: model and system_prompt
        existing_sess_cols = {c["name"] for c in inspector.get_columns("research_sessions")}
        if "model" not in existing_sess_cols:
            conn.execute(sa_text("ALTER TABLE research_sessions ADD COLUMN model VARCHAR(100)"))
        if "system_prompt" not in existing_sess_cols:
            conn.execute(sa_text("ALTER TABLE research_sessions ADD COLUMN system_prompt TEXT"))

        # Add Phase 6A column: hashed_password on users
        existing_user_cols = {c["name"] for c in inspector.get_columns("users")}
        if "hashed_password" not in existing_user_cols:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN hashed_password VARCHAR(255)"))

        # Add Phase 7A columns: failed_login_attempts, locked_until
        if "failed_login_attempts" not in existing_user_cols:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0"))
        if "locked_until" not in existing_user_cols:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN locked_until TIMESTAMP"))

        # Phase 7B hardening: last_used_at on refresh_sessions (nullable so
        # existing rows keep working without a backfill default)
        existing_refresh_cols = {c["name"] for c in inspector.get_columns("refresh_sessions")}
        if "last_used_at" not in existing_refresh_cols:
            conn.execute(sa_text("ALTER TABLE refresh_sessions ADD COLUMN last_used_at TIMESTAMP"))
            # Backfill new column with created_at for existing rows
            conn.execute(sa_text(
                "UPDATE refresh_sessions SET last_used_at = created_at "
                "WHERE last_used_at IS NULL"
            ))

        conn.commit()

        # Add scheduled_tasks table if missing
        existing_tables = inspector.get_table_names()
        if "scheduled_tasks" not in existing_tables:
            conn.execute(sa_text("""
                CREATE TABLE scheduled_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    session_id INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    cron_expression VARCHAR(100) NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    last_run_at TIMESTAMP,
                    next_run_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (session_id) REFERENCES research_sessions(id)
                )
            """))
            # Create indexes
            conn.execute(sa_text("CREATE INDEX ix_scheduled_tasks_user_id ON scheduled_tasks (user_id)"))
            conn.execute(sa_text("CREATE INDEX ix_scheduled_tasks_session_id ON scheduled_tasks (session_id)"))

        # Add mcp_servers table if missing
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

        # F6 Cap 3: agent_directives table (self-improving agent lessons)
        existing_tables = inspector.get_table_names()
        if "agent_directives" not in existing_tables:
            conn.execute(sa_text("""
                CREATE TABLE agent_directives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """))
            conn.execute(sa_text("CREATE INDEX ix_agent_directives_user_id ON agent_directives (user_id)"))

        conn.commit()


def _validate_jwt_secret():
    """
    Validate JWT secret at startup.

    In production mode (PRODUCTION_MODE=true), refuse to start with
    the default secret. In development/test mode, allow with a warning.
    """
    default_secret = "change-me-in-production"
    if settings.jwt_secret == default_secret:
        if settings.production_mode:
            logger.critical(
                "Default JWT secret detected in production mode! "
                "Set JWT_SECRET environment variable to a secure random key.\n"
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
            sys.exit(1)
        else:
            logger.warning(
                "Using default JWT secret '%s'. "
                "Set JWT_SECRET environment variable for production.\n"
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"",
                default_secret,
            )
    else:
        logger.info("JWT secret configured (custom value, not default)")


def _warn_ephemeral_sqlite_in_production():
    """Warn loudly (but do not refuse to start) when production mode runs on
    the default ephemeral SQLite database instead of a managed PostgreSQL."""
    if settings.production_mode and settings.database_url.startswith("sqlite"):
        logger.warning(
            "PRODUCTION_MODE is enabled but DATABASE_URL is a SQLite database "
            "(%s) - data will be EPHEMERAL unless the file is on a persistent "
            "volume. Configure DATABASE_URL with a managed PostgreSQL URL "
            "(e.g. Render Postgres / Neon / RDS) before going live.",
            settings.database_url,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and default user on startup."""
    # Ensure the writable data + upload directories exist (portable:
    # <repo>/data in dev, %APPDATA%/ThunderAI when frozen). Fail-closed: a
    # read-only location must warn, never crash startup.
    try:
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not create data/upload directories (%s); continuing",
            type(exc).__name__,
        )

    # Custom skill marketplace — create data/custom_skills/ and load the
    # user-defined Python skills. Fully contained: a broken skill or an
    # unreadable directory only logs a warning, never blocks startup.
    try:
        from app.services import custom_skill_loader

        custom_skill_loader.ensure_custom_skills_dir()
        for entry in custom_skill_loader.scan_and_load_custom_skills():
            if entry["status"] != "loaded":
                logger.warning(
                    "Custom skill '%s' skipped: %s",
                    entry.get("folder"),
                    entry.get("reason"),
                )
    except Exception as exc:  # noqa: BLE001 — custom skills must never crash startup
        logger.warning(
            "Custom skill scan skipped (%s); continuing without custom skills",
            type(exc).__name__,
        )

    _validate_jwt_secret()
    _warn_ephemeral_sqlite_in_production()
    _migrate_knowledge_graph()
    init_db()
    _migrate_database()
    _create_default_user()

    # Phase 1B.2R MP-3A — fail closed at boot if the durable-run public API is
    # enabled without its dedicated HMAC key (env-only, never logged).
    if settings.run_public_api_enabled:
        from app.run_engine.submission import assert_hmac_key_configured
        assert_hmac_key_configured()

    # Start scheduler
    from app.services.scheduler_service import start_scheduler, shutdown_scheduler
    start_scheduler()

    # Phase 1B.2R MP-3C3A — embedded durable RunWorker. Created AFTER every boot
    # check above (migrations + `_verify_run_engine_schema` + HMAC key) has
    # succeeded; never at import. `.start()` is a no-op unless BOTH
    # `run_engine_enabled` and `run_worker_enabled` are true, and fails closed
    # (halting startup) on an unsafe multi-process SQLite topology.
    from app.run_engine.worker_supervisor import RunWorkerSupervisor
    if not getattr(app.state, "run_worker_supervisor", None):
        app.state.run_worker_supervisor = RunWorkerSupervisor()
    await app.state.run_worker_supervisor.start()

    # Phase 1B.2R MP-3C3B — bind the DB-authoritative run-event notifier to this
    # loop (a wake-up hint only; the run_events table stays the source of truth).
    from app.run_engine.notifier import run_notifier
    run_notifier.bind_loop()

    # Mem0 semantic index: warm Qdrant + Ollama embeddings off the event loop so
    # the first memory search stays within its short budget. Optional by design:
    # failure only means retrieval falls back to DB recency.
    try:
        from app.services import mem0_service
        mem0_service.warm_up()
    except Exception as exc:  # noqa: BLE001 - memory is never a boot dependency
        logger.warning("Mem0 warmup skipped (%s)", type(exc).__name__)

    yield

    # Shutdown — unbind the notifier, then cooperative worker stop (bounded
    # grace) BEFORE the scheduler.
    try:
        run_notifier.unbind_loop()
    except Exception:  # noqa: BLE001
        pass
    sup = getattr(app.state, "run_worker_supervisor", None)
    if sup is not None:
        await sup.stop()
        app.state.run_worker_supervisor = None
    shutdown_scheduler()


def _create_default_user():
    """Create a default user if one does not exist."""
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == "default").first()
        if not existing:
            user = User(username="default", email="default@example.com")
            db.add(user)
            db.commit()
            logger.info("Created default user")
        else:
            logger.info("Default user already exists")
    except Exception as exc:  # noqa: BLE001 - startup must not crash here
        db.rollback()
        # Bounded: never echo raw DB error text into console/log output.
        logger.warning("Error creating default user (%s)", type(exc).__name__)
    finally:
        db.close()


app = FastAPI(
    title="Thunder AI",
    version="0.3.0",
    lifespan=lifespan,
)

# ── CORS (Phase 7C / Phase 5 cloud) ──────────────────────
# Allow only the configured frontend origin(s), with credentials. Wildcard
# origins are never used together with allow_credentials=True. The
# X-CSRF-Token header must be allowed for double-submit CSRF.
# CORS_ORIGINS (Phase 5): comma-separated list for production, e.g.
# "https://app.example.com,https://app2.example.com". Falls back to
# FRONTEND_ORIGIN when unset.
_cors_origins = [
    o.strip() for o in settings.cors_origins.split(",") if o.strip()
] or [settings.frontend_origin]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    # Explicit header allow-list: Authorization (bearer), Content-Type, and
    # X-CSRF-Token (double-submit). Avoids relying on a wildcard with
    # credentials, which some stacks reject during preflight.
    # Phase 1B.2R MP-3C3C: Idempotency-Key + Last-Event-ID are the durable
    # /messages/stream request headers — a cross-origin frontend cannot send
    # them unless they are named here. Still an explicit list, not a wildcard.
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-CSRF-Token",
        "Idempotency-Key",
        "Last-Event-ID",
    ],
    # Phase 1B.2R MP-3C3C: expose ONLY the durable run id so a cross-origin
    # frontend can read it from the streaming response. Not a wildcard; no
    # credential/header exposure beyond this one non-secret id.
    expose_headers=["X-Run-Id"],
)

# --- Proxy-aware setup lock + remote API guard ------------------------------
# Tunnel products (Tailscale Serve, Cloudflare Tunnel) connect to the app from
# loopback, so request.client.host == 127.0.0.1 even for a REMOTE caller. Any
# forwarding header therefore marks the request as remote (fail closed).
#
# 1. POST /api/setup/configure: proxied or non-loopback callers get a bounded
#    403. After first-run (configured == true) a valid session is required
#    even from loopback; first-run on pure loopback stays open.
# 2. Remote callers must authenticate for every /api/* route except the small
#    public allow-list (health, setup status, login/register). Refresh/logout
#    carry the HttpOnly session cookie, so the guard validates it. Pure
#    loopback traffic is unchanged, so local-only behaviour is preserved.
@app.middleware("http")
async def _access_guard(request: Request, call_next):
    path = request.url.path or ""
    method = request.method.upper()

    if path == "/api/setup/configure":
        if access_guard.is_forwarded(request) or not access_guard.is_loopback_client(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "first-run setup is only available from localhost"},
            )
        if setup_service.is_configured() and not access_guard.has_valid_session(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "authentication required to reconfigure setup"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    # CORS preflight carries no credentials by design; let CORSMiddleware answer.
    # remote_access_enabled folds in loopback peers too: a tunnel that strips
    # forwarding headers still cannot reach /api anonymously once the operator
    # has opted into remote exposure.
    if (
        method != "OPTIONS"
        and path.startswith("/api/")
        and (settings.remote_access_enabled or access_guard.is_remote_request(request))
        and not access_guard.is_public_api_path(path)
    ):
        if not access_guard.has_valid_session(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "authentication required for remote access"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)

# ── Register routers ─────────────────────────────────────
app.include_router(sessions_router)
app.include_router(messages_router)
app.include_router(documents_router)
app.include_router(memories_router)
app.include_router(settings_router)
app.include_router(models_router)
app.include_router(providers_router)
app.include_router(directives_router)
app.include_router(skills_router)
app.include_router(capabilities_router)  # Executable capability toggles - /api/capabilities
app.include_router(search_router)
app.include_router(auth_router)
app.include_router(scheduler_router)
app.include_router(mcp_router)
app.include_router(mcp_registry_router)  # Official MCP servers - /api/mcp/registry
app.include_router(share_router)
app.include_router(knowledge_graph_router)
app.include_router(export_router)
app.include_router(terminal_router)
app.include_router(media_router)
app.include_router(approvals_router)
app.include_router(runs_router)  # Phase 1B.2R MP-3A — gated by run_public_api_enabled (404 when off)
app.include_router(vision_router)  # Local Vision (ZERO API cost) — POST /api/vision/describe
app.include_router(setup_router)  # First-run setup - GET /api/setup/status, POST /api/setup/configure
app.include_router(oauth_router)  # Subscription sign-in - /api/oauth/{provider}/start|status|refresh|account
app.include_router(desktop_router)  # Desktop automation - /api/desktop/* (503 unless DESKTOP_AUTOMATION_ENABLED)
app.include_router(openbot_router)  # OpenBot autonomous research queue - /api/agents/openbot/*
app.include_router(browser_router)  # Built-in browser - GET /api/browser/embeddable
app.include_router(custom_skills_router)  # Custom skill marketplace - POST /api/skills/custom/{id}
app.include_router(open_interpreter_router)  # Open Interpreter - /api/interpreter/* (403 until enabled, 503 unless installed)


@app.get("/health")
async def health():
    """Fast, dependency-free liveness probe (Render health check).

    Deliberately does NOT touch the database, ChromaDB or Ollama: a managed
    dependency outage must not fail the platform health check. The richer
    /api/health probe below reports those dependencies.
    """
    return {"status": "healthy", "service": "thunder-ai-backend"}


@app.get("/api/health")
async def health_check():
    """Check whether Backend, Database, ChromaDB, and Ollama are reachable."""
    backend_status = "ok"
    chromadb_status = "unavailable"
    ollama_status = "unavailable"
    # Phase 5 (cloud): report database dialect + connectivity. A managed
    # Postgres outage shows up as "unavailable" without crashing the probe.
    database_status = "unknown"
    database_dialect = engine.dialect.name

    try:
        from sqlalchemy import text as _sa_text

        with engine.connect() as conn:
            conn.execute(_sa_text("SELECT 1"))
        database_status = "connected"
    except Exception as exc:
        database_status = "unavailable"
        logger.warning("Database health probe failed: %s", exc)

    # Check ChromaDB ──────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.chromadb_url}/api/v2/heartbeat")
            if resp.status_code == 200:
                chromadb_status = "ok"
    except Exception:
        pass

    # Check Ollama ────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ollama_url}/")
            if resp.status_code == 200 and "Ollama" in resp.text:
                ollama_status = "ok"
    except Exception:
        pass

    return {
        "backend": backend_status,
        "database": database_status,
        "database_dialect": database_dialect,
        "chromadb": chromadb_status,
        "ollama": ollama_status,
    }


@app.get("/api/health/worker")
async def worker_health(response: Response):
    """Phase 1B.2R MP-3C3A — non-secret embedded durable-RunWorker status.

    Legacy chat health (`/api/health`) is untouched. Fields here NEVER contain
    prompts, tool names/args/results, provider errors, API keys, user/session/run
    ids, or database URLs. When the public durable Run API is enabled but its
    worker is not alive (or is in a fatal state) this returns HTTP 503 so a
    readiness probe cannot conclude the durable execution path is healthy.
    """
    sup = getattr(app.state, "run_worker_supervisor", None)
    if sup is None:
        status = {
            "enabled": bool(settings.run_engine_enabled and settings.run_worker_enabled),
            "started": False, "alive": False, "stopping": False,
            "last_successful_poll_at": None, "last_bounded_error_code": None,
            "consecutive_failure_count": 0,
        }
        ready = not settings.run_public_api_enabled
    else:
        status = sup.status()
        ready = sup.is_durable_path_ready()
    if not ready:
        response.status_code = 503
    return {"durable_run": status, "durable_path_ready": ready}


# --- Serve the built frontend (Phase 1 - .exe packaging) -------------------
# Mounted LAST: after every /api, /health and docs route, so API endpoints
# always win (Starlette matches routes in registration order). When
# frontend/dist is absent this logs a bounded warning and the backend runs
# headless, serving the API only.
mount_frontend(app)
