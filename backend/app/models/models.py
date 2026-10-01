import datetime
import enum

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    DateTime,
    ForeignKey,
    Float,
    Boolean,
    Index,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import relationship

from app.database import Base


def _utcnow_aware() -> datetime.datetime:
    """Timezone-aware UTC now. Used by the approval-domain tables (Phase 1B.1),
    which deliberately use tz-aware timestamps; the older tables keep the
    repo's historical naive ``datetime.datetime.utcnow``."""
    return datetime.datetime.now(datetime.timezone.utc)


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class MemoryCategory(str, enum.Enum):
    fact = "fact"
    preference = "preference"
    research_interest = "research_interest"
    project_context = "project_context"


class AppSetting(Base):
    """Persistent key-value settings stored in SQLite."""

    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(String(500), nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<AppSetting(key='{self.key}', value='{self.value}')>"


class UserSetting(Base):
    """Per-user LLM provider settings (provider, API key, model).

    Overrides the global .env configuration when set, so each user can
    pick their own provider/model without restarting Docker.
    """

    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    llm_provider = Column(String(50), nullable=True)  # ollama | openrouter | nvidia
    api_key = Column(String(500), nullable=True)
    model = Column(String(255), nullable=True)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    def __repr__(self):
        return f"<UserSetting(user_id={self.user_id}, provider='{self.llm_provider}')>"


class DocumentStatus(str, enum.Enum):
    processing = "processing"
    ready = "ready"
    failed = "failed"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=True)  # NULL for legacy default user
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    # Phase 7A: account lockout tracking
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)

    sessions = relationship("ResearchSession", back_populates="user", cascade="all, delete-orphan")
    memories = relationship("Memory", back_populates="user", cascade="all, delete-orphan")
    providers = relationship("UserProvider", back_populates="user", cascade="all, delete-orphan")
    scheduled_tasks = relationship("ScheduledTask", back_populates="user", cascade="all, delete-orphan")
    skills = relationship("UserSkill", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}')>"


class ResearchSession(Base):
    __tablename__ = "research_sessions"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False, default="New Research Session")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    model = Column(String(100), nullable=True)  # NULL = use config default
    system_prompt = Column(Text, nullable=True)  # NULL = use default prompt
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    user = relationship("User", back_populates="sessions")
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="session", cascade="all, delete-orphan")
    scheduled_tasks = relationship("ScheduledTask", back_populates="session", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ResearchSession(id={self.id}, title='{self.title}')>"


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("research_sessions.id"), nullable=False)
    role = Column(SAEnum(MessageRole), nullable=False)
    content = Column(Text, nullable=False)
    image_url = Column(Text, nullable=True)  # Base64-encoded image data URL for multimodal messages
    citations = Column(Text, nullable=True)  # JSON-serialized list of citation dicts
    confidence = Column(Integer, nullable=True)  # F6 Cap 2: 0-100 advisory score (None = n/a)
    confidence_reason = Column(Text, nullable=True)  # F6 Cap 2: one-line why
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    session = relationship("ResearchSession", back_populates="messages")

    def __repr__(self):
        return f"<Message(id={self.id}, role='{self.role}')>"


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("research_sessions.id"), nullable=False)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=True)
    file_path = Column(String(500), nullable=False)
    file_size = Column(Integer, nullable=True)  # bytes
    status = Column(String(20), nullable=False, default=DocumentStatus.processing.value)
    chunk_count = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    session = relationship("ResearchSession", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Document(id={self.id}, filename='{self.filename}', status='{self.status}')>"


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    page_number = Column(Integer, nullable=True)
    embedding = Column(Text, nullable=True)  # JSON-serialized vector placeholder
    chroma_id = Column(String(100), nullable=True, index=True)  # ID in ChromaDB collection
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    document = relationship("Document", back_populates="chunks")

    def __repr__(self):
        return f"<DocumentChunk(id={self.id}, index={self.chunk_index})>"


class RefreshSession(Base):
    """
    Phase 7B — Refresh token session tracking.

    Stores a SHA-256 hash of each active refresh token, associated with
    a user and a token family. Token families enable reuse detection:
    if a rotated/revoked token is reused, the entire family is revoked
    (indicating token theft).
    """

    __tablename__ = "refresh_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Family identifier: all tokens from the same original session share this
    family_id = Column(String(36), nullable=False, index=True)
    # SHA-256 hash of the refresh token (never store raw tokens)
    token_hash = Column(String(64), unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)  # NULL = active
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    # Last time this session was used (created or refreshed).
    # Nullable so existing databases migrate safely; NULL rows keep working.
    last_used_at = Column(DateTime, nullable=True)
    device_info = Column(String(255), nullable=True)  # Optional client metadata

    def __repr__(self):
        return f"<RefreshSession(id={self.id}, user_id={self.user_id}, revoked={'Y' if self.revoked_at else 'N'})>"


class Memory(Base):
    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("research_sessions.id"), nullable=True, index=True)
    content = Column(Text, nullable=False)
    category = Column(String(30), nullable=False, default=MemoryCategory.fact.value)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    # Phase 2 (memory decay / access metrics)
    last_accessed_at = Column(DateTime, nullable=True)
    access_count = Column(Integer, nullable=False, default=0)
    # ── Source attribution (YouTube Knowledge Ingestion, Phase 1) ──
    # ADDITIVE and NULLABLE: every pre-existing memory row keeps NULL here and
    # behaves exactly as before. `source_kind` is a short bounded tag
    # ("youtube_knowledge" for this phase); NULL means "typed/extracted
    # directly, not derived from an external source".
    source_url = Column(Text, nullable=True, index=True)
    source_title = Column(String(500), nullable=True)
    source_kind = Column(String(50), nullable=True, index=True)
    # Extra bounded, non-secret provenance (channel / language / fetched_at),
    # serialized as canonical JSON. NEVER raw transcript content.
    source_meta = Column(Text, nullable=True)
    # Stable fact-level identity: sha256 hex DIGEST over the normalized
    # (kind, content) via app.run_engine.save_markers.save_content_hash — the
    # ONE shared digest used for duplicate suppression. NULLABLE and additive:
    # pre-existing rows keep NULL and are matched by their normalized content
    # instead, so nothing needs backfilling. A DIGEST is stored, never content.
    content_hash = Column(String(80), nullable=True, index=True)

    user = relationship("User", back_populates="memories")

    def __repr__(self):
        return f"<Memory(id={self.id}, category='{self.category}')>"


class UserProvider(Base):
    """A saved LLM provider configuration for a user (multi-provider manager)."""

    __tablename__ = "user_providers"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_name = Column(String(50), nullable=False)
    # Fernet-encrypted API key (canonical storage). Never returned in API
    # responses — routes expose a masked value (e.g. sk-****1234) instead.
    encrypted_api_key = Column(Text, nullable=True, default="")
    # Deprecated legacy column: kept only so pre-encryption rows still read.
    # New writes always go to encrypted_api_key and blank this one.
    api_key = Column(String(500), nullable=False, default="")
    default_model = Column(String(255), nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    user = relationship("User", back_populates="providers")

    def __repr__(self):
        return f"<UserProvider(id={self.id}, provider='{self.provider_name}', active={self.is_active})>"


class ScheduledTask(Base):
    """A scheduled autonomous task that runs on a cron schedule."""

    __tablename__ = "scheduled_tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("research_sessions.id"), nullable=False, index=True)
    prompt = Column(Text, nullable=False)
    cron_expression = Column(String(100), nullable=False)  # e.g., "0 8 * * *" for 8 AM daily
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    # MP-5 scheduler-on-engine: the refresh-session family (and tenant) that
    # CREATED the task, captured while the owner was authenticated. A
    # scheduler-driven durable Run must be bound to a LIVE family exactly like
    # an HTTP submission (`validate_session_binding`), and the worker later
    # re-validates the run own binding from the run row. NULL means the task
    # predates this slice or was created by a token without a `sid` claim -
    # the engine path then fails closed and never submits.
    auth_session_ref = Column(String(64), nullable=True)
    tenant_id = Column(String(64), nullable=True)
    # PERSISTED fire identity for the fire currently in flight. Set (and
    # committed) BEFORE the Run is submitted, cleared when the fire finishes.
    # A retry of that fire - including after a restart has advanced
    # next_run_at - must reuse this identity rather than mint a new one, so one
    # scheduled fire can never produce two Runs.
    pending_fire_key = Column(String(80), nullable=True)

    user = relationship("User")
    session = relationship("ResearchSession")

    def __repr__(self):
        return f"<ScheduledTask(id={self.id}, prompt='{self.prompt[:30]}...', cron='{self.cron_expression}', active={self.is_active})>"


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


class SharedAgent(Base):
    """A minimal, immutable snapshot of an exported agent for public sharing.

    Created when a user clicks "Share" on a session. The fields are copied at
    share time so that deleting/renaming the source session never breaks an
    already-published link (see frontend privacy contract: only snapshot data,
    never the session's private content).

    Table is auto-created by ``Base.metadata.create_all`` on startup.
    """

    __tablename__ = "shared_agents"

    id = Column(Integer, primary_key=True, index=True)
    share_id = Column(String(12), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("research_sessions.id"), nullable=False)
    title = Column(String(255), nullable=False)
    model = Column(String(100), nullable=True)
    system_prompt = Column(Text, nullable=True)
    preview_message = Column(Text, nullable=True)
    tool_count = Column(Integer, nullable=False, default=0)
    has_schedule = Column(Boolean, nullable=False, default=False)
    cover_image_url = Column(Text, nullable=True)
    view_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SharedAgent(share_id='{self.share_id}', title='{self.title}')>"


class AgentDirective(Base):
    """A persistent behavioural directive learned by the agent (F6 Cap 3).

    The agent self-improves by saving durable "lessons learned" — standing
    instructions it discovers from its own reflection (e.g. "Always cite
    sources when answering from documents", "Prefer APA over MLA citations").
    Active directives are injected into every future system prompt so the
    agent behaves better over time without code changes.

    Table is auto-created by ``Base.metadata.create_all`` on startup.
    """

    __tablename__ = "agent_directives"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<AgentDirective(id={self.id}, user_id={self.user_id}, is_active={self.is_active})>"


class UserSkill(Base):
    """A user-defined skill stored in the database (custom skill creator).

    Mirrors the filesystem ``Skill`` shape (name/description/body) so DB
    skills can be merged into the same L1 index and L2 on-demand loading
    path as bundled/filesystem skills. Filesystem skills take precedence
    on name collisions (enforced at create time and at lookup time).
    """

    __tablename__ = "user_skills"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_skills_user_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Marker-safe name: ^[a-z0-9][a-z0-9-_]{1,48}$ (lowercase, no spaces)
    name = Column(String(50), nullable=False, index=True)
    description = Column(String(200), nullable=False, default="")
    # L2 markdown body, loaded on demand when the model emits the skill marker
    body = Column(Text, nullable=False, default="")
    # Comma-separated lowercase keywords (sanitized, max 10, each <= 32 chars)
    trigger_keywords = Column(String(400), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    user = relationship("User", back_populates="skills")

    def __repr__(self):
        return f"<UserSkill(id={self.id}, user_id={self.user_id}, name='{self.name}', enabled={self.enabled})>"


class GraphEntity(Base):
    """A node in the Knowledge Graph — a concise entity extracted from text.

    User-scoped: every entity belongs to the user whose document / chat /
    research run it was extracted from. Names are unique per user, not
    globally (two users can each have their own "Python").
    """

    __tablename__ = "graph_entities"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_graph_entities_user_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False, index=True)
    type = Column(String(60), nullable=False, default="concept")
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    def __repr__(self):
        return (
            f"<GraphEntity(id={self.id}, user_id={self.user_id}, "
            f"name='{self.name}', type='{self.type}')>"
        )


class GraphRelation(Base):
    """A directed edge between two GraphEntity rows (same user).

    ``weight`` is incremented each time the same (source, target, relation)
    triple is seen again, so repeated mentions strengthen the edge.
    """

    __tablename__ = "graph_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "target_id", "relation", name="uq_graph_relations_triple"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    source_id = Column(
        Integer, ForeignKey("graph_entities.id"), nullable=False, index=True
    )
    target_id = Column(
        Integer, ForeignKey("graph_entities.id"), nullable=False, index=True
    )
    relation = Column(String(160), nullable=False)
    weight = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    def __repr__(self):
        return (
            f"<GraphRelation({self.source_id} -[{self.relation}]-> "
            f"{self.target_id}, w={self.weight})>"
        )


class MediaVideoTask(Base):
    """Ownership record for an async video-generation task.

    Video providers issue an opaque ``task_id``; the media itself is not
    persisted. This row is what authorises polling ``GET /video/{task_id}`` —
    a task may only be polled by the user who created it. Rows are short-lived
    and pruned opportunistically once older than ~2x the video timeout.
    """

    __tablename__ = "media_video_tasks"

    task_id = Column(String(200), primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(40), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    def __repr__(self):
        return (
            f"<MediaVideoTask(task_id='{self.task_id}', user_id={self.user_id}, "
            f"provider='{self.provider}')>"
        )


# ── Approval domain (Phase 1B.1) ───────────────────────────────────────
#
# A persistent, tamper-evident, hash-bound approval layer. The existing
# terminal ([PROPOSED_COMMAND]) and browser ([BROWSER_APPROVAL_REQUIRED])
# approvals are transient LangGraph state; these tables are what a later phase
# (1B.2) will bind them to. Nothing here is wired into a call site yet.
#
# Conventions that differ from the older tables, on purpose:
#   * timezone-aware timestamps (DateTime(timezone=True) + _utcnow_aware);
#   * string UUID primary keys (opaque external ids, like MediaVideoTask);
#   * `tenant_id` is nullable with NO foreign key — there is no tenants table
#     in the repo yet; the column is here so the domain is multi-tenant-ready;
#   * `user_id` is a plain Integer with NO foreign key so the domain can be
#     unit-tested on a schema that does not include `users`; ownership is
#     enforced in code on every read / consume. A later additive migration can
#     add the FK once the call sites are wired.
#   * state / event-type columns are plain String (+ code-side constants),
#     matching Document.status / Memory.category rather than SAEnum.


class PendingApproval(Base):
    """One requested action awaiting (or carrying) a human decision.

    Stores ONLY what authorization and safe display need: the two hashes,
    identity/scope columns, a server-generated nonce, and a bounded,
    recursively-redacted display summary. It never stores raw arguments, API
    keys, passwords, auth headers, cookies, or secret env values — approval
    storage is not a secret vault.
    """

    __tablename__ = "pending_approvals"

    id = Column(String(36), primary_key=True)  # uuid4 hex-with-dashes
    # binding
    action_hash = Column(String(64), nullable=False, index=True)
    args_hash = Column(String(64), nullable=False)
    action_envelope_version = Column(String(8), nullable=False, default="1")
    # identity / scope
    tool_id = Column(String(200), nullable=False)
    server_fingerprint = Column(String(64), nullable=False)
    tool_schema_hash = Column(String(64), nullable=False)
    source_type = Column(String(20), nullable=False, default="unknown")
    tenant_id = Column(String(64), nullable=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(Integer, nullable=True, index=True)
    run_id = Column(String(64), nullable=True)
    action_nonce = Column(String(64), nullable=False)
    policy_version = Column(String(32), nullable=False)
    risk_level = Column(String(16), nullable=False, default="low")
    # state machine: requested | approved | denied | expired | consumed | revoked
    state = Column(String(16), nullable=False, default="requested", index=True)
    # Phase 1B.2 — server-owned proposal identity for idempotent (re)proposal.
    # sha256 over the canonicalized dedupe field set (see app.approval.dedupe).
    # A partial-unique index on (tenant_id, proposal_dedupe_key) among ACTIVE
    # states guarantees one active PendingApproval per logical proposal.
    proposal_dedupe_key = Column(String(64), nullable=True)
    execution_turn_id = Column(String(36), nullable=True)
    tool_call_id = Column(String(36), nullable=True)
    # MP-4C decision 2 follow-up - sha256 over the stably-normalized save args
    # (kind + whitespace-normalized content + category). A DIGEST only: never
    # the content. Used to suppress an unchanged duplicate marker. Nullable for
    # every non-save proposal (and every pre-existing row).
    content_hash = Column(String(80), nullable=True)
    # Canonical action-hash fields as JSON (no secrets, no `purpose`), so the
    # trusted server path can reconstruct + re-hash the EXACT approved envelope
    # with its ORIGINAL action_nonce at consume / grant time (1B.2). Nullable —
    # Phase 1B.1 rows predate it.
    trusted_envelope_json = Column(Text, nullable=True)
    # bounded, recursively-redacted; safe to show a user
    display_summary = Column(Text, nullable=False, default="{}")
    purpose_redacted = Column(String(512), nullable=False, default="")
    # timestamps (tz-aware UTC; server decides expiry — never the client)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("action_nonce", name="uq_pending_approvals_nonce"),
        Index("ix_pending_approvals_user_state", "user_id", "state"),
        Index("ix_pending_approvals_tenant_state", "tenant_id", "state"),
        # Active-proposal uniqueness — see _migrate_approval_domain_1b2 for the
        # partial UNIQUE index on COALESCE(tenant_id,'') + proposal_dedupe_key
        # WHERE state IN ('requested','approved') — SQLite (>=3.8) + PostgreSQL.
        Index("ix_pending_approvals_dedupe", "tenant_id", "proposal_dedupe_key"),
        Index("ix_pending_approvals_turn", "execution_turn_id"),
        Index("ix_pending_approvals_call", "tool_call_id"),
        # Duplicate-marker suppression lookup (MP-4C decision 2 follow-up).
        Index("ix_pending_approvals_content_hash", "content_hash"),
    )

    def __repr__(self):
        return (
            f"<PendingApproval(id={self.id!r}, state={self.state!r}, "
            f"user_id={self.user_id}, tool_id={self.tool_id!r})>"
        )


class SessionGrant(Base):
    """A narrow, revocable, per-session pre-approval.

    NOT an "allow all" — the reusable identity is exactly one ``grant_scope_hash``
    (the STABLE authorization template: tenant, user, session, tool identity +
    version, source type, MCP server, server fingerprint, tool-schema hash,
    args_hash, affected resources, network/fs/credential scope, server-derived
    risk, policy version). It has NO per-attempt nonce or run_id, so repeated
    equivalent actions in the same session match while every stable-field change
    invalidates it. Created only by an explicit session-scoped user decision;
    expires no later than the user session; revocable. Has its own CSPRNG nonce.
    """

    __tablename__ = "session_grants"

    id = Column(String(36), primary_key=True)
    grant_nonce = Column(String(64), nullable=False)          # server CSPRNG
    grant_scope_version = Column(String(8), nullable=False, default="1")
    tenant_id = Column(String(64), nullable=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(Integer, nullable=False, index=True)
    tool_id = Column(String(200), nullable=False)
    server_fingerprint = Column(String(64), nullable=False)
    tool_schema_hash = Column(String(64), nullable=False)
    grant_scope_hash = Column(String(64), nullable=False)     # the reusable identity
    policy_version = Column(String(32), nullable=False)
    creating_approval_id = Column(String(36), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("grant_nonce", name="uq_session_grants_nonce"),
        Index("ix_session_grants_lookup", "user_id", "session_id", "tool_id"),
        Index("ix_session_grants_scope", "grant_scope_hash"),
    )

    def __repr__(self):
        return (
            f"<SessionGrant(id={self.id!r}, user_id={self.user_id}, "
            f"session_id={self.session_id}, tool_id={self.tool_id!r}, "
            f"revoked={'Y' if self.revoked_at else 'N'})>"
        )


class ActionLogEntry(Base):
    """One link of a tamper-EVIDENT hash chain.

    Detects accidental or unsophisticated modification / deletion / reordering /
    duplication / mis-linking (verifier + ``UNIQUE(chain_scope_id, seq)`` +
    ``UNIQUE(row_hash)``). It is **not** tamper-proof and **not** immutable
    against a privileged DBA who can rewrite every row and recompute the whole
    chain — that actor is outside the current protection boundary. A future
    phase may add an external signed/HMAC checkpoint anchor (not in 1B.1).

    Application code exposes no update or delete for this table — service code
    only appends.

        row_hash = SHA-256(tag || prev_row_hash || canonical(event_payload))

    ``chain_scope_id`` (``tenant:<id>`` when tenant_id is set, else ``user:<id>``
    or ``system`` — never ambiguous NULL grouping) is part of the hashed
    payload.
    """

    __tablename__ = "action_log"

    id = Column(String(36), primary_key=True)
    chain_scope_id = Column(String(80), nullable=False, index=True)
    seq = Column(BigInteger, nullable=False)
    event_type = Column(String(40), nullable=False)
    approval_id = Column(String(36), nullable=True, index=True)
    grant_id = Column(String(36), nullable=True)
    action_hash = Column(String(64), nullable=True)
    event_metadata = Column(Text, nullable=False, default="{}")  # sanitized JSON
    prev_row_hash = Column(String(64), nullable=False)
    row_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)

    __table_args__ = (
        UniqueConstraint("chain_scope_id", "seq", name="uq_action_log_scope_seq"),
        UniqueConstraint("row_hash", name="uq_action_log_row_hash"),
    )

    def __repr__(self):
        return (
            f"<ActionLogEntry(scope={self.chain_scope_id!r}, seq={self.seq}, "
            f"type={self.event_type!r})>"
        )


class ActionLogHead(Base):
    """One row per chain scope, holding the current head (seq + row_hash).

    Serves as the lock target for appends: PostgreSQL takes
    ``SELECT ... FOR UPDATE`` on this row; SQLite relies on ``BEGIN IMMEDIATE``
    plus the ``UNIQUE(chain_scope_id, seq)`` constraint on ``action_log``. This
    row is advisory — ``action_log`` itself is the source of truth and the
    verifier never trusts this table.
    """

    __tablename__ = "action_log_heads"

    chain_scope_id = Column(String(80), primary_key=True)
    seq = Column(BigInteger, nullable=False, default=0)
    row_hash = Column(String(64), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)

    def __repr__(self):
        return f"<ActionLogHead(scope={self.chain_scope_id!r}, seq={self.seq})>"


# ── Approval gateway / trusted registry (Phase 1B.2) ───────────────────
#
# Same conventions as the Phase 1B.1 tables: tz-aware timestamps, string UUID
# PKs, nullable `tenant_id` with no FK (no tenants table), `user_id` as a plain
# Integer with no FK (code-enforced ownership), plain String state columns.


class TrustedTool(Base):
    """The server's authoritative record of a dispatchable MCP tool.

    A tool is only executable when a row exists here with ``locked = False``.
    Newly discovered tools, and tools whose canonical schema or server
    fingerprint materially changed, are ``locked = True`` until an operator
    (a later phase) re-verifies. Unknown / unregistered tools fail closed.

    "Materially changed" is deterministic: any change to ``tool_schema_hash``
    or ``server_fingerprint`` (both computed by app.approval.hashing over
    server-owned, non-secret inputs). Existing approvals / SessionGrants bind
    these hashes, so a change invalidates every prior authorization.
    """

    __tablename__ = "trusted_tools"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(64), nullable=True, index=True)
    # scoping key: MCP tools are per-user (mcp_servers.user_id); a hosted tenant
    # rollout keys additionally on tenant_id.
    owner_user_id = Column(Integer, nullable=False, index=True)
    tool_id = Column(String(200), nullable=False)        # canonical: mcp__<server>__<tool>
    tool_version = Column(String(64), nullable=False, default="")
    source_type = Column(String(20), nullable=False, default="mcp")
    mcp_server_id = Column(Integer, nullable=True)       # mcp_servers.id
    mcp_server_name = Column(String(64), nullable=True)
    input_schema_json = Column(Text, nullable=False, default="{}")   # canonical, non-secret
    tool_schema_hash = Column(String(64), nullable=False)
    server_fingerprint = Column(String(64), nullable=False)
    config_ref_id = Column(String(120), nullable=True)  # server-owned credential/config reference
    enabled = Column(Boolean, nullable=False, default=True)
    locked = Column(Boolean, nullable=False, default=True)   # fail-closed default
    lock_reason = Column(String(120), nullable=True)
    risk_metadata_json = Column(Text, nullable=False, default="{}")  # capability/risk seam for 1B.3
    policy_version = Column(String(32), nullable=False, default="1b1.1")
    discovered_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "tool_id", name="uq_trusted_tools_owner_tool"),
        Index("ix_trusted_tools_lookup", "owner_user_id", "tool_id", "enabled", "locked"),
        Index("ix_trusted_tools_tenant", "tenant_id"),
    )

    def __repr__(self):
        return (
            f"<TrustedTool(tool_id={self.tool_id!r}, locked={self.locked}, "
            f"enabled={self.enabled}, owner_user_id={self.owner_user_id})>"
        )


class ExecutionTurn(Base):
    """A server-owned generation turn — the parent scope for one or more logical
    tool calls parsed from one assistant response.

    Created and owned by the trusted server path. Its id is NEVER accepted from
    the client or the LLM. In Phase 1B.2 the reference dispatcher opens turns;
    the live `/messages/stream` path is NOT modified to persist turns (durable
    cross-request turn preservation is deferred to the durable-run phase).
    """

    __tablename__ = "execution_turns"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(64), nullable=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(Integer, nullable=True, index=True)
    run_id = Column(String(64), nullable=True)
    # optional link to an assistant message row once one exists (nullable — the
    # streaming path saves the Message only after generation completes).
    assistant_message_id = Column(Integer, nullable=True)
    kind = Column(String(20), nullable=False, default="reference")  # reference | live (future)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)

    __table_args__ = (
        Index("ix_execution_turns_owner", "user_id", "session_id"),
    )

    def __repr__(self):
        return f"<ExecutionTurn(id={self.id!r}, user_id={self.user_id})>"


class ToolCall(Base):
    """A server-owned logical tool call within an ExecutionTurn.

    ``call_index`` is a server-assigned allocation slot within the turn — NOT
    derived from or authorized by an ordinal alone. The identity is this row's
    ``id`` (a server UUID). A slot is reusable only when the stored
    ``(tool_id, args_hash)`` match; a different tool or args on an existing slot
    fails closed so a reordered / regenerated marker list cannot cross-authorize.
    Distinct calls in one turn get distinct ids. A reference-domain retry of the
    SAME persisted logical call reuses this row (and its id).
    """

    __tablename__ = "tool_calls"

    id = Column(String(36), primary_key=True)
    execution_turn_id = Column(String(36), nullable=False, index=True)
    tenant_id = Column(String(64), nullable=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(Integer, nullable=True, index=True)
    call_index = Column(Integer, nullable=False, default=0)   # position in the parsed marker list
    tool_id = Column(String(200), nullable=False)
    args_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)

    __table_args__ = (
        UniqueConstraint("execution_turn_id", "call_index", name="uq_tool_calls_turn_index"),
        Index("ix_tool_calls_owner", "user_id", "session_id"),
    )

    def __repr__(self):
        return (
            f"<ToolCall(id={self.id!r}, turn={self.execution_turn_id!r}, "
            f"idx={self.call_index}, tool_id={self.tool_id!r})>"
        )


class DispatchRecord(Base):
    """At-most-once local dispatch state for one logical ToolCall.

    ``UNIQUE(tool_call_id)`` + a CAS insert guarantees a given persisted logical
    call is dispatched at most once by this server. ``outcome`` is
    ``started`` -> (``succeeded`` | ``failed`` | ``incomplete``). An
    ``incomplete`` row (process crashed after dispatch began, outcome unknown)
    is NOT auto-retried for a side-effecting tool — it awaits reconciliation.

    This is a LOCAL guarantee. It does NOT and cannot claim external
    exactly-once across an MCP server the tool talks to.
    """

    __tablename__ = "dispatch_records"

    id = Column(String(36), primary_key=True)
    tool_call_id = Column(String(36), nullable=False)
    tenant_id = Column(String(64), nullable=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    approval_id = Column(String(36), nullable=True)
    grant_id = Column(String(36), nullable=True)
    action_hash = Column(String(64), nullable=False)
    outcome = Column(String(16), nullable=False, default="started")  # started|succeeded|failed|incomplete
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tool_call_id", name="uq_dispatch_records_tool_call"),
        Index("ix_dispatch_records_owner", "user_id"),
    )

    def __repr__(self):
        return (
            f"<DispatchRecord(tool_call_id={self.tool_call_id!r}, "
            f"outcome={self.outcome!r})>"
        )


# ── Durable run engine (Phase 1B.2R — MP-0 schema foundation) ──────────
#
# The durable outer run ledger. RunEngine is the CANONICAL orchestration
# state; a future LangGraph subflow (run_steps.engine == "langgraph") stays
# SUBORDINATE to this ledger's identity / approval / dispatch / audit state —
# there is never a second authoritative durable store for the same run.
#
# MP-0 is SCHEMA + STATE MACHINE + ENCRYPTED PAYLOAD STORE ONLY. No worker,
# route, streaming adapter, approval resume, or tool invocation is wired here.
#
# Conventions (same as the Phase 1B tables): tz-aware timestamps
# (DateTime(timezone=True) + _utcnow_aware); String(36) UUID PKs; nullable
# `tenant_id` with NO foreign key (no tenants table); `user_id` a plain Integer
# with NO foreign key (code-enforced ownership); plain String status columns
# with code-side constants (app.run_engine.state_machine).
#
# PAYLOAD SECURITY CONTRACT (see docs/superpowers/plans/2026-09-08-...md §"secure
# payload contract"):
#   * runs / run_steps / run_events / run_signals hold ONLY non-secret
#     references + hashes + status. Never raw credentials, tokens, headers,
#     cookies, private keys, or provider configs. Never unrestricted tool output.
#   * The ONLY sanctioned home for run-scoped bytes that could be sensitive AND
#     are not already in a `messages` row is `run_payloads`, encrypted at rest
#     with the repo's Fernet service (AES-128-CBC + HMAC-SHA256). Nothing is
#     ever written there in plaintext; seal fails closed if ENCRYPTION_KEY is
#     unset.
#   * Provider / MCP credentials stay where they already live
#     (`user_providers.encrypted_api_key`, `mcp_servers.env_json`) and are
#     referenced by a server-owned id, never copied into run state.


class Run(Base):
    """One durable unit of work (a chat turn, a scheduled task, a sub-run).

    Owns identity (tenant/user/session/thread/run), lifecycle status, the
    client idempotency binding, the approval it waits on, worker-lease seams,
    and event/reconciliation bookkeeping. NO run state lives only in memory —
    this row is authoritative.
    """

    __tablename__ = "runs"

    # Per-column indexes are declared explicitly in __table_args__ (composites)
    # so the ORM's create_all() and the raw _migrate_run_engine() DDL produce
    # exactly the same index set.
    id = Column(String(36), primary_key=True)                 # opaque public run_id (uuid4)
    tenant_id = Column(String(64), nullable=True)
    user_id = Column(Integer, nullable=False)                 # ownership root (get_current_user)
    session_id = Column(Integer, nullable=True)               # research_sessions.id (no FK, 1B convention)
    thread_id = Column(String(64), nullable=False)            # server-derived "run:<id>"; the LangGraph seam
    kind = Column(String(24), nullable=False, default="chat_stream")  # chat_stream|chat_sync|scheduled|sub_run
    parent_run_id = Column(String(36), nullable=True)         # set for kind == sub_run
    # lifecycle — see app.run_engine.state_machine.STATES
    status = Column(String(24), nullable=False, default="created")
    status_reason = Column(String(64), nullable=True)         # e.g. denied, approval_expired, policy_changed
    # request idempotency (D3) — the key is NOT an authorization credential.
    # Uniqueness is scoped by (tenant_id, user_id, session_id, idempotency_key)
    # via a partial-UNIQUE index created in _migrate_run_engine (see there).
    idempotency_key = Column(String(80), nullable=True)
    # canonical, non-secret hash of the NORMALIZED request identity for this run
    # (never a hash of the raw prompt alone). Same key + same identity ⇒ reuse
    # this run; same key + different identity ⇒ deterministic conflict.
    idempotency_identity_hash = Column(String(64), nullable=True)
    # session-revocation binding (D5): a `refresh_sessions.family_id` — NOT a
    # token, NOT a token hash. NULL == unbound. RESERVED: populated once the
    # access token carries the family claim (MP-1 auth seam). A future resume /
    # dispatch MUST fail closed while this is NULL or its RefreshSession is not
    # active. MP-0 stores the column + the fail-closed predicate + tests only.
    auth_session_ref = Column(String(64), nullable=True)
    # optimistic-concurrency guard for the future worker (bump on every write)
    version = Column(Integer, nullable=False, default=0)
    # worker-lease seams (NO worker in MP-0)
    lease_owner = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    run_deadline_at = Column(DateTime(timezone=True), nullable=True)
    # The approval this run was PARKED on (pending_approvals.id). Written once by
    # the worker proposal pass and deliberately NEVER cleared: it is the run BOUND
    # APPROVAL IDENTITY, consumed by the decision-signal mirror
    # (mcp_flow._MIRROR_STATES), the worker approval-identity guard, executor
    # grant derivation and messages_stream._approval_view. Every one of those is
    # gated by the run status and/or the approval state machine, so the pointer can
    # never authorize anything on a terminal run. Do NOT read "is not None" as
    # "is pending", and do NOT clear it because the name merely looks stale.
    pending_approval_id = Column(String(36), nullable=True)
    # high-water mark for run_events.run_seq
    last_event_seq = Column(Integer, nullable=False, default=0)
    # outcome_unknown reconciliation (explicit future operation only)
    reconciled_by = Column(String(64), nullable=True)
    reconciled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    updated_at = Column(
        DateTime(timezone=True), nullable=False,
        default=_utcnow_aware, onupdate=_utcnow_aware,
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_runs_status_lease", "status", "lease_expires_at"),
        Index("ix_runs_owner_created", "user_id", "session_id", "created_at"),
        Index("ix_runs_pending_approval", "pending_approval_id"),
        Index("ix_runs_parent", "parent_run_id"),
        Index("ix_runs_tenant_status", "tenant_id", "status"),
        # scoped idempotency uniqueness is a PARTIAL unique index created in
        # _migrate_run_engine (COALESCE over tenant_id/session_id + WHERE
        # idempotency_key IS NOT NULL) — SQLite (>= 3.8) + PostgreSQL.
        Index("ix_runs_idem", "tenant_id", "user_id", "session_id", "idempotency_key"),
    )

    def __repr__(self):
        return (
            f"<Run(id={self.id!r}, status={self.status!r}, kind={self.kind!r}, "
            f"user_id={self.user_id})>"
        )


class RunStep(Base):
    """One step of a run's plan. ``kind`` names the work; ``engine`` says who
    runs it (``native`` now; ``langgraph`` reserved for a subordinate subflow).
    Side-effecting steps (``tool_dispatch``) defer their at-most-once guarantee
    to the Phase 1B.2 ``dispatch_records`` ledger — this row never re-implements
    it.
    """

    __tablename__ = "run_steps"

    id = Column(String(36), primary_key=True)                 # step_id (uuid4)
    run_id = Column(String(36), nullable=False)               # UNIQUE(run_id, seq) covers run_id as a prefix
    tenant_id = Column(String(64), nullable=True)
    user_id = Column(Integer, nullable=False)                 # denormalized for owner-scoped queries
    session_id = Column(Integer, nullable=True)
    seq = Column(Integer, nullable=False)                     # 1-based position within the run
    kind = Column(String(32), nullable=False)                 # load_context|llm_generate|parse_tools|tool_dispatch|sub_run|finalize
    engine = Column(String(16), nullable=False, default="native")   # native | langgraph
    status = Column(String(16), nullable=False, default="pending")  # pending|running|succeeded|failed|blocked|waiting|skipped|incomplete
    # links into the Phase 1B.2 identity / approval spine (no FK, 1B convention)
    execution_turn_id = Column(String(36), nullable=True)
    tool_call_id = Column(String(36), nullable=True)
    approval_id = Column(String(36), nullable=True)
    attempt = Column(Integer, nullable=False, default=0)      # retry counter (side-effect-free steps only)
    # NON-SECRET reference JSON only (e.g. {message_id, marker_index, tool_id, args_sha256}).
    input_ref = Column(Text, nullable=True)
    output_ref = Column(Text, nullable=True)                  # {output_sha256, output_len, message_id}
    # optional pointers to encrypted run_payloads rows when a step genuinely
    # needs to persist sensitive bytes not already in a `messages` row
    input_payload_id = Column(String(36), nullable=True)
    output_payload_id = Column(String(36), nullable=True)
    error_code = Column(String(48), nullable=True)            # sanitized; from gateway.BLOCK_REASONS where relevant
    version = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_steps_run_seq"),
        Index("ix_run_steps_status_lease", "status", "lease_expires_at"),
        Index("ix_run_steps_tool_call", "tool_call_id"),
        Index("ix_run_steps_owner", "user_id", "session_id"),
    )

    def __repr__(self):
        return (
            f"<RunStep(id={self.id!r}, run={self.run_id!r}, seq={self.seq}, "
            f"kind={self.kind!r}, status={self.status!r})>"
        )


class RunEvent(Base):
    """One persisted, replayable client-visible event. ``run_seq`` is the
    per-run monotonic id the client uses as a Last-Event-ID equivalent.
    ``category`` splits ``control`` (state / approval / lifecycle / error —
    written immediately) from ``text`` (token deltas — batched by a future
    writer per D7). ``data`` is sanitized JSON: token text and status only,
    NEVER raw tool output (hash + length), args, headers, or secrets.
    """

    __tablename__ = "run_events"

    id = Column(String(36), primary_key=True)                 # uuid4 (dialect-safe; ordering is (run_id, run_seq))
    run_id = Column(String(36), nullable=False)
    run_seq = Column(Integer, nullable=False)                 # per-run monotonic (assigned under the run lease, later)
    tenant_id = Column(String(64), nullable=True)             # denormalized for owner-scoped replay
    user_id = Column(Integer, nullable=False)
    category = Column(String(16), nullable=False, default="control")  # control | text
    type = Column(String(32), nullable=False)                 # status|token|plan|agent_status|tool_approval_required|
                                                              # tool_execution_started|_succeeded|_failed|_blocked|
                                                              # run_completed|run_failed|run_cancelled|error
    data = Column(Text, nullable=False, default="{}")         # sanitized JSON
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)

    __table_args__ = (
        UniqueConstraint("run_id", "run_seq", name="uq_run_events_run_seq"),
        Index("ix_run_events_run", "run_id", "created_at"),
        Index("ix_run_events_owner", "user_id"),
    )

    def __repr__(self):
        return (
            f"<RunEvent(run={self.run_id!r}, seq={self.run_seq}, "
            f"type={self.type!r}, category={self.category!r})>"
        )


class RunSignal(Base):
    """An out-of-band instruction from a request thread to the (future) worker:
    an approval decision or a cancel request. A table (not a column) so the
    producer and consumer are decoupled, every signal is audited, and duplicate
    approve / duplicate cancel are naturally idempotent via a partial-UNIQUE
    index (one live signal of a kind per run). ``payload`` carries the decision
    only — NEVER a hash, envelope, or trusted field.
    """

    __tablename__ = "run_signals"

    id = Column(String(36), primary_key=True)
    run_id = Column(String(36), nullable=False)               # ix_run_signals_run_kind covers run_id as a prefix
    kind = Column(String(24), nullable=False)                 # approval_decided | cancel_requested
    # ALLOWLISTED schema ONLY — build/parse via app.run_engine.signals. Holds
    # {approval_id?, decision?, reason_code?, server_ts?}. NEVER a raw prompt,
    # raw tool args, credential, token, cookie, header, unrestricted error text,
    # or unrestricted client JSON.
    payload = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=False)      # MUST equal runs.user_id (re-checked by the worker, later)
    created_by_tenant_id = Column(String(64), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)

    __table_args__ = (
        # one LIVE (unconsumed) signal of a kind per run — a partial-UNIQUE
        # index is created in _migrate_run_engine (WHERE consumed_at IS NULL).
        Index("ix_run_signals_run_kind", "run_id", "kind"),
    )

    def __repr__(self):
        return (
            f"<RunSignal(run={self.run_id!r}, kind={self.kind!r}, "
            f"consumed={'Y' if self.consumed_at else 'N'})>"
        )


class RunPayload(Base):
    """Encrypted-at-rest store for run-scoped bytes that (a) are not already in
    a `messages` row and (b) could contain sensitive user data (resolved tool
    arguments, a tool result a later step needs, a sub-run input).

    Sealed with a **versioned, context-bound encrypted envelope** (see
    ``app.run_engine.payloads``): a canonical JSON object carrying
    ``version, tenant_id, user_id, run_id, step_id, payload_type, action_hash,
    payload_encoding, payload`` is encrypted **once** with the repo's Fernet
    cipher (AES-128-CBC + HMAC-SHA256 — authenticated). Every context field is
    therefore inside the authenticated ciphertext; a ciphertext swapped between
    rows fails closed on decrypt. ``open_payload`` re-checks each envelope field
    against TRUSTED server-supplied expected values.

    **There is NO adjacent plaintext digest.** An unkeyed ``sha256(plaintext)``
    stored next to the ciphertext would let an attacker offline-guess a
    low-entropy payload; it is unnecessary given Fernet's HMAC. (A local dev DB
    from the earlier MP-0 iteration may still carry an inert ``plaintext_sha256``
    column — the clean schema neither creates nor requires it; MP-0 does not
    carry destructive migration logic to drop it.)

    NEVER holds a credential (those stay in `user_providers` / `mcp_servers`).
    NEVER written in plaintext. Seal fails closed when ENCRYPTION_KEY is unset.
    ``byte_len`` is OUTSIDE the ciphertext — it is the payload's UTF-8 length,
    used only for the pre-encryption size cap + operational reporting, and it
    **intentionally reveals payload length** (not confidential). Hard delete
    (drop the row) is the real delete — crypto-shredding.
    """

    __tablename__ = "run_payloads"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(64), nullable=True)
    user_id = Column(Integer, nullable=False)
    run_id = Column(String(36), nullable=False)
    step_id = Column(String(36), nullable=True)
    payload_type = Column(String(32), nullable=False)         # tool_args|tool_output|sub_run_input|other
    # enc_scheme identifies the CIPHER SCHEME ("fernet.v1" == a single
    # cryptography.fernet.Fernet). It is NOT a per-row key-version: the repo's
    # only rotation mechanism is MultiFernet in app.services.encryption
    # (re-encrypt under a new primary key, decrypt with the old key as a
    # fallback). This column is a forward-compat seam, not a key-version claim.
    enc_scheme = Column(String(24), nullable=False, default="fernet.v1")
    ciphertext = Column(Text, nullable=False)                 # Fernet token (base64url) over the canonical envelope
    envelope_action_hash = Column(String(64), nullable=True)  # operational mirror of the envelope's action_hash
    byte_len = Column(Integer, nullable=False)                # payload UTF-8 length (size cap only; NOT confidential)
    expires_at = Column(DateTime(timezone=True), nullable=True)   # retention (future sweeper)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)

    __table_args__ = (
        Index("ix_run_payloads_run", "run_id"),
        Index("ix_run_payloads_step", "step_id"),
        Index("ix_run_payloads_owner", "user_id"),
        Index("ix_run_payloads_expiry", "expires_at"),
    )

    def __repr__(self):
        return (
            f"<RunPayload(id={self.id!r}, run={self.run_id!r}, "
            f"type={self.payload_type!r}, bytes={self.byte_len})>"
        )
