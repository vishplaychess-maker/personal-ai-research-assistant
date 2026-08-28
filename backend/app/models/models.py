import datetime
import enum

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, Boolean
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import relationship

from app.database import Base


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

    user = relationship("User", back_populates="memories")

    def __repr__(self):
        return f"<Memory(id={self.id}, category='{self.category}')>"


class UserProvider(Base):
    """A saved LLM provider configuration for a user (multi-provider manager)."""

    __tablename__ = "user_providers"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_name = Column(String(50), nullable=False)
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
