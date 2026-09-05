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

import asyncio
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
from app.services.settings_service import get_memory_enabled, get_user_llm_config
from app.services import tool_registry
from app.services.system_prompts import build_base_prompt, build_mcp_tools_block
from app.config import settings
from app.tools.youtube_summarizer import youtube_summarizer, is_youtube_url
from app.tools.python_sandbox import extract_python_code, format_code_result, run_python_code_async
from app.tools.mcp_tool import extract_mcp_calls, run_mcp_calls

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────

MAX_HISTORY_MESSAGES = 20
# Agent loop guardrail: rough char-count cap to prevent runaway generation.
MAX_TOKENS_PER_TASK = 10000
DEFAULT_SYSTEM_PROMPT = build_base_prompt(
    terminal_enabled=settings.enable_terminal_tool
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
        history: List[Dict[str, Any]],
        system_prompt: str,
        citations: List[Dict[str, Any]],
        sources_used: bool,
        memories_used: bool,
        user_id: int = 1,
        model_name: Optional[str] = None,
        provider_config: Optional[dict] = None,
        user_input: Optional[str] = None,
        directives_block: str = "",
    ):
        self.session_id = session_id
        self.user_message = user_message
        self.history = history
        self.system_prompt = system_prompt
        self.citations = citations
        self.sources_used = sources_used
        self.memories_used = memories_used
        self.user_id = user_id
        self.model_name = model_name
        self.provider_config = provider_config
        self.user_input = user_input
        self.directives_block = directives_block


def prepare_chat_context(
    session_id: int,
    user_input: str,
    db: DBSession,
    user_id: int = 1,
    image_url: Optional[str] = None,
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
        image_url=image_url,
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
        {"role": msg.role.value, "content": msg.content, **({"image_url": msg.image_url} if msg.image_url else {})}
        for msg in recent_messages
        if msg.id != user_msg.id  # exclude the just-saved user message temporarily
    ]
    # Add the new user message at the end
    if image_url:
        # Multimodal message
        history.append({
            "role": "user",
            "content": [
                {"type": "text", "text": user_input},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
        })
    else:
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

    # F6 Cap 3: inject standing "lessons learned" directives (if any)
    directives_block = ""
    try:
        from app.services.system_prompts import directives_context

        directive_block = directives_context(db, user_id)
        if directive_block:
            system_parts.append(directive_block)
            directives_block = directive_block
    except Exception as exc:
        logger.warning("Directive prompt injection failed (non-fatal): %s", exc)

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

    # 6b. MCP tools catalog (never break the chat on failure)
    if settings.enable_mcp_tool:
        try:
            mcp_block = build_mcp_tools_block(tool_registry.list_tools(db, user_id))
            if mcp_block:
                system_parts.append(mcp_block)
        except Exception as exc:
            logger.warning("MCP prompt block failed (non-fatal): %s", exc)

    # L2 skills: if the user explicitly requests a skill via [USE_SKILL: <name>],
    # load its full body into the prompt so the model follows it this turn.
    # Deterministic, no regeneration loop — the marker comes from the user
    # message (or an earlier decision) and the body is loaded upfront.
    try:
        from app.skills.loader import extract_skill_calls, load_skill_body

        for skill_name in extract_skill_calls(user_input):
            body_block = load_skill_body(skill_name)
            if body_block:
                system_parts.append(body_block)
    except Exception as exc:
        logger.warning("Skill (L2) injection failed (non-fatal): %s", exc)

    # 7. Agentic web scraping — detect URLs, invoke web_scraper tool
    web_scraped = False
    try:
        from app.tools.web_scraper import web_scraper, extract_urls

        urls = extract_urls(user_input)
        if urls:
            web_parts = []
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

            if web_parts:
                system_parts.append("\n\n".join(web_parts))
                web_scraped = True
    except Exception as exc:
        logger.warning("Web scraping failed (non-fatal): %s", exc)

    # 7b. Deep Research Mode — autonomous web search + scrape, gated by the
    # Phase 4 keyword detector: runs ONLY when the user did not supply a URL
    # AND the prompt explicitly asks for research (short/ambiguous prompts
    # skip the expensive search+scrape pass).
    if settings.enable_deep_research and not web_scraped:
        from app.services.agent_personas import detect_research_task

        if detect_research_task(user_input):
            try:
                from app.tools.deep_research import run_deep_research

                research_context = run_deep_research(user_input)
                if research_context:
                    system_parts.append(research_context)
            except Exception as exc:
                logger.warning("Deep research failed (non-fatal): %s", exc)

    system_prompt = "\n\n".join(system_parts)

    # Per-user LLM provider settings (override global .env when set)
    provider_config = get_user_llm_config(db, user_id)

    # Override provider config model with session-specific model if set
    if session_model and provider_config is not None:
        provider_config = dict(provider_config)
        provider_config["model"] = session_model

    return ChatContext(
        session_id=session.id,
        user_message=user_msg,
        history=history,
        system_prompt=system_prompt,
        citations=citations,
        sources_used=sources_used,
        memories_used=memories_used,
        user_id=user_id,
        model_name=session_model,
        provider_config=provider_config,
        user_input=user_input,
        directives_block=directives_block,
    )


# ── Multi-agent collaboration (Phase 3) ───────────────────


def _agent_status_sse(agent: str, message: str) -> str:
    """Format an agent_status SSE event so the UI can show who is working."""
    return format_sse("agent_status", {
        "type": "agent_status",
        "agent": agent,
        "message": message,
    })


async def stream_multi_agent_response(
    context: ChatContext,
) -> AsyncGenerator[str, None]:
    """
    Run the Researcher -> Coder -> Reviewer team for a complex task and
    stream its progress as ``agent_status`` SSE events.

    The final user-facing answer is the Reviewer-approved output (never raw
    Coder output). The review loop is hard-bounded: after MAX_REVIEW_RETRIES
    rejected cycles the Reviewer must approve the best version (the workflow
    force-approves), so the team can never deadlock.

    Event sequence:
      1. ``event: agent_status`` — one per agent hand-off
      2. ``event: token`` — the approved answer, chunked
      3. ``event: complete`` — final event (message is saved by the route)
         OR ``event: error`` — terminal error
    """
    # Imported here to avoid a module-level cycle and to reuse the same
    # patchable LLM wrapper the LangGraph workflow uses.
    from app.services.langgraph_workflow import generate_response
    from app.services.agent_personas import (
        AGENT_PERSONAS,
        MAX_REVIEW_RETRIES,
        parse_review_verdict,
        review_round_block,
    )

    async def _llm(messages: List[Dict[str, Any]], system_prompt: str) -> str:
        """Run a (blocking) persona LLM call off the event loop."""
        return await asyncio.to_thread(
            generate_response,
            messages=messages,
            system_prompt=system_prompt,
            model_name=context.model_name,
            provider_config=context.provider_config,
        )

    def _err(code: str, detail: str) -> str:
        return format_sse("error", {"code": code, "detail": detail})

    history = [m for m in context.history if isinstance(m.get("content"), str)]
    generated = 0

    # ── CAG layer 2: semantic cache lookup BEFORE the agent pipeline ──
    # A hit here streams the cached FINAL answer and skips the entire
    # Researcher -> Coder -> Reviewer team. Same guards as the single-agent
    # stream: plain-text user turn, no RAG context (answers can go stale).
    from app.services import semantic_cache

    cache_question = None
    _last_msg = context.history[-1] if context.history else None
    if (
        _last_msg
        and _last_msg.get("role") == "user"
        and isinstance(_last_msg.get("content"), str)
        and not context.sources_used
    ):
        cache_question = _last_msg["content"]
        cached, cache_kind = semantic_cache.chat_lookup(
            context.session_id, cache_question,
            context.provider_config, context.model_name,
        )
        if cached is not None:
            label = "[Semantic Cache Hit] " if cache_kind == "semantic" else "[Cached] "
            yield format_sse("token", {"token": label + cached})
            yield format_sse("complete", {
                "message_id": None,
                "citations": context.citations,
                "sources_used": context.sources_used,
                "memories_used": context.memories_used,
                "content": label + cached,
            })
            return

    def _budget_exceeded() -> bool:
        return generated >= MAX_TOKENS_PER_TASK

    # Phase 4: compact L1 skills index (names + descriptions only) injected
    # into the Coder and Reviewer prompts. L2 bodies still load on demand.
    skills_index = ""
    try:
        from app.skills.loader import skills_catalog
        skills_index = skills_catalog() or ""
    except Exception as exc:
        logger.warning("Skills index injection failed (non-fatal): %s", exc)

    try:
        # ── 1. Researcher: requirements + references -> research brief ──
        yield _agent_status_sse(
            "researcher", "Researcher agent is gathering requirements and references..."
        )
        researcher_prompt = "\n\n".join([
            AGENT_PERSONAS["researcher"]["system_prompt"],
            "=== Session Context (web results, documents, memories) ===",
            context.system_prompt,
        ])
        brief = await _llm(history, researcher_prompt)
        generated += len(brief)
        if _budget_exceeded():
            yield _err("TOKEN_BUDGET_EXCEEDED", "Generation stopped: token budget exceeded.")
            return

        def _coder_prompt() -> str:
            parts = [AGENT_PERSONAS["coder"]["system_prompt"]]
            if brief:
                parts.append("=== Research Brief (from the Researcher) ===\n" + brief)
            if skills_index:
                parts.append(skills_index)
            return "\n\n".join(parts)

        async def _run_sandbox(coder_output: str) -> str:
            code = extract_python_code(coder_output)
            if not code:
                return ""
            result = await run_python_code_async(code)
            return format_code_result(code, result)

        def _persist_reviewer_markers(text: str) -> str:
            """Persist [SAVE_DIRECTIVE]/[SAVE_MEMORY] the Reviewer emitted
            and resolve any [USE_MEMORY: ...] recall markers."""
            try:
                from app.database import SessionLocal
                from app.tools.memory_tool import (
                    process_memory_markers,
                    process_use_memory_markers,
                )
                from app.tools.directive_tool import process_directive_markers

                _db = SessionLocal()
                try:
                    text, _ = process_memory_markers(
                        text, _db, context.user_id, context.session_id
                    )
                    text, _ = process_use_memory_markers(
                        text, _db, context.user_id, context.session_id
                    )
                    text, _ = process_directive_markers(text, _db, context.user_id)
                finally:
                    _db.close()
            except Exception as exc:
                logger.warning(
                    "Reviewer marker persistence failed (non-fatal): %s", exc
                )
            return text

        # ── 2. Coder: implement the brief ─────────────────────────────
        yield _agent_status_sse("coder", "Coder agent is writing code...")
        code_out = await _llm(history, _coder_prompt())
        generated += len(code_out)
        if _budget_exceeded():
            yield _err("TOKEN_BUDGET_EXCEEDED", "Generation stopped: token budget exceeded.")
            return
        code_result = await _run_sandbox(code_out)

        # ── 3. Review loop (hard-bounded: max MAX_REVIEW_RETRIES rejections) ──
        rounds = 0            # completed REJECTED review cycles
        research_used = 0     # extra Researcher round-trips requested by the Reviewer
        final_text = ""
        while True:
            yield _agent_status_sse(
                "reviewer", "Reviewer agent is checking the code for bugs and security issues..."
            )
            review_parts = [
                AGENT_PERSONAS["reviewer"]["system_prompt"],
                review_round_block(rounds),
            ]
            if skills_index:
                review_parts.append(skills_index)
            if context.directives_block:
                review_parts.append(context.directives_block)
            review_parts.append("=== Original Request ===\n" + (context.user_input or ""))
            if brief:
                review_parts.append("=== Research Brief (from the Researcher) ===\n" + brief)
            review_parts.append("=== Coder Output (under review) ===\n" + code_out)
            if code_result:
                review_parts.append("=== Sandbox Result ===\n" + code_result)

            review = await _llm(history, "\n\n".join(review_parts))
            generated += len(review)
            review = _persist_reviewer_markers(review)
            verdict = parse_review_verdict(review)

            # Approve — or force-approve at the retry limit so the team
            # can never get stuck in an infinite revise loop.
            if verdict["approved"] or rounds >= MAX_REVIEW_RETRIES:
                if rounds >= MAX_REVIEW_RETRIES and not verdict["approved"]:
                    logger.info(
                        "Multi-agent review hit the %d-retry limit — forcing approval",
                        MAX_REVIEW_RETRIES,
                    )
                final_text = verdict["cleaned"] or code_out
                break

            # Reviewer can route back to the Researcher (bounded to once).
            if verdict["needs_research"] and research_used < 1:
                research_used += 1
                yield _agent_status_sse(
                    "researcher",
                    "Reviewer needs more information — Researcher agent is gathering it...",
                )
                research_msgs = history + [{
                    "role": "user",
                    "content": (
                        "The Reviewer needs more information before coding can "
                        "continue: " + (verdict["feedback"] or "see the request")
                    ),
                }]
                extra_brief = await _llm(research_msgs, researcher_prompt)
                generated += len(extra_brief)
                brief = ((brief + "\n\n" + extra_brief).strip()) if brief else extra_brief
                yield _agent_status_sse("coder", "Coder agent is updating the implementation...")
                code_out = await _llm(history, _coder_prompt())
                generated += len(code_out)
                if _budget_exceeded():
                    yield _err("TOKEN_BUDGET_EXCEEDED", "Generation stopped: token budget exceeded.")
                    return
                code_result = await _run_sandbox(code_out)
                continue

            # Rejected — send the Coder back with actionable feedback.
            rounds += 1
            yield _agent_status_sse(
                "coder",
                f"Reviewer sent feedback — Coder agent is revising (attempt {rounds}/{MAX_REVIEW_RETRIES})...",
            )
            logger.info(
                "Multi-agent review rejected (cycle %d/%d) in session %s",
                rounds, MAX_REVIEW_RETRIES, context.session_id,
            )
            revise_prompt = "\n\n".join([
                _coder_prompt(),
                "=== Reviewer Feedback (address EVERY point) ===\n"
                + (verdict["feedback"] or verdict["cleaned"]),
            ])
            code_out = await _llm(history, revise_prompt)
            generated += len(code_out)
            if _budget_exceeded():
                yield _err("TOKEN_BUDGET_EXCEEDED", "Generation stopped: token budget exceeded.")
                return
            code_result = await _run_sandbox(code_out)

        # ── 4. Stream the Reviewer-approved answer to the user ─────────
        final_text = (final_text or "").strip()
        # CAG: cache this FINAL (Reviewer-approved) answer. Skip when the
        # sandbox executed code — replaying must never re-run code paths.
        if cache_question and not code_result:
            semantic_cache.chat_store(
                context.session_id, cache_question, final_text,
                context.provider_config, context.model_name,
            )
        for i in range(0, len(final_text), 120):
            yield format_sse("token", {"token": final_text[i:i + 120]})
        yield format_sse("complete", {
            "message_id": None,  # Filled in by the route after DB save
            "citations": context.citations,
            "sources_used": context.sources_used,
            "memories_used": context.memories_used,
            "content": final_text,
        })

    except Exception as exc:
        logger.error("Multi-agent streaming error: %s", exc, exc_info=True)
        yield _err(
            "AGENT_ERROR",
            "The multi-agent team could not complete this task. Please try again.",
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

    # ── F6 Cap 1: emit an optional plan preview (never blocks the chat) ──
    from app.services.planning_service import generate_plan_for_query

    try:
        plan = generate_plan_for_query(
            query=context.user_input or "",
            messages=[m for m in context.history if isinstance(m.get("content"), str)],
            provider_config=context.provider_config,
            model_name=context.model_name,
        )
        yield format_sse("plan", {"steps": plan})
    except Exception as exc:
        logger.warning("Plan preview failed (non-fatal): %s", exc)
        yield format_sse("plan", {"steps": []})

    # ── Phase 3: Multi-Agent Collaboration for complex build/code tasks ──
    # Additive router with a safe fallback: anything that doesn't look like a
    # build/code task continues through the unchanged single-agent stream.
    # NOTE (Phase 4): research-keyword prompts do NOT divert this router —
    # detect_research_task is deliberately separate from detect_complex_task,
    # so prompts with build/code phrasing keep multi-agent routing priority.
    from app.services.agent_personas import detect_complex_task

    _last_msg = context.history[-1] if context.history else {}
    if (
        settings.enable_multi_agent
        and detect_complex_task(context.user_input or "")
        and isinstance(_last_msg.get("content"), str)
    ):
        logger.info(
            "Complex task detected — routing session %s to the multi-agent team",
            context.session_id,
        )
        async for sse_event in stream_multi_agent_response(context):
            yield sse_event
        return

    # ── CAG: serve an identical or semantically similar, context-free ──
    # repeat from cache. Only when the last turn is a plain-text user
    # question and no RAG context was injected (RAG answers can go stale as
    # documents change). Layer 1 = exact match (zero embedding cost),
    # layer 2 = ChromaDB-backed semantic match scoped to provider+model.
    from app.services import semantic_cache

    cache_question = None
    _last = context.history[-1] if context.history else None
    if (
        _last
        and _last.get("role") == "user"
        and isinstance(_last.get("content"), str)
        and not context.sources_used
    ):
        cache_question = _last["content"]
        cached, cache_kind = semantic_cache.chat_lookup(
            context.session_id, cache_question,
            context.provider_config, context.model_name,
        )
        if cached is not None:
            label = "[Semantic Cache Hit] " if cache_kind == "semantic" else "[Cached] "
            yield format_sse("token", {"token": label + cached})
            yield format_sse("complete", {
                "message_id": None,
                "citations": context.citations,
                "sources_used": context.sources_used,
                "memories_used": context.memories_used,
                "content": label + cached,
            })
            return

    try:
        # Stream tokens from the configured LLM provider, falling down the
        # free-tier chain (primary -> GLM 5.3 Flash -> free cloud -> Ollama)
        # when a provider fails BEFORE emitting any tokens. If a provider
        # fails mid-stream (after tokens were sent), the error is surfaced
        # as-is instead of duplicating partial output.
        from app.services.llm_providers import build_fallback_chain

        provider_chain = [
            get_provider(config=cfg)
            for cfg in build_fallback_chain(context.provider_config)
        ]
        for _p_idx, provider in enumerate(provider_chain):
            full_response = []
            # Streaming filter: never lets skill markers leak to the UI mid-stream.
            from app.skills.loader import SkillStreamFilter
            skill_stream = SkillStreamFilter()
            stream_error = None
            emitted_any = False
            async for chunk in provider.generate_stream_async(
                messages=context.history,
                system_prompt=context.system_prompt,
                model_name=context.model_name if _p_idx == 0 else None,
            ):
                if chunk["type"] == "token":
                    emitted_any = True
                    full_response.append(chunk["token"])
                    # Agent loop guardrail: stop streaming if token budget exceeded.
                    if len(full_response) >= MAX_TOKENS_PER_TASK:
                        yield format_sse("error", {
                            "code": "TOKEN_BUDGET_EXCEEDED",
                            "detail": "Generation stopped: token budget exceeded.",
                        })
                        return
                    visible = skill_stream.push(chunk["token"])
                    if visible:
                        yield format_sse("token", {"token": visible})

                elif chunk["type"] == "done":
                    # Flush any held-back non-marker text so the UI never stalls.
                    held = skill_stream.flush()
                    if held:
                        yield format_sse("token", {"token": held})
                    full_response_text = chunk.get("response", "")
                    # Execute any [PYTHON_CODE: ...] the LLM emitted (single-pass).
                    code = extract_python_code(full_response_text)
                    if code:
                        result = await run_python_code_async(code)
                        full_response_text += "\n\n" + format_code_result(code, result)
                    # [MCP_CALL: …] — Hermes auto-correction loop (up to 3 retries)
                    mcp_calls = []
                    mcp_retry = 0
                    if settings.enable_mcp_tool:
                        mcp_calls = extract_mcp_calls(full_response_text)
                        if mcp_calls:
                            from app.database import SessionLocal

                            _db = SessionLocal()
                            try:
                                mcp_result = run_mcp_calls(
                                    mcp_calls, _db, context.user_id
                                )
                                full_response_text += "\n\n" + mcp_result
                                # Hermes self-correction: surface fixing iteration
                                if "[error]" in mcp_result.lower() and mcp_retry < 3:
                                    fix_note = f"\n\n[Fixing error... attempt {mcp_retry+1}/3 - analyzing traceback and retrying]"
                                    full_response_text += fix_note
                                    yield format_sse("token", {"token": fix_note})
                                    mcp_retry += 1
                            finally:
                                _db.close()
                    # Skills (L2, free-model fallback): if the model emitted a skill
                    # marker in its text (<skill>name</skill> | USE SKILL: name |
                    # [USE_SKILL: name]), load the body and strip the markers from
                    # the user-visible response. Bounded — single load, no loop.
                    try:
                        from app.skills.loader import (
                            extract_skill_calls,
                            load_skill_body,
                            process_skill_markers,
                        )
                        skill_names = extract_skill_calls(full_response_text)
                        if skill_names:
                            blocks = []
                            for sname in skill_names:
                                body_block = load_skill_body(sname)
                                if body_block:
                                    blocks.append(body_block)
                            full_response_text = process_skill_markers(full_response_text)
                            if blocks:
                                full_response_text += "\n\n" + "\n\n".join(blocks)
                    except Exception as exc:
                        logger.warning("Skill marker processing failed (non-fatal): %s", exc)
                    # CAG: cache this FINAL answer for identical/similar future
                    # repeats. Skip when code/MCP ran (replay must not re-execute).
                    if cache_question and not code and not mcp_calls:
                        semantic_cache.chat_store(
                            context.session_id, cache_question, full_response_text,
                            context.provider_config, context.model_name,
                        )
                    # F6 Cap 2: advisory self-evaluation (never raises, null on error)
                    confidence = None
                    confidence_reason = None
                    try:
                        from app.services.evaluation_service import evaluate_response
                        _eval = evaluate_response(
                            response=full_response_text,
                            query=context.user_input or "",
                            messages=[m for m in context.history if isinstance(m.get("content"), str)],
                            provider_config=context.provider_config,
                            model_name=context.model_name,
                        )
                        confidence = _eval.get("confidence")
                        confidence_reason = _eval.get("reason")
                    except Exception:
                        logger.warning("F6 self-evaluation skipped (non-fatal)", exc_info=True)
                    # Yield complete event with metadata
                    yield format_sse("complete", {
                        "message_id": None,  # Will be filled after DB save
                        "citations": context.citations,
                        "sources_used": context.sources_used,
                        "memories_used": context.memories_used,
                        "content": full_response_text,
                        "confidence": confidence,
                        "confidence_reason": confidence_reason,
                    })
                    return

                elif chunk["type"] == "error":
                    # Remember the provider's error; fall back to the next
                    # provider in the chain (or surface it on the last one).
                    stream_error = chunk["error"]
                    break

            if stream_error and not emitted_any and _p_idx < len(provider_chain) - 1:
                logger.warning(
                    "Streaming provider %s failed (%s) — trying next fallback",
                    provider.name, stream_error,
                )
                continue
            if stream_error:
                # Pass through the provider's error with a generic code
                # The detail contains the actual error from the provider (e.g., "OpenRouter returned HTTP 404: Model not found")
                yield format_sse("error", {
                    "code": "PROVIDER_ERROR",
                    "detail": stream_error,
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
    confidence: Optional[int] = None,
    confidence_reason: Optional[str] = None,
) -> Message:
    """
    Persist the completed assistant message to the database.

    Args:
        session_id: The session to save the message in.
        content: The full assistant response text.
        citations: List of citation dicts to serialize.
        db: Database session.
        confidence: Optional F6 Cap 2 self-evaluation score (0-100).
        confidence_reason: Optional one-line why for the score.

    Returns:
        The saved Message object.
    """
    citations_json = json.dumps(citations) if citations else None
    assistant_msg = Message(
        session_id=session_id,
        role=MessageRole.assistant,
        content=content,
        citations=citations_json,
        confidence=confidence,
        confidence_reason=confidence_reason,
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
