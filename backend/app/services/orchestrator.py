"""OpenBot orchestrator (Phase 1).

A real, bounded pipeline that replaces the mock worker:

1. plan (deterministic router; no LLM latency),
2. collect read-only context (workspace/git + enabled official MCP servers),
3. run web research when the query asks for it and Tavily is configured,
4. inspect workspace artifacts with the local REA MCP server when the user
   enabled the ``rea_tools`` capability (read-only, workspace-confined),
5. synthesize a deterministic result from the gathered material.

It never edits files or runs user code (that remains gated for Phase 2), and it
checks the cancellation flag between every step.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.database import SessionLocal
from app.models.models import OpenbotTask
from app.services import agent_context, agent_events, rea_service
from app.services.capability_preferences import is_capability_enabled
from app.services.skill_executors import (
    WebSearchNotConfigured,
    WebSearchRateLimited,
    execute_web_search,
)

logger = logging.getLogger(__name__)

RESEARCH_HINTS = ("search", "news", "latest", "compare", "research", "find", "look up")
CODE_HINTS = ("refactor", "implement", "fix", "bug", "code", "test", "migrate")
REA_CAPABILITY_ID = "rea_tools"
REA_HINTS = (
    "reverse engineer",
    "reverse-engineer",
    "decompile",
    "disassemble",
    "binary",
    "malware",
    "firmware",
    "artifact",
    "apk",
    "pe file",
    "elf",
    "wasm",
    "pyc",
    "sqlite",
)
REA_SUFFIX_HINTS = rea_service.artifact_suffixes()


def _set_status(task_id: str, status: str, result_text: str | None = None) -> None:
    db = SessionLocal()
    try:
        task = db.query(OpenbotTask).filter(OpenbotTask.task_id == task_id).first()
        if task is None:
            return
        task.status = status
        if result_text is not None:
            task.result_text = result_text
        db.commit()
    except Exception:  # noqa: BLE001 - the worker must never crash the app
        db.rollback()
        logger.warning("openbot status update failed")
    finally:
        db.close()


def build_plan(query: str) -> list[dict]:
    text = (query or "").lower()
    plan = [
        {"step": "Collect workspace and MCP context", "agent": "context"},
    ]
    if any(hint in text for hint in RESEARCH_HINTS):
        plan.append({"step": "Search the web for current sources", "agent": "research"})
    if any(hint in text for hint in CODE_HINTS):
        plan.append(
            {
                "step": "Analyze the code task (read-only in Phase 1)",
                "agent": "code",
            }
        )
    if any(hint in text for hint in REA_HINTS) or any(
        suffix in text for suffix in REA_SUFFIX_HINTS
    ):
        plan.append(
            {
                "step": "Inspect workspace artifacts with REA (read-only)",
                "agent": "rea",
            }
        )
    plan.append({"step": "Synthesize the result", "agent": "synthesizer"})
    return plan


def _cancelled(task_id: str) -> bool:
    return agent_events.is_cancelled(task_id)


def _finish_cancelled(task_id: str) -> None:
    message = "Task cancelled by user."
    _set_status(task_id, "cancelled", result_text=message)
    agent_events.emit(task_id, "warning", message=message)
    agent_events.emit(task_id, "status", message="cancelled")


def _research(task_id: str, query: str) -> tuple[str, list[str]]:
    """Run the Tavily executor; return (text block, warnings)."""
    agent_events.emit(
        task_id, "tool", agent="research", tool="web_search",
        message="Searching the web",
    )
    try:
        results = execute_web_search(query, limit=3)
    except WebSearchNotConfigured:
        warning = "Web search is not configured on this server (TAVILY_API_KEY)."
        agent_events.emit(task_id, "warning", agent="research", message=warning)
        return "", [warning]
    except WebSearchRateLimited:
        warning = "Web search API limit reached; continuing without live sources."
        agent_events.emit(task_id, "warning", agent="research", message=warning)
        return "", [warning]
    except Exception as exc:  # noqa: BLE001 - research is optional
        logger.warning("openbot research failed (%s)", type(exc).__name__)
        warning = f"Web search failed ({type(exc).__name__}); continuing without it."
        agent_events.emit(task_id, "warning", agent="research", message=warning)
        return "", [warning]

    if not results:
        warning = "Web search returned no results."
        agent_events.emit(task_id, "warning", agent="research", message=warning)
        return "", [warning]

    lines = []
    for index, item in enumerate(results, 1):
        title = item.get("title") or item.get("url") or "Untitled"
        url = item.get("url") or ""
        snippet = " ".join((item.get("content") or "").split())[:240]
        lines.append(f"{index}. {title}\n   {url}\n   {snippet}")
    text = "\n".join(lines)
    agent_events.emit(task_id, "output", agent="research", output=text)
    return text, []


def _rea_warning(task_id: str, message: str) -> None:
    agent_events.emit(task_id, "warning", agent="rea", message=message)


def _rea_analysis(task_id: str, user_id: int, query: str) -> tuple[str, list[str]]:
    """Inspect workspace artifacts with REA, confined to the workspace.

    Returns (markdown block, warnings). Every failure is a warning; the task
    still completes with context/research results.
    """
    warnings: list[str] = []
    if not rea_service.is_enabled():
        message = "REA MCP is disabled on this server (REA_MCP_ENABLED)."
        _rea_warning(task_id, message)
        return "", [message]

    db = SessionLocal()
    try:
        enabled = is_capability_enabled(db, user_id, REA_CAPABILITY_ID)
    finally:
        db.close()
    if not enabled:
        message = "REA tools are not enabled for this user (Settings > Skills)."
        _rea_warning(task_id, message)
        return "", [message]

    agent_events.emit(
        task_id, "thinking", agent="rea",
        message="Locating workspace artifacts for REA…",
    )
    try:
        candidates, candidate_warnings = rea_service.find_workspace_candidates(query)
    except rea_service.ReaUnavailable as exc:
        message = f"REA is unavailable: {exc}"
        _rea_warning(task_id, message)
        return "", [message]

    warnings.extend(candidate_warnings)
    for warning in candidate_warnings:
        _rea_warning(task_id, warning)

    blocks: list[str] = []
    for path in candidates:
        if _cancelled(task_id):
            break
        tool = rea_service.tool_for_path(path)
        agent_events.emit(
            task_id, "tool", agent="rea", tool=tool,
            message=f"Inspecting {path.name} with REA",
        )
        try:
            text, is_error = rea_service.call_tool(tool, {"path": str(path)})
        except (
            rea_service.ReaUnavailable,
            rea_service.ReaToolNotAllowed,
            rea_service.ReaConfinementError,
        ) as exc:
            message = f"REA refused {path.name}: {exc}"
            warnings.append(message)
            _rea_warning(task_id, message)
            continue
        except Exception as exc:  # noqa: BLE001 - analysis is optional
            logger.warning("REA tool failed (%s)", type(exc).__name__)
            message = f"REA analysis failed for {path.name} ({type(exc).__name__})."
            warnings.append(message)
            _rea_warning(task_id, message)
            continue

        if is_error:
            message = f"REA returned an error for {path.name}."
            warnings.append(message)
            _rea_warning(task_id, message)
            continue
        if not text:
            message = f"REA returned no output for {path.name}."
            warnings.append(message)
            _rea_warning(task_id, message)
            continue

        agent_events.emit(task_id, "output", agent="rea", output=text)
        blocks.append(f"### {path.name} — `{tool}`\n\n```json\n{text}\n```")

    return "\n\n".join(blocks), warnings


def _synthesize(query: str, plan: list[dict], context: agent_context.ContextBundle,
                research: str, rea_text: str, warnings: list[str]) -> str:
    parts = [
        "# OpenBot Task Result",
        "",
        f"**Request:** {query}",
        "",
        "## Plan",
        *[f"{i}. {step['step']} — `{step['agent']}`" for i, step in enumerate(plan, 1)],
        "",
        "## Context",
        context.text() or "_No workspace or MCP context was available._",
        "",
        "## Web research",
        research or "_No live web sources were used._",
    ]
    if rea_text:
        parts += ["", "## REA analysis", rea_text]
    if warnings:
        parts += ["", "## Warnings", *[f"- {warning}" for warning in warnings]]
    parts += [
        "",
        "## Notes",
        "Phase 1 executes context collection, research, and read-only REA "
        "inspection. Autonomous file edits/terminal actions stay disabled "
        "pending the approval-gated CodeAgent.",
        f"_Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_",
    ]
    return "\n".join(parts)


def run_task(task_id: str, user_id: int, query: str) -> None:
    """pending -> running -> completed/failed/cancelled (real Phase 1 pipeline)."""
    _set_status(task_id, "running")
    agent_events.emit(task_id, "status", message="running")
    agent_events.emit(task_id, "thinking", message="Decomposing the request into steps…")
    plan = build_plan(query)
    agent_events.emit(
        task_id, "plan", message="Plan ready", output="\n".join(
            f"{i}. {step['step']}" for i, step in enumerate(plan, 1)
        ),
    )

    warnings: list[str] = []
    try:
        if _cancelled(task_id):
            return _finish_cancelled(task_id)

        db = SessionLocal()
        try:
            context = agent_context.collect(db, user_id, agent_events.emit_wrapper(task_id))
        finally:
            db.close()
        warnings.extend(context.warnings)
        agent_events.emit(
            task_id, "thinking", agent="context",
            message=f"Context collected ({len(context.tools)} tool result(s))",
        )

        if _cancelled(task_id):
            return _finish_cancelled(task_id)

        research = ""
        if any(step["agent"] == "research" for step in plan):
            research, research_warnings = _research(task_id, query)
            warnings.extend(research_warnings)

        if _cancelled(task_id):
            return _finish_cancelled(task_id)

        rea_text = ""
        if any(step["agent"] == "rea" for step in plan):
            rea_text, rea_warnings = _rea_analysis(task_id, user_id, query)
            warnings.extend(rea_warnings)

        if any(step["agent"] == "code" for step in plan):
            note = (
                "Code execution is disabled in Phase 1; this task is analyzed "
                "read-only. Enable the approval-gated CodeAgent in Phase 2."
            )
            warnings.append(note)
            agent_events.emit(task_id, "warning", agent="code", message=note)

        if _cancelled(task_id):
            return _finish_cancelled(task_id)

        agent_events.emit(task_id, "thinking", agent="synthesizer", message="Synthesizing…")
        result = _synthesize(query, plan, context, research, rea_text, warnings)
        _set_status(task_id, "completed", result_text=result)
        agent_events.emit(task_id, "output", agent="synthesizer", output=result)
        agent_events.emit(task_id, "status", message="completed")
    except Exception:  # noqa: BLE001 - a task failure is a status, not a crash
        logger.exception("openbot task failed")
        _set_status(task_id, "failed", result_text="The agent task failed.")
        agent_events.emit(task_id, "warning", message="The agent task failed.")
        agent_events.emit(task_id, "status", message="failed")
