import datetime
import enum

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float
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


class DocumentStatus(str, enum.Enum):
    processing = "processing"
    ready = "ready"
    failed = "failed"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    sessions = relationship("ResearchSession", back_populates="user", cascade="all, delete-orphan")
    memories = relationship("Memory", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}')>"


class ResearchSession(Base):
    __tablename__ = "research_sessions"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False, default="New Research Session")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
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
