"""
Minimal LangGraph workflow for research chat.

Nodes:
  load_context    — Fetch session info and recent messages from DB.
  generate_answer — Call Ollama with conversation history.
  save_output     — Persist the assistant's response to SQLite.

Graph:
  load_context → generate_answer → save_output → END
  If generate_answer errors → END (skip save_output)
"""

from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy.orm import Session as DBSession

from app.models.models import Message, MessageRole, ResearchSession
from app.services.ollama_client import generate_response

# ── Workflow state ─────────────────────────────────────────


class WorkflowState(TypedDict):
    """State passed between LangGraph nodes."""

    session_id: int
    user_input: str
    messages: List[Dict[str, str]]
    response: str
    assistant_message: Optional[Any]  # SQLAlchemy Message object
    error: Optional[str]
    # We carry the db session object for save_output.
    # Not serializable, but this graph doesn't use checkpointing.
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


# ── Node: generate_answer ──────────────────────────────────


def generate_answer(state: WorkflowState) -> WorkflowState:
    """Call Ollama with conversation history and return a response."""
    if state.get("error"):
        return state

    history = state.get("messages", [])
    user_input = state.get("user_input", "")

    # Ensure the latest user message is included
    # (it may already be in history if load_context fetched it from DB)
    if not history or history[-1].get("content") != user_input:
        history.append({"role": "user", "content": user_input})

    system_prompt = (
        "You are a helpful research assistant. Answer the user's questions "
        "clearly and concisely. If you don't know something, say so."
    )

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
    """Save the assistant's response to the database."""
    if state.get("error"):
        state["assistant_message"] = None
        return state

    response = state.get("response", "")
    if not response:
        return state

    db: DBSession = state["db"]

    assistant_msg = Message(
        session_id=state["session_id"],
        role=MessageRole.assistant,
        content=response,
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)

    state["assistant_message"] = assistant_msg
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
    workflow.add_node("generate_answer", generate_answer)
    workflow.add_node("save_output", save_output)

    # Define edges
    workflow.set_entry_point("load_context")
    workflow.add_edge("load_context", "generate_answer")
    workflow.add_conditional_edges(
        "generate_answer",
        _route_after_generate,
        {
            "save_output": "save_output",
            "error_end": END,
        },
    )
    workflow.add_edge("save_output", END)

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
      - error: error string (or None)
    """
    initial_state: WorkflowState = {
        "session_id": session_id,
        "user_input": user_input,
        "messages": [],
        "response": "",
        "assistant_message": None,
        "error": None,
        "db": db,
    }

    final_state = _workflow_app.invoke(initial_state)

    return {
        "response": final_state.get("response", ""),
        "assistant_message": final_state.get("assistant_message"),
        "error": final_state.get("error"),
    }
