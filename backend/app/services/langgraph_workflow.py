"""
LangGraph workflow for the AI Research Agent.

Nodes:
  load_context            — Fetch session info and recent messages from DB.
  retrieve_memories       — Load relevant user memories if memory is enabled.
  retrieve_context        — If session has documents, retrieve relevant chunks via RAG.
  browse_web              — Agentic: detect URLs -> invoke web_scraper tool -> inject content.
  generate_answer         — Call LLM; detect proposed commands; interrupt for approval if needed.
  ask_terminal_approval   — interrupt() — pause graph, return approval request to user.
  execute_terminal        — Run the approved command and store output.
  skip_terminal           — No-op when user denies the command.
  regenerate_answer       — Re-run LLM with command output injected into context.
  save_output             — Persist the assistant's response and citations to SQLite.
  extract_memory          — After generating answer, extract any durable memory.

Graph (normal path):
  load_context -> retrieve_memories -> retrieve_context -> browse_web
    -> generate_answer -> save_output -> extract_memory -> END

Graph (terminal approval path):
  load_context -> retrieve_memories -> retrieve_context -> browse_web
    -> generate_answer -> ask_terminal_approval (interrupt)
      -> execute_terminal -> regenerate_answer -> save_output -> extract_memory -> END
      OR
      -> skip_terminal -> save_output -> extract_memory -> END
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.models.models import Message, MessageRole, ResearchSession
from app.services.llm_providers import get_provider
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
from app.services.settings_service import get_memory_enabled, get_user_llm_config
from app.tools.web_scraper import extract_urls, web_scraper
from app.tools.youtube_summarizer import youtube_summarizer, is_youtube_url
from app.tools.python_sandbox import extract_python_code, format_code_result, run_python_code
from app.tools.terminal_executor import (
    extract_proposed_command,
    run_command,
    format_result_message,
)
from app.services.system_prompts import build_base_prompt


logger = logging.getLogger(__name__)


# ── Patchable generate_response wrapper ───────────────────


def generate_response(
    messages: List[Dict[str, str]],
    system_prompt: Optional[str] = None,
    model_name: Optional[str] = None,
    provider_config: Optional[dict] = None,
) -> str:
    """Call the configured LLM provider. Patchable for testing."""
    provider = get_provider(config=provider_config)
    return provider.generate_response(
        messages=messages,
        system_prompt=system_prompt,
        model_name=model_name,
    )


# ── Workflow state ─────────────────────────────────────────


class WorkflowState(TypedDict):
    """State passed between LangGraph nodes."""

    session_id: int
    user_input: str
    image_url: Optional[str]
    messages: List[Dict[str, Any]]
    response: str
    retrieved_context: str
    citations: List[Dict[str, Any]]
    sources_used: bool
    assistant_message_id: Optional[int]
    memory_context: str
    memories_used: bool
    extraction_result: Optional[Dict[str, Any]]
    web_context: str
    # ── Terminal tool state ────────────────────────────────
    pending_command: Optional[str]       # Command proposed by LLM, awaiting approval
    command_approved: Optional[bool]     # True=user approved, False=denied, None=no command
    command_result: Optional[str]        # Output of the executed command
    code_result: Optional[str]           # Output of executed Python code (sandbox)
    regenerate: bool                     # Whether generate_answer should re-run with results
    # ── Meta ──────────────────────────────────────────────
    error: Optional[str]
    db: Optional[Any]
    model_name: Optional[str]
    system_prompt: Optional[str]
    user_id: int


# ── Node: load_context ─────────────────────────────────────

MAX_HISTORY_MESSAGES = 20


def load_context(state: WorkflowState) -> WorkflowState:
    """Load the session and its recent messages from SQLite."""
    db: DBSession = state["db"]
    session_id = state["session_id"]

    session = db.query(ResearchSession).filter(
        ResearchSession.id == session_id
    ).first()
    if not session:
        return {**state, "error": f"Session {session_id} not found"}

    state["model_name"] = session.model
    state["system_prompt"] = session.system_prompt

    recent_messages = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(MAX_HISTORY_MESSAGES)
        .all()
    )
    recent_messages.reverse()

    history: List[Dict[str, Any]] = [
        {"role": msg.role.value, "content": msg.content, **({"image_url": msg.image_url} if msg.image_url else {})}
        for msg in recent_messages
    ]

    state["messages"] = history
    return state


# ── Node: retrieve_memories ────────────────────────────────


def retrieve_memories(state: WorkflowState) -> WorkflowState:
    """Load relevant user memories if memory is enabled."""
    if state.get("error"):
        return state

    db: DBSession = state["db"]

    if not get_memory_enabled(db):
        state["memory_context"] = ""
        state["memories_used"] = False
        return state

    memories = retrieve_relevant_memories(db, user_id=state.get("user_id", 1))
    if not memories:
        state["memory_context"] = ""
        state["memories_used"] = False
        return state

    state["memory_context"] = format_memories_for_prompt(memories)
    state["memories_used"] = True
    return state


# ── Node: retrieve_context ─────────────────────────────────


def retrieve_context(state: WorkflowState) -> WorkflowState:
    """Retrieve relevant document chunks via RAG."""
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

    state["retrieved_context"] = format_rag_context(chunks)
    state["citations"] = build_citation_list(chunks)
    state["sources_used"] = True
    return state


# ── Node: browse_web ──────────────────────────────────────


def browse_web(state: WorkflowState) -> WorkflowState:
    """Detect URLs in user input and scrape them with the web_scraper tool."""
    if state.get("error"):
        return state

    user_input = state.get("user_input", "")
    if not user_input:
        state["web_context"] = ""
        return state

    urls = extract_urls(user_input)
    if not urls:
        state["web_context"] = ""
        return state

    web_parts: list[str] = []
    for url in urls[:3]:
        if is_youtube_url(url):
            logger.info("Invoking youtube_summarizer tool for: %s", url)
            try:
                content = youtube_summarizer.invoke({"url": url})
                web_parts.append(
                    f"=== YouTube Summarizer Result for {url} ===\n"
                    f"{content}\n"
                    f"=== End of YouTube Summarizer Result ==="
                )
            except Exception as exc:
                logger.warning("youtube_summarizer failed for %s: %s", url, exc)
                web_parts.append(
                    f"=== YouTube Summarizer Result for {url} ===\n"
                    f"Error: {exc}\n"
                    f"=== End of YouTube Summarizer Result ==="
                )
        else:
            logger.info("Invoking web_scraper tool for: %s", url)
            try:
                content = web_scraper.invoke({"url": url})
                web_parts.append(
                    f"=== Web Scraper Result for {url} ===\n"
                    f"{content}\n"
                    f"=== End of Web Scraper Result ==="
                )
            except Exception as exc:
                logger.warning("web_scraper failed for %s: %s", url, exc)
                web_parts.append(
                    f"=== Web Scraper Result for {url} ===\n"
                    f"Error: {exc}\n"
                    f"=== End of Web Scraper Result ==="
                )

    state["web_context"] = "\n\n".join(web_parts)
    return state


# ── Helper: build system prompt ────────────────────────────


def _build_system_prompt(state: WorkflowState) -> str:
    """Assemble the full system prompt from base + memories + RAG + web + terminal."""
    custom_system_prompt = state.get("system_prompt", None)

    # Use per-session custom prompt if set, otherwise use the shared
    # advisor prompt with tool-specific instructions.
    base_prompt = (
        custom_system_prompt
        if custom_system_prompt
        else build_base_prompt(terminal_enabled=settings.enable_terminal_tool)
    )

    system_parts = [base_prompt]

    # Memory context
    memory_context = state.get("memory_context", "")
    if memory_context:
        system_parts.append(memory_context)

    # RAG context
    retrieved_context = state.get("retrieved_context", "")
    if retrieved_context:
        system_parts.append(retrieved_context)
    else:
        system_parts.append("If you don't know something, say so.")

    # Web browsing context
    web_context = state.get("web_context", "")
    if web_context:
        system_parts.append(web_context)

    # Command output context (after execution)
    command_result = state.get("command_result", "")
    if command_result:
        system_parts.append(command_result)

    # Python code execution result (sandbox)
    code_result = state.get("code_result", "")
    if code_result:
        system_parts.append(code_result)

    return "\n\n".join(system_parts)


# ── Node: generate_answer ──────────────────────────────────


def generate_answer(state: WorkflowState) -> WorkflowState:
    """Call the LLM and detect proposed terminal commands.

    Normal path:
      1. Build system prompt + conversation history.
      2. Call the LLM.
      3. If the response contains [PROPOSED_COMMAND: ...] and terminal tool
         is enabled, save a placeholder response and store the pending
         command in state. The next node (ask_terminal_approval) will
         interrupt the graph.

    Regenerate path (after command execution):
      If state["regenerate"] is True, the LLM is called again with the
      command output injected into the system prompt context. The new
      response replaces the placeholder.
    """
    if state.get("error"):
        return state

    history = list(state.get("messages", []))
    user_input = state.get("user_input", "")
    image_url = state.get("image_url", None)
    model_name = state.get("model_name", None)

    # Per-user LLM provider settings (override global .env when set)
    db: DBSession = state.get("db")
    provider_config = get_user_llm_config(db, state.get("user_id", 1)) if db else None

    # Override provider config model with session-specific model if set
    if model_name and provider_config is not None:
        provider_config = dict(provider_config)
        provider_config["model"] = model_name

    # Ensure the latest user message is in the history
    # If there's an image, create a multimodal message
    if not history or history[-1].get("content") != user_input:
        if image_url:
            # Multimodal message: text + image
            history.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": user_input},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            })
        else:
            history.append({"role": "user", "content": user_input})

    # If resuming from approval and NOT regenerating, return the existing
    # placeholder response so we flow straight to ask_terminal_approval.
    if state.get("pending_command") and not state.get("regenerate"):
        return state

    # If regenerating with command output, prepend a system message so the
    # LLM knows the command already ran.
    if state.get("regenerate") and state.get("command_result"):
        # The command_result is already in the system prompt via
        # _build_system_prompt, so just let the LLM see it.
        pass

    system_prompt = _build_system_prompt(state)

    # ── CAG: return an identical prior answer without an LLM call ──────
    # Session-scoped; skipped when regenerating, when a command is pending,
    # for image prompts, and when RAG context was injected (can go stale).
    from app.services import cache_service

    cache_question = None
    if (
        not state.get("regenerate")
        and not state.get("pending_command")
        and not image_url
        and not state.get("sources_used")
    ):
        cache_question = user_input
        _cached = cache_service.get(state.get("session_id"), user_input)
        if _cached is not None:
            state["response"] = "[Cached] " + _cached
            return state

    try:
        response = generate_response(
            messages=history,
            system_prompt=system_prompt,
            model_name=model_name,
            provider_config=provider_config,
        )
        # save_memory tool: persist any [SAVE_MEMORY: ...] markers the LLM emitted
        if db is not None:
            from app.tools.memory_tool import process_memory_markers
            cleaned, saved_count = process_memory_markers(
                response, db, state.get("user_id", 1), state.get("session_id")
            )
            if saved_count:
                logger.info(
                    "save_memory tool saved %d memory(ies) for user %s",
                    saved_count, state.get("user_id", 1),
                )
            response = cleaned
        state["response"] = response
    except (ConnectionError, TimeoutError, RuntimeError) as exc:
        state["error"] = str(exc)
        state["response"] = ""
        return state

    # ── Detect proposed command ──────────────────────────
    if settings.enable_terminal_tool and not state.get("regenerate"):
        command = extract_proposed_command(response)
        if command:
            logger.info("LLM proposed command: %s", command)
            state["pending_command"] = command
            state["command_approved"] = None
            state["command_result"] = ""

            # Save a placeholder response to the DB so the user sees
            # the LLM's explanation *before* the approval prompt.
            db: DBSession = state["db"]
            assistant_msg = Message(
                session_id=state["session_id"],
                role=MessageRole.assistant,
                content=response,
            )
            db.add(assistant_msg)
            db.commit()
            db.refresh(assistant_msg)
            state["assistant_message_id"] = assistant_msg.id

    # Detect LLM-emitted Python code (sandbox) - auto-execute, no approval.
    if not state.get("regenerate") and not state.get("pending_command"):
        code = extract_python_code(response)
        if code:
            logger.info("LLM emitted Python code (%d chars)", len(code))
            result = run_python_code(code)
            state["code_result"] = format_code_result(code, result)
            state["regenerate"] = True

            assistant_msg = Message(
                session_id=state["session_id"],
                role=MessageRole.assistant,
                content=response,
            )
            db.add(assistant_msg)
            db.commit()
            db.refresh(assistant_msg)
            state["assistant_message_id"] = assistant_msg.id

    # CAG: cache the answer for identical future repeats in this session.
    # Skip when a command/code path fired — those must not be replayed blindly.
    if (
        cache_question
        and not state.get("pending_command")
        and not state.get("code_result")
        and not state.get("error")
    ):
        cache_service.set(state.get("session_id"), cache_question, state["response"])

    # Clear regenerate flag after use
    if state.get("regenerate"):
        state["regenerate"] = False

    return state


# ── Node: ask_terminal_approval ────────────────────────────


def ask_terminal_approval(state: WorkflowState) -> WorkflowState:
    """Interrupt the graph and ask the user to approve/deny the command.

    Uses LangGraph's ``interrupt()`` to pause execution and surface
    an approval request to the user.  When the user responds (yes/no),
    the graph resumes via ``Command(resume=...)`` and this node receives
    the user's decision.
    """
    pending = state.get("pending_command")
    if not pending:
        return state

    # interrupt() pauses the graph and returns the user's response
    # when the graph is resumed with Command(resume=...)
    user_decision: str = interrupt(
        f"I'd like to run this command: `{pending}`\n"
        "Reply **yes** to approve, or **no** to deny."
    )

    # Store the decision
    decision = user_decision.strip().lower()
    state["command_approved"] = decision in ("yes", "y", "approve", "approved")

    if state["command_approved"]:
        logger.info("User APPROVED command: %s", pending)
    else:
        logger.info("User DENIED command: %s", pending)

    return state


# ── Node: execute_terminal ─────────────────────────────────


def execute_terminal(state: WorkflowState) -> WorkflowState:
    """Execute the approved command and store the output."""
    command = state.get("pending_command")
    approved = state.get("command_approved")

    if not command or not approved:
        state["command_result"] = ""
        return state

    logger.info("Executing approved command: %s", command)
    output = run_command(command)
    state["command_result"] = format_result_message(command, output)

    # Save the command execution result as a system message so it
    # appears in the conversation history.
    db: DBSession = state["db"]
    cmd_msg = Message(
        session_id=state["session_id"],
        role=MessageRole.assistant,
        content=f"✅ Executed: `{command}`\n\n```\n{output}\n```",
    )
    db.add(cmd_msg)
    db.commit()

    return state


# ── Node: skip_terminal ────────────────────────────────────


def skip_terminal(state: WorkflowState) -> WorkflowState:
    """Handle denied command — store denial message for the LLM."""
    command = state.get("pending_command")
    if command and state.get("command_approved") is False:
        state["command_result"] = (
            f"The user denied the command: `{command}`. "
            "Please answer the question without executing that command."
        )
    else:
        state["command_result"] = ""
    return state


# ── Node: regenerate_answer ────────────────────────────────


def regenerate_answer(state: WorkflowState) -> WorkflowState:
    """Re-run the LLM with command output injected into context.

    This node runs AFTER execute_terminal or skip_terminal.  It calls
    the LLM again with the same conversation history but the command
    output (or denial message) now visible in the system prompt.
    The new response replaces the placeholder saved earlier.
    """
    if state.get("error"):
        return state

    command_result = state.get("command_result", "")
    if not command_result and not state.get("code_result", ""):
        # No result to inject — skip regeneration
        return state

    history = list(state.get("messages", []))
    user_input = state.get("user_input", "")
    model_name = state.get("model_name", None)

    # Per-user LLM provider settings (override global .env when set)
    db: DBSession = state.get("db")
    provider_config = get_user_llm_config(db, state.get("user_id", 1)) if db else None

    # Override provider config model with session-specific model if set
    if model_name and provider_config is not None:
        provider_config = dict(provider_config)
        provider_config["model"] = model_name

    # Ensure user message is present
    if not history or history[-1].get("content") != user_input:
        history.append({"role": "user", "content": user_input})

    system_prompt = _build_system_prompt(state)

    try:
        response = generate_response(
            messages=history,
            system_prompt=system_prompt,
            model_name=model_name,
            provider_config=provider_config,
        )
        state["response"] = response

        # Update the placeholder message in the DB
        msg_id = state.get("assistant_message_id")
        if msg_id:
            db: DBSession = state["db"]
            msg = db.query(Message).filter(Message.id == msg_id).first()
            if msg:
                msg.content = response
                db.commit()

    except (ConnectionError, TimeoutError, RuntimeError) as exc:
        state["error"] = str(exc)
        state["response"] = ""

    return state


# ── Node: save_output ──────────────────────────────────────


def save_output(state: WorkflowState) -> WorkflowState:
    """Save the assistant's response and citations to the database."""
    if state.get("error"):
        state["assistant_message_id"] = None
        return state

    response = state.get("response", "")
    if not response:
        return state

    # If an assistant message was already saved (by generate_answer's
    # placeholder or regenerate_answer), skip creating a new one.
    if state.get("assistant_message_id"):
        return state

    db: DBSession = state["db"]
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

    state["assistant_message_id"] = assistant_msg.id
    return state


# ── Node: extract_memory ───────────────────────────────────


def extract_memory(state: WorkflowState) -> WorkflowState:
    """Extract durable memory from the user's input (best-effort)."""
    if state.get("error"):
        return state

    db: DBSession = state["db"]

    if not get_memory_enabled(db):
        state["extraction_result"] = {"saved": False, "reason": "disabled"}
        return state

    result: MemoryExtractionResult = extract_memory_from_message(
        user_message=state.get("user_input", ""),
        db=db,
        user_id=state.get("user_id", 1),
        session_id=state["session_id"],
    )

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
            result.reason, state.get("user_input", ""),
        )

    return state


# ── Routing ────────────────────────────────────────────────


def _route_after_generate(state: WorkflowState) -> str:
    """Route after generate_answer: terminal approval, error, or save."""
    if state.get("error"):
        return "error_end"
    if state.get("code_result"):
        return "regenerate_code"
    if state.get("pending_command") and state.get("command_approved") is None:
        return "ask_approval"
    return "save_output"


def _route_after_approval(state: WorkflowState) -> str:
    """Route after ask_terminal_approval: execute or skip."""
    if state.get("command_approved"):
        return "execute_terminal"
    return "skip_terminal"


# ── Build graph ────────────────────────────────────────────


def build_workflow() -> StateGraph:
    """Build and compile the LangGraph research workflow."""
    workflow = StateGraph(WorkflowState)

    # Register nodes
    workflow.add_node("load_context", load_context)
    workflow.add_node("retrieve_memories", retrieve_memories)
    workflow.add_node("retrieve_context", retrieve_context)
    workflow.add_node("browse_web", browse_web)
    workflow.add_node("generate_answer", generate_answer)
    workflow.add_node("ask_terminal_approval", ask_terminal_approval)
    workflow.add_node("execute_terminal", execute_terminal)
    workflow.add_node("skip_terminal", skip_terminal)
    workflow.add_node("regenerate_answer", regenerate_answer)
    workflow.add_node("save_output", save_output)
    workflow.add_node("extract_memory", extract_memory)

    # Define edges
    workflow.set_entry_point("load_context")
    workflow.add_edge("load_context", "retrieve_memories")
    workflow.add_edge("retrieve_memories", "retrieve_context")
    workflow.add_edge("retrieve_context", "browse_web")
    workflow.add_edge("browse_web", "generate_answer")

    # After generate: either terminal approval, error, or save
    workflow.add_conditional_edges(
        "generate_answer",
        _route_after_generate,
        {
            "ask_approval": "ask_terminal_approval",
            "regenerate_code": "regenerate_answer",
            "save_output": "save_output",
            "error_end": END,
        },
    )

    # After approval: execute or skip
    workflow.add_conditional_edges(
        "ask_terminal_approval",
        _route_after_approval,
        {
            "execute_terminal": "execute_terminal",
            "skip_terminal": "skip_terminal",
        },
    )

    # After execute/skip -> regenerate -> save -> extract_memory -> END
    workflow.add_edge("execute_terminal", "regenerate_answer")
    workflow.add_edge("skip_terminal", "regenerate_answer")
    workflow.add_edge("regenerate_answer", "save_output")
    workflow.add_edge("save_output", "extract_memory")
    workflow.add_edge("extract_memory", END)

    return workflow.compile()


# Compiled singleton
_workflow_app = build_workflow()


# ── Public API ─────────────────────────────────────────────


def run_research_workflow(
    session_id: int,
    user_input: str,
    db: DBSession,
    user_id: int = 1,
    resume_from: Optional[str] = None,
    image_url: Optional[str] = None,
) -> dict:
    """
    Run the research workflow synchronously.

    Args:
        session_id: The session to run in.
        user_input: The user's message.
        db: Database session.
        user_id: The authenticated user ID.
        resume_from: If resuming from an interrupt, this is the user's
            approval response ("yes" or "no"). When provided, the workflow
            resumes from the interrupt point instead of starting fresh.

    Returns a dict with keys:
      - response: the generated text
      - assistant_message_id: ID of the saved assistant Message
      - citations: list of citation dicts
      - sources_used / memories_used: booleans
      - extraction_result: dict with memory extraction outcome
      - pending_approval: str if the graph interrupted for approval
      - error: error string (or None)
    """
    # When resuming from an interrupt, read the pending command from the
    # last assistant message's citations (stored by generate_answer).
    pending_command_from_db = None
    assistant_msg_id_for_resume = None
    if resume_from is not None:
        last_msg = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == MessageRole.assistant,
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        if last_msg and last_msg.citations:
            try:
                meta = json.loads(last_msg.citations)
                if isinstance(meta, dict) and meta.get("pending_approval"):
                    pending_command_from_db = meta.get("pending_command")
                    assistant_msg_id_for_resume = last_msg.id
            except (json.JSONDecodeError, TypeError):
                pass

    initial_state: WorkflowState = {
        "session_id": session_id,
        "user_input": user_input,
        "image_url": image_url,
        "messages": [],
        "response": "",
        "retrieved_context": "",
        "citations": [],
        "sources_used": False,
        "assistant_message_id": assistant_msg_id_for_resume,
        "memory_context": "",
        "memories_used": False,
        "extraction_result": None,
        "web_context": "",
        "pending_command": pending_command_from_db,
        "command_approved": None,
        "command_result": "",
        "code_result": "",
        "regenerate": False,
        "error": None,
        "db": db,
        "model_name": None,
        "system_prompt": None,
        "user_id": user_id,
    }

    try:
        if resume_from is not None:
            final_state = _workflow_app.invoke(
                initial_state,
                config={"resume_from": Command(resume=resume_from)},
            )
        else:
            final_state = _workflow_app.invoke(initial_state)

    except Exception as exc:
        # LangGraph may raise GraphInterrupt for human-in-the-loop pauses.
        from langgraph.errors import GraphInterrupt

        if isinstance(exc, GraphInterrupt):
            interrupt_value = exc.args[0] if exc.args else ""
            return {
                "response": "",
                "assistant_message_id": initial_state.get("assistant_message_id"),
                "citations": [],
                "sources_used": False,
                "memories_used": False,
                "extraction_result": None,
                "pending_approval": str(interrupt_value),
                "pending_command": initial_state.get("pending_command"),
                "error": None,
            }
        raise

    # ── Detect interrupt via state (fallback) ─────────────
    # Some LangGraph versions return the state at the interrupt point
    # instead of raising.  If pending_command is set and command_approved
    # is still None, the graph paused for approval.
    if (
        final_state.get("pending_command")
        and final_state.get("command_approved") is None
    ):
        pending = final_state["pending_command"]
        return {
            "response": final_state.get("response", ""),
            "assistant_message_id": final_state.get("assistant_message_id"),
            "citations": [],
            "sources_used": False,
            "memories_used": False,
            "extraction_result": None,
            "pending_approval": (
                f"I'd like to run this command: `{pending}`\n"
                "Reply **yes** to approve, or **no** to deny."
            ),
            "pending_command": pending,
            "error": None,
        }

    return {
        "response": final_state.get("response", ""),
        "assistant_message_id": final_state.get("assistant_message_id"),
        "citations": final_state.get("citations", []),
        "sources_used": final_state.get("sources_used", False),
        "memories_used": final_state.get("memories_used", False),
        "extraction_result": final_state.get("extraction_result"),
        "pending_approval": None,
        "pending_command": final_state.get("pending_command"),
        "error": final_state.get("error"),
    }
