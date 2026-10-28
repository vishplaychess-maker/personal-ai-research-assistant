"""Durable Deep Research — synthesis input assembly (Phase 1B.2R MP-3C3D4C).

Builds the provider-ready :class:`chat_stream.ChatInput` for a deep-research
run's synthesis step from EXCLUSIVELY immutable sealed state: the original
submission (D2 local RAG/memory context, via the SAME
``chat_stream.build_chat_input`` every chat run already uses, UNCHANGED) plus
the D4B structured web-source snapshot (sealed by the acquisition step).
Nothing here calls a network/DB-retrieval service — both context sources are
already-sealed bytes being re-opened, never re-selected.
"""

from __future__ import annotations

import dataclasses
from typing import Dict

from sqlalchemy.orm import Session

from app.models.models import Run, RunStep
from app.run_engine import chat_stream
from app.run_engine.deep_research_citations import build_web_marker_map, render_web_source_block


def build_deep_research_input(
    db: Session, run: Run, submission_step: RunStep, research_snapshot: dict,
) -> tuple[chat_stream.ChatInput, Dict[str, dict]]:
    """Returns ``(chat_input, web_marker_map)``. ``chat_input`` already has
    the D2 local RAG/memory envelope spliced in (via
    ``chat_stream.build_chat_input`, entirely unmodified) — this function
    ADDITIONALLY splices the web-research envelope as one more untrusted
    user-role message immediately before the final (current-turn) user
    message, exactly mirroring how ``build_chat_input`` itself splices the
    D2 context envelope. ``web_marker_map`` is returned so the caller can
    validate/build citations from the SAME map after synthesis completes."""
    base = chat_stream.build_chat_input(db, run, submission_step)
    marker_map = build_web_marker_map(research_snapshot)
    web_block = render_web_source_block(research_snapshot, marker_map)
    if not web_block:
        return base, marker_map

    messages = list(base.messages)
    messages.insert(len(messages) - 1, {"role": "user", "content": web_block})
    return dataclasses.replace(base, messages=messages), marker_map
