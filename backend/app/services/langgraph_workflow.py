"""
Minimal LangGraph workflow for research chat with RAG and Long-Term Memory.

Nodes:
  load_context       — Fetch session info and recent messages from DB.
  retrieve_memories  — Load relevant user memories if memory is enabled.
  retrieve_context   — If session has documents, retrieve relevant chunks via RAG.
  generate_answer    — Call Ollama with conversation history + document context + memories.
  save_output        — Persist the assistant's response and citations to SQLite.
  extract_memory     — After generating answer, extract any durable memory from user input.

Graph:
  load_context → retrieve_memories → retrieve_context → generate_answer → save_output → extract_memory → END
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy.orm import Session as DBSession

from app.models.models import Message, MessageRole, ResearchSession
from app.services.ollama_client import generate_response
from app.services.rag_service import (
    retrieve_chunks,
    format_rag_context,
    build_citation_list,
)
from app.services.memory_service import (
    extract_memory_from_message,
    retrieve_relevant_memories,
    format_memories_for_prompt,
    MemoryExtractionResult,
)
from app.services.settings_service import get_memory_enabled


logger = logging.getLogger(__name__)

# ── Workflow state ─────────────────────────────────────────


class WorkflowState(TypedDict):
    """State passed between LangGraph nodes."""

    session_id: int
    user_input: str
    messages: List[Dict[str, str]]
    response: str
    retrieved_context: str
    citations: List[Dict[str, Any]]
    sources_used: bool
    assistant_message_id: Optional[int]  # ID of the saved assistant Message (avoids ORM detach issues)
    memory_context: str  # Formatted memory block for the prompt
    memories_used: bool  # Whether memories were injected into the prompt
    extraction_result: Optional[Dict[str, Any]]  # Latest memory extraction outcome
    error: Optional[str]
    db: Optional[Any]


# ── Node: load_context ─────────────────────────────────────

MAX_HISTORY_MESSAGES = 20


def load_context(state: WorkflowState) -> WorkflowState:
    """Load the session and its recent messages from SQLite."""
    db: DBSession = state["db"]
    session_id = state["session_id"]

    # Verify the session exists
    session = db.query(ResearchSession).filter(
        ResearchSession.id == session_id
    ).first()
    if not session:
        return {**state, "error": f"Session {session_id} not found"}

    # Fetch the most recent messages (oldest first for context ordering)
    recent_messages = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(MAX_HISTORY_MESSAGES)
        .all()
    )
    # Reverse to chronological order
    recent_messages.reverse()

    # Convert to simple dicts
    history: List[Dict[str, str]] = [
        {"role": msg.role.value, "content": msg.content}
        for msg in recent_messages
    ]

    state["messages"] = history
    return state


# ── Node: retrieve_memories ────────────────────────────────


def retrieve_memories(state: WorkflowState) -> WorkflowState:
    """
    Load relevant user memories and format them for the prompt.

    If memory is disabled (persisted SQLite setting), skip the
    retrieval call entirely and return empty context. This is the
    primary gate — the retrieve_relevant_memories function also
    checks the setting, but this early return avoids an unnecessary
    DB query and provides defense in depth.
    """
    if state.get("error"):
        return state

    db: DBSession = state["db"]

    # Defense in depth: check the persisted setting before even calling
    # retrieve_relevant_memories. The function also checks internally,
    # but this avoids the DB query entirely when disabled.
    if not get_memory_enabled(db):
        state["memory_context"] = ""
        state["memories_used"] = False
        return state

    memories = retrieve_relevant_memories(db)
    if not memories:
        state["memory_context"] = ""
        state["memories_used"] = False
        return state

    memory_block = format_memories_for_prompt(memories)
    state["memory_context"] = memory_block
    state["memories_used"] = True
    return state


# ── Node: retrieve_context ─────────────────────────────────


def retrieve_context(state: WorkflowState) -> WorkflowState:
    """
    If the session has ready documents, retrieve relevant chunks
    and format them as context for the LLM prompt.
    """
    if state.get("error"):
        return state

    db: DBSession = state["db"]
    session_id = state["session_id"]
    user_input = state.get("user_input", "")

    chunks = retrieve_chunks(session_id, user_input, db)
    if not chunks:
        state["retrieved_context"] = ""
        state["citations"] = []
        state["sources_used"] = False
        return state

    context_block = format_rag_context(chunks)
    citations = build_citation_list(chunks)

    state["retrieved_context"] = context_block
    state["citations"] = citations
    state["sources_used"] = True
    return state


# ── Node: generate_answer ──────────────────────────────────


def generate_answer(state: WorkflowState) -> WorkflowState:
    """Call Ollama with conversation history and RAG context."""
    if state.get("error"):
        return state

    history = state.get("messages", [])
    user_input = state.get("user_input", "")

    # Ensure the latest user message is included
    if not history or history[-1].get("content") != user_input:
        history.append({"role": "user", "content": user_input})

    # Build system prompt with memories and RAG context
    system_parts = [
        "You are a helpful research assistant. Answer the user's questions "
        "clearly and concisely."
    ]

    # Add memory context if available
    memory_context = state.get("memory_context", "")
    if memory_context:
        system_parts.append(memory_context)

    # Add RAG context if available
    retrieved_context = state.get("retrieved_context", "")
    if retrieved_context:
        system_parts.append(retrieved_context)
    else:
        system_parts.append(
            "If you don't know something, say so."
        )

    system_prompt = "\n\n".join(system_parts)

    try:
        response = generate_response(
            messages=history,
            system_prompt=system_prompt,
        )
        state["response"] = response
    except (ConnectionError, TimeoutError, RuntimeError) as exc:
        state["error"] = str(exc)
        state["response"] = ""

    return state


# ── Node: save_output ──────────────────────────────────────


def save_output(state: WorkflowState) -> WorkflowState:
    """Save the assistant's response and citations to the database."""
    if state.get("error"):
        state["assistant_message"] = None
        return state

    response = state.get("response", "")
    if not response:
        return state

    db: DBSession = state["db"]

    # Serialize citations as JSON if present
    citations = state.get("citations", [])
    citations_json = json.dumps(citations) if citations else None

    assistant_msg = Message(
        session_id=state["session_id"],
        role=MessageRole.assistant,
        content=response,
        citations=citations_json,
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)

    # Store the ID instead of the ORM object to avoid session-detach issues
    # when subsequent nodes (e.g. extract_memory) commit on the same session.
    state["assistant_message_id"] = assistant_msg.id
    return state


# ── Node: extract_memory ────────────────────────────────────


def extract_memory(state: WorkflowState) -> WorkflowState:
    """
    After generating the answer, check if the user's input contains
    a durable memory to save.

    If memory is disabled, skip extraction entirely (defense in depth
    — extract_memory_from_message also checks the setting internally).

    The extraction result is stored in state['extraction_result'] so the
    API layer can surface it to the frontend. The workflow itself does
    NOT fail when extraction fails — it's a background enrichment step.
    """
    if state.get("error"):
        return state

    db: DBSession = state["db"]

    if not get_memory_enabled(db):
        state["extraction_result"] = {
            "saved": False,
            "reason": "disabled",
        }
        return state

    user_input = state.get("user_input", "")
    session_id = state["session_id"]

    result: MemoryExtractionResult = extract_memory_from_message(
        user_message=user_input,
        db=db,
        session_id=session_id,
    )

    # Expose extraction result so the frontend can display feedback
    state["extraction_result"] = {
        "saved": result.saved,
        "memory_id": result.memory_id,
        "reason": result.reason,
        "content": result.content,
        "category": result.category,
    }

    if not result.saved and result.reason not in (
        "disabled", "nothing_to_save", "sensitive_input",
    ):
        logger.info(
            "Memory extraction did not save: reason=%s for message=%.60s",
            result.reason, user_input,
        )

    return state


# ── Build graph ────────────────────────────────────────────


def _route_after_generate(state: WorkflowState) -> str:
    """If there was an error, skip save_output; otherwise proceed."""
    if state.get("error"):
        return "error_end"
    return "save_output"


def build_workflow() -> StateGraph:
    """Build and compile the LangGraph research workflow."""
    workflow = StateGraph(WorkflowState)

    # Register nodes
    workflow.add_node("load_context", load_context)
    workflow.add_node("retrieve_memories", retrieve_memories)
    workflow.add_node("retrieve_context", retrieve_context)
    workflow.add_node("generate_answer", generate_answer)
    workflow.add_node("save_output", save_output)
    workflow.add_node("extract_memory", extract_memory)

    # Define edges
    workflow.set_entry_point("load_context")
    workflow.add_edge("load_context", "retrieve_memories")
    workflow.add_edge("retrieve_memories", "retrieve_context")
    workflow.add_edge("retrieve_context", "generate_answer")
    workflow.add_conditional_edges(
        "generate_answer",
        _route_after_generate,
        {
            "save_output": "save_output",
            "error_end": END,
        },
    )
    workflow.add_edge("save_output", "extract_memory")
    workflow.add_edge("extract_memory", END)

    return workflow.compile()


# Compiled singleton
_workflow_app = build_workflow()


def run_research_workflow(
    session_id: int,
    user_input: str,
    db: DBSession,
) -> dict:
    """
    Run the research workflow synchronously.

    Returns a dict with keys:
      - response: the generated text
      - assistant_message: the SQLAlchemy Message object (or None)
      - citations: list of citation dicts
      - sources_used: whether document context was included
      - memories_used: whether memories were injected into the prompt
      - extraction_result: dict with memory extraction outcome (saved, reason, etc.)
      - error: error string (or None)
    """
    initial_state: WorkflowState = {
        "session_id": session_id,
        "user_input": user_input,
        "messages": [],
        "response": "",
        "retrieved_context": "",
        "citations": [],
        "sources_used": False,
        "assistant_message_id": None,
        "memory_context": "",
        "memories_used": False,
        "extraction_result": None,
        "error": None,
        "db": db,
    }

    final_state = _workflow_app.invoke(initial_state)

    return {
        "response": final_state.get("response", ""),
        "assistant_message_id": final_state.get("assistant_message_id"),
        "citations": final_state.get("citations", []),
        "sources_used": final_state.get("sources_used", False),
        "memories_used": final_state.get("memories_used", False),
        "extraction_result": final_state.get("extraction_result"),
        "error": final_state.get("error"),
    }
