"""F6 Capability 2 — self-evaluation / confidence scoring service.

The ``self_evaluate`` LangGraph node asks the LLM how confident it is in the
answer it just produced. The LLM returns a JSON object:

    { "confidence": <int 0-100>, "reason": "<short why>" }

Confidence is advisory (never blocks the answer) and is stored on the
assistant Message row. Parsing is defensive: any malformed output falls back
to a null confidence so evaluation never breaks the chat.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from app.services.llm_providers import get_provider

logger = logging.getLogger(__name__)

EVALUATION_SYSTEM_PROMPT = (
    "You evaluate an AI assistant's answer for factual confidence. Return ONLY a "
    'JSON object with "confidence" (an integer 0-100, where 100 means fully '
    'confident the answer is accurate and grounded) and "reason" (a short one-line '
    "explanation). Do not emit anything outside the JSON."
)

_MAX_CONFIDENCE = 100
_MIN_CONFIDENCE = 0


def parse_evaluation_response(text: Optional[str]) -> Dict[str, Any]:
    """Parse the LLM's evaluation output into a confidence dict.

    Returns ``{"confidence": <int|None>, "reason": "<str>"}``. Confidence is
    clamped to 0-100 and nulled if it is not an integer. Never raises.
    """
    if not text:
        return {"confidence": None, "reason": ""}

    stripped = (text or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Evaluation output was not valid JSON; ignoring score")
        return {"confidence": None, "reason": ""}

    if not isinstance(parsed, dict):
        return {"confidence": None, "reason": ""}

    confidence = parsed.get("confidence")
    reason = parsed.get("reason") or ""

    if not isinstance(confidence, int) or isinstance(confidence, bool):
        return {"confidence": None, "reason": reason}

    confidence = max(_MIN_CONFIDENCE, min(_MAX_CONFIDENCE, confidence))
    return {"confidence": confidence, "reason": reason}


def evaluate_response(
    response: str,
    query: Optional[str] = None,
    messages: Optional[list] = None,
    provider_config: Optional[dict] = None,
    model_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Ask the LLM to self-assess the given response (never raises)."""
    history = list(messages or [])
    history = history + [
        {"role": "user", "content": query or "Evaluate this"},
        {"role": "assistant", "content": response},
    ]
    prompt = system_prompt or EVALUATION_SYSTEM_PROMPT
    try:
        provider = get_provider(config=provider_config)
        raw = provider.generate_json_response(
            messages=history,
            system_prompt=prompt,
            model_name=model_name,
        )
    except Exception as exc:  # noqa: BLE001 — evaluation must never break chat
        logger.warning("Self-evaluation failed (non-fatal): %s", exc)
        return {"confidence": None, "reason": ""}
    return parse_evaluation_response(raw)