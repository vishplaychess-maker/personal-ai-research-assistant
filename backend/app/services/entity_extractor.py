"""Entity + relationship extraction from free text, via GLM 5.3 Flash.

Returns the shape the knowledge_graph module ingests::

    {"entities":  [{"name": "...", "type": "..."}],
     "relations": [{"source": "...", "target": "...", "relation": "..."}]}

Every failure mode — GLM down, timeout, non-JSON reply, wrong shape —
degrades to an empty result. Extraction is never on a critical path.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from app.services.llm_providers import get_provider

logger = logging.getLogger(__name__)

_EMPTY: Dict[str, list] = {"entities": [], "relations": []}

_PROMPT = (
    "Extract entities and relationships from the following text. Return JSON "
    'in the format: {"entities": [{"name": "...", "type": "..."}], "relations": '
    '[{"source": "...", "target": "...", "relation": "..."}]}. Keep entities '
    "concise and relations descriptive.\n\nTEXT:\n"
)

# Guard against a runaway reply blowing up json.loads / the graph.
_MAX_ENTITIES = 60
_MAX_RELATIONS = 80


def _coerce_json(raw: str) -> Dict[str, Any]:
    """Parse the model reply into a dict, tolerating ```json fences and
    leading/trailing prose."""
    if not raw:
        return {}
    text = raw.strip()
    # strip a ```json ... ``` (or bare ```) fence if present
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last resort: grab the outermost {...}
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            try:
                return json.loads(brace.group(0))
            except json.JSONDecodeError:
                return {}
        return {}


def _clean_list(items: Any, keys: tuple, limit: int) -> list:
    """Keep only dict rows where every key except optional ``type`` is a
    non-empty string. Returns at most ``limit`` rows."""
    out = []
    if not isinstance(items, list):
        return out
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        row = {k: str(it.get(k, "")).strip() for k in keys}
        if all(row[k] for k in keys if k != "type"):
            out.append(row)
    return out


def extract_entities(text: str) -> Dict[str, list]:
    """Extract ``{"entities": [...], "relations": [...]}`` from ``text``.

    Returns the empty graph on any failure — callers can ingest it as-is.
    """
    if not text or not text.strip():
        return dict(_EMPTY)

    try:
        provider = get_provider({"provider": "glm"})
        raw = provider.generate_json_response(
            messages=[{"role": "user", "content": _PROMPT + text}],
            system_prompt=(
                "You are an information extraction engine. Output only the "
                "requested JSON object."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — extraction must never break callers
        logger.warning("Entity extraction (GLM) failed, returning empty: %s", exc)
        return dict(_EMPTY)

    data = _coerce_json(raw)
    if not isinstance(data, dict):
        logger.warning("Entity extraction: reply was not a JSON object, ignoring.")
        return dict(_EMPTY)

    entities = _clean_list(data.get("entities"), ("name", "type"), _MAX_ENTITIES)
    for e in entities:
        e["type"] = e["type"] or "concept"
    relations = _clean_list(
        data.get("relations"), ("source", "target", "relation"), _MAX_RELATIONS
    )
    return {"entities": entities, "relations": relations}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    # Parser checks run offline (no GLM needed).
    assert _coerce_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _coerce_json('note: {"a": 2} trailing') == {"a": 2}
    assert _coerce_json("not json at all") == {}
    sample = {
        "entities": [{"name": "LangGraph", "type": "library"}, {"bad": "row"}],
        "relations": [
            {"source": "LangGraph", "target": "Thunder AI", "relation": "part of"},
            {"source": "", "target": "x", "relation": "y"},
        ],
    }
    ents = _clean_list(sample["entities"], ("name", "type"), 60)
    rels = _clean_list(sample["relations"], ("source", "target", "relation"), 80)
    assert len(ents) == 1 and ents[0]["name"] == "LangGraph", ents
    assert len(rels) == 1 and rels[0]["relation"] == "part of", rels
    print("entity_extractor self-check OK")
