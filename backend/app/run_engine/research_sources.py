"""Durable Deep Research — structured source snapshot schema, bounds, and
normalization (Phase 1B.2R MP-3C3D4B).

Pure, DB-free module: schema validation, deterministic normalization
(dedupe + ordering + citation correspondence), marker neutralization for
untrusted retrieved text, and the server-owned progress-code set. No
network, no encryption, no ORM session — every function here is a plain
data transformation, which is what makes it independently unit-testable
without any hermetic fixture.

Persistence (sealing/opening this snapshot as an encrypted ``RunPayload``)
lives in :mod:`app.run_engine.research_snapshot`. Search/fetch adapters live
in :mod:`app.tools.research_search` / :mod:`app.tools.research_fetch`.
Orchestration (wiring adapters + this module's normalization together with
cancellation/deadline/heartbeat seams) lives in
:mod:`app.run_engine.research_acquisition`.

This module does not implement final-answer synthesis, does not touch a
``Message``, does not know about ``RunWorker``, and never executes MCP,
Python, shell, browser-action, memory, directive, or knowledge-graph writes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.run_engine.errors import RunEngineError
from app.skills.loader import _MARKER_OPENS as _REPO_MARKER_OPENS

# ── errors ───────────────────────────────────────────────────


class ResearchSnapshotError(RunEngineError):
    """Base class for structured-source-snapshot errors."""


class ResearchSnapshotInvalid(ResearchSnapshotError):
    """The snapshot is malformed, oversized, or fails a bound/invariant."""


# ── versioning ───────────────────────────────────────────────

RESEARCH_SOURCE_SNAPSHOT_VERSION = 1

# server-owned progress codes (Phase 1B.2R MP-3C3D4B / D4-DECISION-1: no
# separate "research_planning" stage — any lightweight query preparation is
# folded into "research_searching", since nothing in this repository makes
# planning an independently durable operation).
RESEARCH_PROGRESS_CODES = frozenset({
    "research_searching",
    "research_fetching",
    "research_sources_ready",
})

# ── bounds (hardcoded, not deployer-tunable — mirrors chat_stream.py's
# `_CITATION_MAX_COUNT` precedent; deployer-tunable bounds live in
# `config.py`'s `research_*` settings instead) ──────────────────────────

RESEARCH_MAX_SOURCES = 10
RESEARCH_MAX_PASSAGES_PER_PAGE = 3
RESEARCH_MAX_PASSAGE_CHARS = 1000
RESEARCH_MAX_TOTAL_PASSAGE_BYTES = 20_000

# Bounded metadata/text field lengths (defense in depth — a title/domain
# from an untrusted page must never be allowed to grow unbounded).
_MAX_TITLE_CHARS = 300
_MAX_DOMAIN_CHARS = 253  # max valid DNS name length

_REQUIRED_SNAPSHOT_FIELDS = frozenset({
    "research_snapshot_version", "acquired_at", "query_count", "ordering",
    "total_passage_count", "truncated", "sources",
})
_REQUIRED_SOURCE_FIELDS = frozenset({
    "source_id", "url", "title", "domain", "search_rank", "retrieved_at",
    "published_at", "passages",
})
_REQUIRED_PASSAGE_FIELDS = frozenset({"passage_id", "marker", "text", "content_hash"})


# ── marker neutralization (untrusted-input boundary) ────────────────────
#
# `app.run_engine.chat_stream.MarkerScanner` scans MODEL OUTPUT only — it
# has no code path that ever sees retrieved/fetched INPUT text (confirmed
# by inspection: `MarkerScanner.feed()` is fed streaming provider chunks).
# A page whose raw text contains a literal marker-shaped substring (e.g.
# `[MCP_CALL: ...]` quoted verbatim from documentation, or planted by a
# hostile page) would otherwise reach the model UNSCANNED. This function
# neutralizes every known marker opener (reusing the SAME canonical list
# `app.skills.loader._MARKER_OPENS` the output-side scanner/stripper uses —
# no second, drifting definition) by inserting a zero-width space
# immediately after the opening bracket/tag character. This is a minimal,
# reversible-in-spirit encoding: no ordinary text is deleted or altered,
# every character survives, and the substring can no longer exact-match a
# marker opener (case-insensitively) in the model's own output-side scanner
# or in a naive human/model reading of the raw bracket sequence.
_ZWSP = "​"
_MARKER_NEUTRALIZE_PATTERNS = tuple(
    re.compile(re.escape(opener), re.IGNORECASE) for opener in _REPO_MARKER_OPENS
)


def _insert_zwsp_after_first_char(match: re.Match) -> str:
    """Replacement callback (NOT a fixed string): inserts the ZWSP after the
    matched text's own first character, preserving whatever the ORIGINAL
    text's casing actually was. A fixed-string replacement under
    `re.IGNORECASE` would silently rewrite e.g. "[SAVE_DIRECTIVE" to the
    pattern's own lowercase spelling — that would be an unrequired,
    unnoticed alteration of factual page text, not just neutralization."""
    matched = match.group(0)
    return matched[0] + _ZWSP + matched[1:]


def neutralize_markers(text: str) -> str:
    """Break every known reserved-marker opener in *text* so it can never
    exact-match downstream (the output scanner, or a careless re-display),
    without deleting or otherwise altering any other character — including
    preserving the exact original casing of the matched text itself."""
    if not text:
        return text
    for pattern in _MARKER_NEUTRALIZE_PATTERNS:
        text = pattern.sub(_insert_zwsp_after_first_char, text)
    return text


def content_hash(text: str) -> str:
    """SHA-256 of the exact (already normalized/truncated/neutralized) text,
    prefixed to name the algorithm — computed AFTER final normalization so
    the hash always describes exactly what was sealed."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── candidate types (pre-normalization, adapter output) ─────────────────


@dataclass(frozen=True)
class RawPassageCandidate:
    text: str


@dataclass(frozen=True)
class RawSourceCandidate:
    """One successfully fetched page, before normalization/bounding. A page
    that failed to fetch (timeout, rejected content-type, SSRF-blocked,
    etc.) never becomes a candidate at all — failures are surfaced only via
    bounded progress/log codes, never stored in the snapshot itself."""

    url: str                     # already validated + canonicalized by the fetch adapter
    title: Optional[str]
    domain: str
    search_rank: int
    retrieved_at: str            # ISO-8601 UTC, server clock
    published_at: Optional[str]  # only set if the page itself exposed one
    passages: List[RawPassageCandidate] = field(default_factory=list)


# ── normalization (the ONE authoritative function) ──────────────────────


def build_research_snapshot(
    *,
    acquired_at: str,
    query_count: int,
    candidates: List[RawSourceCandidate],
) -> dict:
    """Build the final, versioned, bounded, citation-correspondent snapshot
    from already-fetched, already-validated candidates.

    Invariants enforced here (the authoritative correspondence guarantee):
      * every passage that survives has exactly one citation marker;
      * marker order matches passage order (``[1]`` is always the first
        surviving passage, etc. — assigned AFTER dedup/truncation, never
        before, so a dropped duplicate can never leave a gap);
      * a source with zero surviving passages (e.g. empty/duplicate content)
        contributes no citation and is dropped entirely — an uncited
        passage can never exist because passages ARE the citation unit;
      * content hashes are computed on the exact neutralized/truncated text
        that will be sealed, never on the raw pre-truncation text;
      * a `research_max_sources`/`RESEARCH_MAX_TOTAL_PASSAGE_BYTES` bound
        reached sets `truncated: true` rather than silently exceeding it.

    Deduplication policy (documented, not implicit):
      1. identical canonical URL → second occurrence dropped (the fetch
         adapter already canonicalizes: lowercased host, no fragment, no
         default-port suffix, so a redirect alias that resolves to the same
         canonical URL is already collapsed before this function runs);
      2. identical passage `content_hash` (post-normalization) from
         DIFFERENT URLs → second occurrence dropped — the same paragraph
         quoted verbatim on two pages is cited once, from whichever source
         was encountered first (stable, since candidates preserve search
         rank order);
      3. a URL repeated across queries never reaches this function twice —
         the acquisition orchestrator dedupes result URLs before fetching
         (see `research_acquisition.py`), so this is enforced upstream too,
         not only here — defense in depth, not a single point of failure.
    """
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    sources: list[dict] = []
    total_passage_bytes = 0
    truncated = False
    marker_index = 0

    for src_idx, cand in enumerate(candidates):
        if len(sources) >= RESEARCH_MAX_SOURCES:
            truncated = True
            break
        if cand.url in seen_urls:
            continue
        seen_urls.add(cand.url)

        kept_passages: list[dict] = []
        for p_idx, raw in enumerate(cand.passages):
            if len(kept_passages) >= RESEARCH_MAX_PASSAGES_PER_PAGE:
                truncated = True
                break
            text = neutralize_markers(_normalize_whitespace(raw.text))
            if len(text) > RESEARCH_MAX_PASSAGE_CHARS:
                text = text[:RESEARCH_MAX_PASSAGE_CHARS]
                truncated = True
            if not text:
                continue
            h = content_hash(text)
            if h in seen_hashes:
                continue  # identical passage content from a different URL
            if total_passage_bytes + len(text.encode("utf-8")) > RESEARCH_MAX_TOTAL_PASSAGE_BYTES:
                truncated = True
                break
            seen_hashes.add(h)
            total_passage_bytes += len(text.encode("utf-8"))
            marker_index += 1
            kept_passages.append({
                "passage_id": f"s{src_idx}p{p_idx}",
                "marker": f"[{marker_index}]",
                "text": text,
                "content_hash": h,
            })

        if not kept_passages:
            continue  # a source with nothing citable contributes no citation

        sources.append({
            "source_id": f"s{src_idx}",
            "url": cand.url,
            "title": (cand.title or "")[:_MAX_TITLE_CHARS] or None,
            "domain": cand.domain[:_MAX_DOMAIN_CHARS],
            "search_rank": int(cand.search_rank),
            "retrieved_at": cand.retrieved_at,
            "published_at": cand.published_at,
            "passages": kept_passages,
        })

    snapshot = {
        "research_snapshot_version": RESEARCH_SOURCE_SNAPSHOT_VERSION,
        "acquired_at": acquired_at,
        # bounded COUNT only — never the raw query text (kept out of the
        # snapshot entirely so it can never leak into a ref/event/log via
        # this object; the orchestrator's caller already owns the raw
        # query for its own request-scoped use).
        "query_count": int(query_count),
        "ordering": "search_rank",
        "total_passage_count": sum(len(s["passages"]) for s in sources),
        # `truncated` reflects only a REAL configured bound being reached
        # (source/passage/byte caps) — a page that simply failed to fetch
        # (timeout, rejected content-type, SSRF-blocked) is a normal,
        # expected outcome, not a truncation signal.
        "truncated": bool(truncated),
        "sources": sources,
    }
    validate_research_snapshot(snapshot)
    return snapshot


def _normalize_whitespace(text: str) -> str:
    return "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())


# ── validation (used both when sealing and when re-opening) ─────────────


def validate_research_snapshot(snapshot: object) -> dict:
    """Fail closed on anything malformed, wrong-versioned, or over a bound.
    Returns the same dict on success (never mutates it)."""
    if not isinstance(snapshot, dict):
        raise ResearchSnapshotInvalid("snapshot is not an object")
    if set(snapshot) != _REQUIRED_SNAPSHOT_FIELDS:
        raise ResearchSnapshotInvalid("snapshot has missing or extra fields")
    if (isinstance(snapshot["research_snapshot_version"], bool)
            or snapshot["research_snapshot_version"] != RESEARCH_SOURCE_SNAPSHOT_VERSION):
        raise ResearchSnapshotInvalid(
            f"unsupported research snapshot version {snapshot['research_snapshot_version']!r}"
        )
    if not isinstance(snapshot["acquired_at"], str) or not snapshot["acquired_at"]:
        raise ResearchSnapshotInvalid("acquired_at must be a non-empty string")
    if isinstance(snapshot["query_count"], bool) or not isinstance(snapshot["query_count"], int) or snapshot["query_count"] < 0:
        raise ResearchSnapshotInvalid("query_count must be a non-negative int")
    if snapshot["ordering"] != "search_rank":
        raise ResearchSnapshotInvalid("unsupported ordering")
    if isinstance(snapshot["total_passage_count"], bool) or not isinstance(snapshot["total_passage_count"], int):
        raise ResearchSnapshotInvalid("total_passage_count must be an int")
    if not isinstance(snapshot["truncated"], bool):
        raise ResearchSnapshotInvalid("truncated must be a bool")
    sources = snapshot["sources"]
    if not isinstance(sources, list) or len(sources) > RESEARCH_MAX_SOURCES:
        raise ResearchSnapshotInvalid("sources must be a bounded list")

    seen_source_ids: set[str] = set()
    seen_markers: set[str] = set()
    total_passages = 0
    for src in sources:
        if not isinstance(src, dict) or set(src) != _REQUIRED_SOURCE_FIELDS:
            raise ResearchSnapshotInvalid("malformed source entry")
        if src["source_id"] in seen_source_ids:
            raise ResearchSnapshotInvalid("duplicate source_id")
        seen_source_ids.add(src["source_id"])
        if not isinstance(src["url"], str) or not src["url"]:
            raise ResearchSnapshotInvalid("source url must be a non-empty string")
        if not isinstance(src["domain"], str) or not src["domain"]:
            raise ResearchSnapshotInvalid("source domain must be a non-empty string")
        if isinstance(src["search_rank"], bool) or not isinstance(src["search_rank"], int):
            raise ResearchSnapshotInvalid("search_rank must be an int")
        passages = src["passages"]
        if not isinstance(passages, list) or not passages or len(passages) > RESEARCH_MAX_PASSAGES_PER_PAGE:
            raise ResearchSnapshotInvalid("source must have 1..N bounded passages")
        for p in passages:
            if not isinstance(p, dict) or set(p) != _REQUIRED_PASSAGE_FIELDS:
                raise ResearchSnapshotInvalid("malformed passage entry")
            if p["marker"] in seen_markers:
                raise ResearchSnapshotInvalid("duplicate citation marker")
            seen_markers.add(p["marker"])
            if not isinstance(p["text"], str) or not p["text"]:
                raise ResearchSnapshotInvalid("passage text must be a non-empty string")
            if len(p["text"]) > RESEARCH_MAX_PASSAGE_CHARS:
                raise ResearchSnapshotInvalid("passage text exceeds the bound")
            if not isinstance(p["content_hash"], str) or not p["content_hash"].startswith("sha256:"):
                raise ResearchSnapshotInvalid("content_hash must be a sha256: string")
            if p["content_hash"] != content_hash(p["text"]):
                raise ResearchSnapshotInvalid("content_hash does not match its passage text")
            total_passages += 1
    if total_passages != snapshot["total_passage_count"]:
        raise ResearchSnapshotInvalid("total_passage_count does not match the sources")
    return snapshot


def remove_passages_with_invalid_citations(snapshot: dict, valid_markers: set[str]) -> dict:
    """Drop any passage whose marker is not in *valid_markers* (e.g. a
    citation a later stage flagged as malformed), dropping the whole source
    if none of its passages survive. Never re-numbers surviving markers —
    a marker identifies one immutable passage for the lifetime of the run."""
    sources = []
    total = 0
    for src in snapshot["sources"]:
        kept = [p for p in src["passages"] if p["marker"] in valid_markers]
        if not kept:
            continue
        total += len(kept)
        sources.append({**src, "passages": kept})
    return {**snapshot, "sources": sources, "total_passage_count": total}
