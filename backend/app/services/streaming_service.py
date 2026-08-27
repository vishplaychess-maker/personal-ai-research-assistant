"""
Streaming service for Phase 5A — SSE-based token streaming.

Orchestrates the full chat pipeline for the streaming endpoint:
  1. Validate session and save user message
  2. Load conversation history
  3. Retrieve relevant memories (if enabled)
  4. Retrieve RAG context (if documents exist)
  5. Build combined system prompt
  6. Stream tokens from Ollama
  7. Save assistant message on completion
  8. Extract memory in background after completion

This service reuses the same services as the LangGraph workflow
(memory_service, rag_service, ollama_client) but bypasses LangGraph
to keep the non-streaming path unchanged.
"""

import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from sqlalchemy.orm import Session as DBSession

from app.models.models import Message, MessageRole, ResearchSession
from app.services.llm_providers import get_provider
from app.services.memory_service import (
    extract_memory_from_message,
    retrieve_relevant_memories,
    format_memories_for_prompt,
)
from app.services.rag_service import retrieve_chunks, format_rag_context, build_citation_list
from app.services.settings_service import get_memory_enabled

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────

MAX_HISTORY_MESSAGES = 20
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful research assistant. Answer the user's questions "
    "clearly and concisely."
)


# ── SSE event formatting ──────────────────────────────────


def format_sse(event: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ── Context preparation ───────────────────────────────────


class ChatContext:
    """Prepared context for a streaming chat response.

    Stores primitive/scalar values only — no ORM objects — to avoid
    DetachedInstanceError after db.commit() expires loaded instances.
    """

    def __init__(
        self,
        session_id: int,
        user_message: Message,
        history: List[Dict[str, str]],
        system_prompt: str,
        citations: List[Dict[str, Any]],
        sources_used: bool,
        memories_used: bool,
        model_name: Optional[str] = None,
    ):
        self.session_id = session_id
        self.user_message = user_message
        self.history = history
        self.system_prompt = system_prompt
        self.citations = citations
        self.sources_used = sources_used
        self.memories_used = memories_used
        self.model_name = model_name


def prepare_chat_context(
    session_id: int,
    user_input: str,
    db: DBSession,
    user_id: int = 1,
) -> ChatContext:
    """
    Prepare all context needed for a streaming chat response.

    Args:
        session_id: The session to chat in.
        user_input: The user's message text.
        db: Database session.
        user_id: The user ID (scoped session lookup).

    Steps:
      1. Validate session exists and belongs to user
      2. Save user message to database
      3. Load recent conversation history
      4. Retrieve memories (if enabled)
      5. Retrieve RAG document context (if documents exist)
      6. Build combined system prompt with memory and RAG context
    """
    # 1. Validate session belongs to user
    session = db.query(ResearchSession).filter(
        ResearchSession.id == session_id,
        ResearchSession.user_id == user_id,
    ).first()
    if not session:
        raise ValueError(f"Session {session_id} not found")

    # Load per-session model name and custom system prompt
    session_model = session.model
    session_system_prompt = session.system_prompt

    # 2. Save user message
    user_msg = Message(
        session_id=session.id,
        role=MessageRole.user,
        content=user_input,
    )
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    # 3. Load recent conversation history
    recent_messages = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(MAX_HISTORY_MESSAGES)
        .all()
    )
    recent_messages.reverse()

    history = [
        {"role": msg.role.value, "content": msg.content}
        for msg in recent_messages
        if msg.id != user_msg.id  # exclude the just-saved user message temporarily
    ]
    # Add the new user message at the end
    history.append({"role": "user", "content": user_input})

    # 4. Build system prompt parts
    # Use per-session custom system prompt if set, otherwise use default
    base_prompt = (
        session_system_prompt
        if session_system_prompt
        else DEFAULT_SYSTEM_PROMPT
    )
    system_parts = [base_prompt]

    # 5. Retrieve memories (if enabled)
    memories_used = False
    try:
        if get_memory_enabled(db):
            memories = retrieve_relevant_memories(db, user_id=user_id)
            if memories:
                memory_block = format_memories_for_prompt(memories)
                system_parts.append(memory_block)
                memories_used = True
    except Exception as exc:
        logger.warning("Memory retrieval failed (non-fatal): %s", exc)

    # 6. Retrieve RAG context (if documents exist)
    sources_used = False
    citations = []
    try:
        chunks = retrieve_chunks(session_id, user_input, db)
        if chunks:
            context_block = format_rag_context(chunks)
            citations = build_citation_list(chunks)
            system_parts.append(context_block)
            sources_used = True
        else:
            system_parts.append("If you don't know something, say so.")
    except Exception as exc:
        logger.warning("RAG retrieval failed (non-fatal): %s", exc)
        system_parts.append("If you don't know something, say so.")

    system_prompt = "\n\n".join(system_parts)

    return ChatContext(
        session_id=session.id,
        user_message=user_msg,
        history=history,
        system_prompt=system_prompt,
        citations=citations,
        sources_used=sources_used,
        memories_used=memories_used,
        model_name=session_model,
    )


# ── Streaming generator ───────────────────────────────────


async def stream_chat_response(
    context: ChatContext,
) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted events for a streaming chat response.

    Event sequence:
      1. ``event: start\n`` — signals the beginning of a stream
      2. ``event: token\n`` — zero or more token events
      3. ``event: complete\n`` — final event with message_id and citations
         OR ``event: error\n`` — terminal error
         OR ``event: cancelled\n`` — client disconnected

    Args:
        context: Prepared ChatContext with session, history, system prompt, etc.

    Yields:
        SSE-formatted strings ready for a StreamingResponse.
    """
    # Start event
    yield format_sse("start", {
        "session_id": context.session_id,
        "sources_used": context.sources_used,
        "memories_used": context.memories_used,
    })

    try:
        # Stream tokens from the configured LLM provider
        provider = get_provider()
        full_response = []
        async for chunk in provider.generate_stream_async(
            messages=context.history,
            system_prompt=context.system_prompt,
            model_name=context.model_name,
        ):
            if chunk["type"] == "token":
                full_response.append(chunk["token"])
                yield format_sse("token", {"token": chunk["token"]})

            elif chunk["type"] == "done":
                full_response_text = chunk.get("response", "")
                # Yield complete event with metadata
                yield format_sse("complete", {
                    "message_id": None,  # Will be filled after DB save
                    "citations": context.citations,
                    "sources_used": context.sources_used,
                    "memories_used": context.memories_used,
                    "content": full_response_text,
                })
                return

            elif chunk["type"] == "error":
                yield format_sse("error", {
                    "code": "OLLAMA_ERROR",
                    "detail": chunk["error"],
                })
                return

    except Exception as exc:
        logger.error("Unexpected streaming error: %s", exc, exc_info=True)
        yield format_sse("error", {
            "code": "INTERNAL_ERROR",
            "detail": "An unexpected error occurred during generation.",
        })


# ── Message persistence helpers ────────────────────────────


def save_assistant_message(
    session_id: int,
    content: str,
    citations: List[Dict[str, Any]],
    db: DBSession,
) -> Message:
    """
    Persist the completed assistant message to the database.

    Args:
        session_id: The session to save the message in.
        content: The full assistant response text.
        citations: List of citation dicts to serialize.
        db: Database session.

    Returns:
        The saved Message object.
    """
    citations_json = json.dumps(citations) if citations else None
    assistant_msg = Message(
        session_id=session_id,
        role=MessageRole.assistant,
        content=content,
        citations=citations_json,
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)
    return assistant_msg


def run_memory_extraction(
    user_input: str,
    db: DBSession,
    session_id: int,
    user_id: int = 1,
) -> None:
    """
    Run memory extraction in a best-effort manner.

    This is called after the stream completes. Failures are logged
    but do not affect the streaming response.
    """
    try:
        extract_memory_from_message(
            user_message=user_input,
            db=db,
            session_id=session_id,
            user_id=user_id,
        )
    except Exception as exc:
        logger.warning("Background memory extraction failed: %s", exc)
