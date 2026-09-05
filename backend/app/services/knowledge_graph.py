"""Knowledge Graph — extract, store and query entities + relationships.

Storage model
-------------
Two plain SQLite tables (``graph_entities``, ``graph_relations``) are the
source of truth. NetworkX is used only as an in-memory view, rebuilt from
those rows on demand, for traversal queries (ego / subgraph). The graph is
global (not user-scoped) — a shared knowledge base grown from every
conversation, document and research run.

Everything here is best-effort: a failure to extract or persist must never
break chat, document upload or research. Callers wrap nothing; the module
swallows its own errors and logs them.

Public API
----------
* ``add_entity(db, name, type)``            -> entity id (get-or-create)
* ``add_relation(db, src, tgt, relation)``  -> creates / strengthens an edge
* ``search_graph(db, query, radius)``       -> {"nodes": [...], "links": [...]}
* ``get_all_graph(db)``                     -> {"nodes": [...], "links": [...]}
* ``ingest_extraction(db, data)``           -> write an extractor result
* ``ingest_text_async(text, source)``       -> fire-and-forget extract + write
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional

import networkx as nx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.models import GraphEntity, GraphRelation

logger = logging.getLogger(__name__)

# Agent marker: the model emits [KG_QUERY: <term>] to pull a subgraph into
# context. Mirrors the existing [USE_SKILL: ...] / [MCP_CALL: ...] markers.
KG_QUERY_PATTERN = re.compile(r"\[KG_QUERY:\s*([^\]]+)\]", re.IGNORECASE)

# Keep extractor input bounded — the GLM JSON path caps output at 512 tokens,
# so feeding it a whole 50-page report just yields truncated JSON.
# ponytail: flat char cap; chunk + merge if partial extraction proves too lossy.
MAX_EXTRACT_CHARS = 4000

_MAX_NAME_LEN = 200
_MAX_RELATION_LEN = 160


# ── Normalisation ─────────────────────────────────────────


def _norm_name(name: str) -> str:
    """Collapse whitespace, trim, cap length. Names match case-insensitively
    on lookup but we store the first-seen casing."""
    return re.sub(r"\s+", " ", (name or "").strip())[:_MAX_NAME_LEN]


# ── Writes ────────────────────────────────────────────────


def add_entity(db: Session, name: str, type: str = "concept") -> Optional[int]:
    """Get-or-create an entity by (case-insensitive) name. Returns its id,
    or ``None`` when the name is empty."""
    clean = _norm_name(name)
    if not clean:
        return None

    row = (
        db.query(GraphEntity)
        .filter(func.lower(GraphEntity.name) == clean.lower())
        .first()
    )
    if row:
        return row.id

    row = GraphEntity(name=clean, type=(type or "concept").strip()[:60] or "concept")
    db.add(row)
    db.flush()  # populate row.id without committing the whole caller txn
    return row.id


def add_relation(
    db: Session,
    source_name: str,
    target_name: str,
    relation: str,
    weight: float = 1.0,
) -> Optional[int]:
    """Create the edge ``source -[relation]-> target`` (creating either
    endpoint entity as needed), or strengthen it (+weight) if it already
    exists. Returns the relation id, or ``None`` on invalid input."""
    rel = re.sub(r"\s+", " ", (relation or "").strip())[:_MAX_RELATION_LEN]
    src_id = add_entity(db, source_name)
    tgt_id = add_entity(db, target_name)
    if not (src_id and tgt_id and rel) or src_id == tgt_id:
        return None

    edge = (
        db.query(GraphRelation)
        .filter(
            GraphRelation.source_id == src_id,
            GraphRelation.target_id == tgt_id,
            GraphRelation.relation == rel,
        )
        .first()
    )
    if edge:
        edge.weight = (edge.weight or 1.0) + weight
        db.flush()
        return edge.id

    edge = GraphRelation(
        source_id=src_id, target_id=tgt_id, relation=rel, weight=weight
    )
    db.add(edge)
    db.flush()
    return edge.id


def ingest_extraction(db: Session, data: Dict[str, Any]) -> int:
    """Persist an ``{"entities": [...], "relations": [...]}`` dict (the
    entity_extractor output shape). Commits on success. Returns the number
    of relations written/strengthened. Never raises."""
    if not isinstance(data, dict):
        return 0
    try:
        for ent in data.get("entities", []) or []:
            if isinstance(ent, dict) and ent.get("name"):
                add_entity(db, str(ent["name"]), str(ent.get("type", "concept")))

        written = 0
        for rel in data.get("relations", []) or []:
            if not isinstance(rel, dict):
                continue
            if add_relation(
                db,
                str(rel.get("source", "")),
                str(rel.get("target", "")),
                str(rel.get("relation", "related to")),
            ):
                written += 1

        db.commit()
        logger.info(
            "Knowledge graph: ingested %d entities / %d relations",
            len(data.get("entities", []) or []),
            written,
        )
        return written
    except Exception as exc:  # noqa: BLE001 — ingestion must never break callers
        logger.warning("Knowledge graph ingest failed (non-fatal): %s", exc)
        db.rollback()
        return 0


def ingest_text_async(text: str, source: str = "") -> None:
    """Fire-and-forget: extract entities from ``text`` and write them to the
    graph on a daemon thread with its own DB session. Returns immediately so
    it never adds latency to chat / upload / research."""
    if not text or not text.strip():
        return

    def _worker() -> None:
        from app.services.entity_extractor import extract_entities

        db = SessionLocal()
        try:
            data = extract_entities(text[:MAX_EXTRACT_CHARS])
            if data.get("entities") or data.get("relations"):
                ingest_extraction(db, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Knowledge graph background ingest (%s) failed: %s", source, exc
            )
        finally:
            db.close()

    threading.Thread(
        target=_worker, name=f"kg-ingest-{source or 'text'}", daemon=True
    ).start()


# ── Reads ─────────────────────────────────────────────────


def _load_graph(db: Session) -> nx.DiGraph:
    """Build the in-memory NetworkX view from the two tables."""
    g = nx.DiGraph()
    for e in db.query(GraphEntity).all():
        g.add_node(e.id, name=e.name, type=e.type)
    for r in db.query(GraphRelation).all():
        if g.has_node(r.source_id) and g.has_node(r.target_id):
            g.add_edge(
                r.source_id, r.target_id, relation=r.relation, weight=r.weight
            )
    return g


def _dump(g: nx.DiGraph) -> Dict[str, List[Dict[str, Any]]]:
    """NetworkX graph -> the node-link JSON shape react-force-graph-2d wants."""
    nodes = [
        {"id": n, "name": d.get("name", str(n)), "type": d.get("type", "concept")}
        for n, d in g.nodes(data=True)
    ]
    links = [
        {
            "source": u,
            "target": v,
            "relation": d.get("relation", ""),
            "weight": d.get("weight", 1.0),
        }
        for u, v, d in g.edges(data=True)
    ]
    return {"nodes": nodes, "links": links}


def get_all_graph(db: Session) -> Dict[str, List[Dict[str, Any]]]:
    """The whole graph, for the UI visualisation."""
    return _dump(_load_graph(db))


def search_graph(
    db: Session, query: str, radius: int = 1
) -> Dict[str, List[Dict[str, Any]]]:
    """Return the subgraph around every entity whose name contains ``query``
    (case-insensitive substring), expanded ``radius`` hops in either
    direction. Empty query -> empty result."""
    q = (query or "").strip().lower()
    if not q:
        return {"nodes": [], "links": []}

    g = _load_graph(db)
    seeds = [n for n, d in g.nodes(data=True) if q in d.get("name", "").lower()]
    if not seeds:
        return {"nodes": [], "links": []}

    keep: set = set()
    for s in seeds:
        keep |= set(
            nx.ego_graph(g, s, radius=radius, undirected=True).nodes()
        )
    return _dump(g.subgraph(keep))


# ── Agent marker helpers ──────────────────────────────────


def extract_kg_queries(text: str) -> List[str]:
    """All [KG_QUERY: term] terms in ``text`` (deduped, order-preserving)."""
    seen, out = set(), []
    for m in KG_QUERY_PATTERN.finditer(text or ""):
        term = m.group(1).strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def strip_kg_queries(text: str) -> str:
    """Remove [KG_QUERY: ...] markers from user-visible text."""
    return KG_QUERY_PATTERN.sub("", text or "").strip()


def render_subgraph_text(data: Dict[str, List[Dict[str, Any]]], query: str = "") -> str:
    """Human/LLM-readable rendering of a subgraph for prompt injection."""
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    links = data.get("links", [])
    if not links and not nodes:
        return f"[Knowledge graph] No entities found for \"{query}\"."

    lines = [f"[Knowledge graph] Entities/relations related to \"{query}\":"]
    for l in sorted(links, key=lambda x: -x.get("weight", 1.0))[:40]:
        s = nodes.get(l["source"], {}).get("name", l["source"])
        t = nodes.get(l["target"], {}).get("name", l["target"])
        lines.append(f"  - {s} --[{l.get('relation', 'related to')}]--> {t}")
    if not links:
        lines.append("  " + ", ".join(sorted(n["name"] for n in nodes.values())))
    return "\n".join(lines)


def inject_kg_context(db: Session, user_input: str) -> List[str]:
    """For each [KG_QUERY: term] in ``user_input``, return a rendered
    subgraph block. Returns [] when there are no markers or on any error."""
    try:
        blocks = []
        for term in extract_kg_queries(user_input):
            blocks.append(render_subgraph_text(search_graph(db, term), term))
        return blocks
    except Exception as exc:  # noqa: BLE001
        logger.warning("KG context injection failed (non-fatal): %s", exc)
        return []


# ── Self-check ────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover - manual smoke test
    from app.database import init_db

    init_db()
    _db = SessionLocal()
    try:
        assert add_relation(_db, "LangGraph", "Thunder AI", "part of")
        assert add_relation(_db, "LangGraph", "checkpointer", "uses")
        # strengthen existing edge
        rid = add_relation(_db, "LangGraph", "Thunder AI", "part of")
        _db.commit()
        edge = _db.get(GraphRelation, rid)
        assert edge.weight >= 2.0, edge.weight

        full = get_all_graph(_db)
        assert any(n["name"] == "LangGraph" for n in full["nodes"])

        sub = search_graph(_db, "langgraph")
        assert len(sub["links"]) >= 2, sub
        assert extract_kg_queries("hmm [KG_QUERY: LangGraph] please") == ["LangGraph"]
        assert "LangGraph" in render_subgraph_text(sub, "langgraph")
        print("knowledge_graph self-check OK:", full)
    finally:
        _db.close()
