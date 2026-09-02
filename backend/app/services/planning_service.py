"""F6 Capability 1 — plan generation service.

The ``generate_plan`` LangGraph node asks the LLM for a step-by-step plan
before answering. A plan is a list of dicts, each with:

    { "step": int, "action": str, "target": str, "reason": str }

A simple question (no tools / no research needed) yields an empty list,
which the workflow treats as "answer directly". Parsing is defensive: any
malformed or non-JSON output falls back to an empty plan so a plan prompt
can never break the chat.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.services.llm_providers import get_provider

logger = logging.getLogger(__name__)

# A step is only accepted if it carries all of these string-able fields.
_REQUIRED_FIELDS = ("step", "action", "target", "reason")

_PLAN_SYSTEM_PROMPT = (
    "You are a planning assistant. Decide whether the user's request needs "
    "a multi-step plan before answering. If it does, return ONLY a JSON array "
    'of step objects, each with keys "step" (integer), "action" (verb like '
    '"search", "retrieve", "summarize", "compute"), "target" (what to act on), '
    'and "reason" (why this step). If the request is simple and needs no plan, '
    'return an empty JSON array: []. Do not emit anything outside the JSON.'
)


def parse_plan_response(text: Optional[str]) -> List[Dict[str, Any]]:
    """Parse the LLM's plan output into a list of step dicts.

    Returns an empty list for any input that is not a JSON array of complete
    step objects, so callers never see a parsing exception.
    """
    if not text:
        return []

    stripped = (text or "").strip()
    # Tolerate a ```json ... ``` markdown fence around the payload.
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Plan output was not valid JSON; ignoring plan")
        return []

    if not isinstance(parsed, list):
        return []

    plan: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        # A step is only usable if every required field is present.
        if not all(k in item for k in _REQUIRED_FIELDS):
            continue
        plan.append({
            "step": item.get("step"),
            "action": item.get("action"),
            "target": item.get("target"),
            "reason": item.get("reason"),
        })
    return plan


def generate_plan_for_query(
    query: str,
    messages: Optional[List[Dict[str, str]]] = None,
    provider_config: Optional[dict] = None,
    model_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Ask the LLM for a plan and return the parsed steps (never raises).

    Any failure (provider error, bad output) falls back to an empty plan so
    the chat continues normally.
    """
    history = (messages or []) + [{"role": "user", "content": query}]
    prompt = system_prompt or _PLAN_SYSTEM_PROMPT
    try:
        provider = get_provider(config=provider_config)
        raw = provider.generate_response(
            messages=history,
            system_prompt=prompt,
            model_name=model_name,
        )
    except Exception as exc:  # noqa: BLE001 — plan must never break chat
        logger.warning("Plan generation failed (non-fatal): %s", exc)
        return []
    return parse_plan_response(raw)
