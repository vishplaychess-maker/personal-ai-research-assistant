"""Foundational three-agent swarm: Planner -> Researcher -> Synthesizer.

This is the OpenBot pipeline's optional collaboration mode (``use_swarm``),
separate from the durable run-engine team in ``app.run_engine`` (researcher
-> solution_specialist -> reviewer). It is deliberately simple and bounded:

* A fixed, server-owned roster (:data:`DEFAULT_SWARM`) — no dynamic roles,
  no router model call, no parallel execution.
* A :class:`SwarmState` carries the query, the plan, gathered evidence, the
  tools used, and warnings from one agent to the next.
* Every step emits real ``agent_events`` frames tagged with the acting role
  (``planner`` / ``researcher`` / ``synthesizer``), so the dashboard shows
  genuine per-agent activity — never simulated ones.
* The Synthesizer's report is generated with the USER's configured
  provider/model (same resolution as the single-agent synthesis), and every
  provider call runs under a daemon-thread timeout.
* Any failure (provider error, timeout, empty output) raises
  :class:`SwarmError`; the orchestrator catches it and falls back to the
  single-agent flow. Nothing here mutates task status itself.

No memory text, prompt, or credential is logged.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Tuple

from pydantic import BaseModel, Field

from app.config import settings
from app.database import SessionLocal
from app.services import agent_context, agent_events
from app.services.llm_providers import get_provider
from app.services.settings_service import get_user_llm_config
from app.services.skill_executors import (
    WebSearchNotConfigured,
    WebSearchRateLimited,
    execute_web_search,
)

logger = logging.getLogger(__name__)

#: Query hints shared with the orchestrator's plan heuristics.
RESEARCH_HINTS = ("search", "news", "latest", "compare", "research", "find", "look up")
WORKSPACE_HINTS = ("file", "files", "code", "module", "service", "workspace", "repo", "test")

MAX_PLAN_STEPS = 6

#: Strips "- ", "* ", "1. ", "1) " prefixes from planner output lines.
_PLAN_PREFIX_RE = re.compile(r"^(?:[-*•]|\d{1,2}[.)])\s*")


class SwarmError(RuntimeError):
    """A swarm step failed; the orchestrator must fall back to single agent."""


class SwarmAgent(BaseModel):
    name: str
    role: str
    goal: str
    backstory: str
    allowed_tools: List[str] = Field(default_factory=list)


class SwarmState(BaseModel):
    query: str
    user_id: str
    plan: List[str] = Field(default_factory=list)
    findings: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    final: str = ""

    def transcript(self, limit: int = 6000) -> str:
        """Bounded, labeled handoff text for the next agent's prompt."""
        sections = [
            "## Plan",
            "\n".join(f"{i}. {step}" for i, step in enumerate(self.plan, 1)) or "_None._",
            "## Findings",
            "\n\n".join(self.findings) or "_None._",
        ]
        text = "\n".join(sections)
        if limit and len(text) > limit:
            text = text[: max(0, limit - 1)].rstrip() + "…"
        return text


DEFAULT_SWARM: Tuple[SwarmAgent, ...] = (
    SwarmAgent(
        name="Planner",
        role="planner",
        goal="Break the user's request into ordered, actionable steps.",
        backstory=(
            "A meticulous project planner who turns vague requests into a short, "
            "concrete checklist an execution team can follow."
        ),
        allowed_tools=[],
    ),
    SwarmAgent(
        name="Researcher",
        role="researcher",
        goal="Gather facts and evidence with the available search tools.",
        backstory=(
            "A grounded analyst who prefers cited tool output over speculation and "
            "states plainly when evidence is unavailable."
        ),
        allowed_tools=["web_search", "search_workspace_files"],
    ),
    SwarmAgent(
        name="Synthesizer",
        role="synthesizer",
        goal="Combine the plan and findings into one polished final report.",
        backstory=(
            "A senior technical writer who produces compact Markdown reports with "
            "clear structure, honest gaps, and concrete next steps."
        ),
        allowed_tools=[],
    ),
)

PLANNER_PROMPT = (
    "You are the Planner of a three-agent team (Planner, Researcher, Synthesizer). "
    "Return ONLY a numbered list of 2 to 6 concrete steps that would complete the "
    "user's request. No preamble, no headings, no explanation."
)
RESEARCHER_PROMPT = (
    "You are the Researcher. Using ONLY the supplied tool evidence and the plan, "
    "extract the most relevant facts for the Synthesizer. If the evidence is thin, "
    "say exactly what is missing. Output short bullet points; never claim to have "
    "browsed anything beyond the supplied evidence."
)
SYNTHESIZER_PROMPT = (
    "You are the Synthesizer of a three-agent team. Write the final answer for the "
    "user in GitHub-flavoured Markdown: a one-line verdict, the key findings, the "
    "evidence used, and next steps. Use only the plan and findings provided; list "
    "open gaps honestly. Do not mention internal agent roles."
)


# ── LLM plumbing ───────────────────────────────────────────────────────────


def _agent_completion(user_id: str, system_prompt: str, user_prompt: str) -> str:
    """One provider call on the user's own provider/model, with a hard timeout."""
    db = SessionLocal()
    try:
        config = get_user_llm_config(db, int(user_id))
    finally:
        db.close()

    provider = get_provider(config=config) if config else get_provider()
    model_name = (config or {}).get("model") or None
    messages = [{"role": "user", "content": user_prompt}]

    outcome: Dict[str, object] = {}

    def work() -> None:
        try:
            outcome["text"] = provider.generate_response(
                messages=messages,
                system_prompt=system_prompt,
                model_name=model_name,
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = exc

    thread = threading.Thread(target=work, name="swarm-agent", daemon=True)
    thread.start()
    thread.join(timeout=float(settings.swarm_agent_timeout_s))
    if thread.is_alive():
        raise SwarmError("Swarm agent provider call timed out")
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise SwarmError(f"Swarm agent provider failed ({type(error).__name__})")
    return str(outcome.get("text") or "").strip()


def _parse_plan(text: str) -> List[str]:
    steps: List[str] = []
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        cleaned = _PLAN_PREFIX_RE.sub("", raw).strip()
        if cleaned:
            steps.append(cleaned[:300])
        if len(steps) >= MAX_PLAN_STEPS:
            break
    return steps


def _remaining_budget(started: float) -> float:
    return float(settings.swarm_timeout_s) - (time.monotonic() - started)


# ── Researcher tools ───────────────────────────────────────────────────────


def _gather_evidence(task_id: str, query: str) -> Tuple[str, List[str], List[str]]:
    """Run the read-only search tools available to the Researcher."""
    lowered = query.lower()
    parts: List[str] = []
    tools: List[str] = []
    warnings: List[str] = []

    if any(hint in lowered for hint in RESEARCH_HINTS):
        agent_events.emit(
            task_id, "tool", agent="researcher", tool="web_search",
            message="Searching the web…",
        )
        try:
            results = execute_web_search(query, limit=3)
        except WebSearchNotConfigured:
            warnings.append("Web search is not configured; using workspace evidence only.")
        except WebSearchRateLimited:
            warnings.append("Web search is rate limited; using workspace evidence only.")
        else:
            lines = [
                f"- {item.get('title', 'Untitled')} — {item.get('url', '')}\n"
                f"  {(item.get('content') or '')[:600]}"
                for item in results
            ]
            if lines:
                parts.append("### Web results\n" + "\n".join(lines))
                tools.append("web_search")

    if any(hint in lowered for hint in WORKSPACE_HINTS) or not parts:
        agent_events.emit(
            task_id, "tool", agent="researcher", tool="search_workspace_files",
            message="Searching the workspace…",
        )
        try:
            files = agent_context.search_workspace_files(".", "*.py")
        except (ValueError, OSError) as exc:
            warnings.append(f"Workspace search refused: {exc}")
        else:
            if files:
                lines = [
                    f"- {item['path']} ({item['size']} bytes)"
                    + (f" — {item['description']}" if item.get("description") else "")
                    for item in files[:30]
                ]
                parts.append("### Workspace files\n" + "\n".join(lines))
                tools.append("search_workspace_files")

    evidence = "\n\n".join(parts)
    limit = int(settings.swarm_max_evidence_chars)
    if len(evidence) > limit:
        evidence = evidence[: max(0, limit - 1)].rstrip() + "…"
    return evidence, tools, warnings


# ── Pipeline ───────────────────────────────────────────────────────────────


def run_swarm_task(task_id: str, query: str, user_id: str) -> str:
    """Run Planner -> Researcher -> Synthesizer and return the final report.

    Raises :class:`SwarmError` on any step failure; the orchestrator turns
    that into the single-agent fallback.
    """
    started = time.monotonic()
    state = SwarmState(query=query, user_id=str(user_id))

    planner, researcher, synthesizer = DEFAULT_SWARM
    agent_events.emit(
        task_id, "thinking", agent=planner.role,
        message=f"{planner.name} is thinking…",
    )
    plan_text = _agent_completion(str(user_id), PLANNER_PROMPT, query)
    state.plan = _parse_plan(plan_text)
    if not state.plan:
        raise SwarmError("Planner returned no usable steps")
    agent_events.emit(
        task_id, "plan", agent=planner.role,
        message="Plan ready",
        output="\n".join(f"{i}. {s}" for i, s in enumerate(state.plan, 1)),
    )

    if _remaining_budget(started) <= 0:
        raise SwarmError("Swarm time budget exhausted before research")

    agent_events.emit(
        task_id, "thinking", agent=researcher.role,
        message=f"{researcher.name} is gathering data…",
    )
    evidence, tools, tool_warnings = _gather_evidence(task_id, query)
    state.tools_used.extend(tools)
    state.warnings.extend(tool_warnings)
    for warning in tool_warnings:
        agent_events.emit(task_id, "warning", agent=researcher.role, message=warning)

    research_prompt = (
        f"User request:\n{query}\n\n{state.transcript(limit=int(settings.swarm_max_evidence_chars))}"
        f"\n\nTool evidence:\n{evidence or '_No tool evidence was available._'}"
    )
    findings = _agent_completion(str(user_id), RESEARCHER_PROMPT, research_prompt)
    if not findings:
        raise SwarmError("Researcher returned no findings")
    state.findings.append(findings)
    agent_events.emit(task_id, "output", agent=researcher.role, output=findings)

    if _remaining_budget(started) <= 0:
        raise SwarmError("Swarm time budget exhausted before synthesis")

    agent_events.emit(
        task_id, "thinking", agent=synthesizer.role,
        message=f"{synthesizer.name} is writing the final report…",
    )
    agent_events.emit(
        task_id, "synthesis", agent=synthesizer.role,
        message="Synthesizing the swarm report…",
    )
    synthesis_prompt = (
        f"User request:\n{query}\n\n{state.transcript()}\n\n"
        f"Tools used: {', '.join(state.tools_used) or 'none'}\n"
        f"Warnings: {'; '.join(state.warnings) or 'none'}"
    )
    final = _agent_completion(str(user_id), SYNTHESIZER_PROMPT, synthesis_prompt)
    if not final:
        raise SwarmError("Synthesizer returned an empty report")
    state.final = final
    agent_events.emit(task_id, "output", agent=synthesizer.role, output=final)
    logger.info(
        "swarm completed (steps=%d, tools=%s)",
        len(state.plan),
        ",".join(state.tools_used) or "none",
    )
    return final
