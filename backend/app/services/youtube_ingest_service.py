"""YouTube Knowledge Ingestion — Phase 1 (MVP, backend-only).

When a user pastes a supported YouTube link into a chat message AND
``settings.youtube_ingest_enabled`` is True, this service:

    detect URL -> fetch transcript -> fetch title/channel (oEmbed, no key)
    -> chunk (bounded) -> LLM extracts key facts per chunk
    -> save each fact into the memory store WITH source attribution
    -> dedupe by source_url (same video already ingested -> skip)
    -> return a bounded confirmation for the user

Consent model (deliberate, and NOT a relaxation of MP-4C)
--------------------------------------------------------
A pasted link is an explicit, user-initiated request to ingest that video, so
the ingest WRITES DIRECTLY. That is the same consent basis the pre-existing
explicit ``memory_service.save_memory`` path uses. This service does **not**
touch the model-initiated ``[SAVE_MEMORY]`` / ``[SAVE_DIRECTIVE]`` path, which
remains approval-gated in ``app.run_engine.worker`` — the two paths are
separate authorities and this module never creates, decides, or bypasses an
approval.

Reuse (no second implementation of anything)
-------------------------------------------
* URL detection / video-id extraction: ``app.tools.youtube_summarizer``
  (``is_youtube_url`` / ``extract_video_id``) via
  ``app.tools.youtube_transcript_context.find_supported_urls``.
* Transcript fetch + outcome classification:
  ``app.tools.youtube_transcript_context.load_transcript`` — the ONE fetch
  implementation, which already probes whichever ``youtube-transcript-api``
  surface is installed.
* Fact-level content hash: ``app.run_engine.save_markers.save_content_hash``
  — the ONE stable digest, so a fact already in memory is not stored twice.
* The memory write itself: ``app.services.memory_service.save_memory``.

Privacy (HARD invariant)
------------------------
Transcript text, chunk text, extracted facts and model output are NEVER logged.
Log lines carry only: video id, url, character/segment/chunk counts, sha256
digests (truncated), and save/skip counts. Error paths log ``type(exc).__name__``
only — never ``str(exc)``, which can quote upstream text.

Fail-closed (HARD invariant)
----------------------------
Transcript unavailable / private / any error -> ZERO saves + a bounded notice
carrying a machine-readable reason. Nothing here ever raises into the chat path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.run_engine.save_markers import save_content_hash

logger = logging.getLogger(__name__)

# ── bounded outcome vocabulary (status slugs, never content) ──────────

STATUS_DISABLED = "disabled"
STATUS_NO_URL = "no_url"
STATUS_TRANSCRIPT_UNAVAILABLE = "transcript_unavailable"
STATUS_EXTRACTION_FAILED = "extraction_failed"
STATUS_SAVED = "saved"
STATUS_DUPLICATE = "duplicate"
STATUS_SAVE_FAILED = "save_failed"

#: The ``memories.source_kind`` tag for every row written by this service.
SOURCE_KIND_YOUTUBE = "youtube_knowledge"

#: ``memories.category`` for ingested facts. "fact" is the existing enum member
#: for an objective statement, which is what a transcript-derived fact is.
FACT_CATEGORY = "fact"

#: Hard ceiling on facts REQUESTED from the model per chunk. This bounds one
#: prompt's output; the per-VIDEO cap is ``youtube_ingest_max_facts_per_video``.
MAX_FACTS_PER_CHUNK = 12

#: Hard ceiling on how many transcript SEGMENTS are joined into the transcript
#: text considered for ingestion. Purely a work bound (a 12-hour stream would
#: otherwise build a multi-megabyte string before the char cap applies).
MAX_SEGMENTS = 20000

EXTRACTION_SYSTEM_PROMPT = """You are a knowledge extraction system. You are given ONE excerpt of a video transcript.

Extract only durable, self-contained facts that are EXPLICITLY stated in the excerpt.

Rules:
- Never invent, infer beyond the text, or add outside knowledge.
- Each fact must be a single concise sentence in the third person.
- "quote" must be the exact supporting phrase copied from the excerpt, at most 200 characters.
- Skip filler, greetings, sponsor reads, calls to action, and pure opinion with no factual claim.
- If the excerpt contains no durable fact, return {"facts": []}.

Return ONLY a JSON object with exactly this shape:
{"facts": [{"fact": "...", "quote": "..."}]}

At most %d facts. Prefer fewer, higher-quality facts.""" % MAX_FACTS_PER_CHUNK

# ── bounded user-facing notices (fixed templates, never content) ──────

NO_TRANSCRIPT_NOTICE = ("I could not read that video's transcript, so nothing "
                        "was saved from it.")
FETCH_FAILED_NOTICE = ("I could not retrieve that video's transcript, so nothing "
                       "was saved from it.")
EXTRACTION_FAILED_NOTICE = ("I read the transcript but could not extract any "
                            "facts from it, so nothing was saved.")
NO_FACTS_NOTICE = ("I read the transcript but found no durable facts to save "
                   "from it.")
DUPLICATE_NOTICE_TEMPLATE = "Already saved knowledge from %s - skipped re-ingesting it."
TRUNCATION_NOTICE = ("The transcript was longer than the configured limit and "
                     "was truncated before extraction.")
SAVED_NOTICE_TEMPLATE = "Saved %d facts from %s."

_TRANSCRIPT_UNAVAILABLE = {
    "no_transcript": NO_TRANSCRIPT_NOTICE,
    "fetch_failed": FETCH_FAILED_NOTICE,
}


@dataclass(frozen=True)
class YouTubeIngestResult:
    """The truthful outcome of one ingest attempt. Carries COUNTS and slugs;
    never transcript text, chunk text, or extracted fact content."""

    status: str
    url: Optional[str] = None
    video_id: Optional[str] = None
    source_title: Optional[str] = None
    transcript_status: Optional[str] = None
    chunks: int = 0
    transcript_chars: int = 0
    facts_found: int = 0
    facts_saved: int = 0
    facts_skipped_duplicate: int = 0
    truncated: bool = False
    notice: str = ""

    @property
    def saved(self) -> bool:
        return self.status == STATUS_SAVED and self.facts_saved > 0


@dataclass
class _RawFact:
    content: str
    category: str = FACT_CATEGORY


# ── pure helpers (no I/O - directly unit-testable) ────────────────────


def ingest_enabled() -> bool:
    """True only when the server-owned flag authorizes ingestion."""
    return bool(getattr(settings, "youtube_ingest_enabled", False))


def detect_youtube_url(message: str) -> Optional[Tuple[str, str]]:
    """(url, video_id) for the FIRST supported YouTube URL in *message*.

    Reuses the repository's ONE detector (``find_supported_urls``, built on
    ``is_youtube_url`` / ``extract_video_id``) — no second URL grammar. Returns
    None when the message contains no supported link (a plain chat turn).
    """
    if not message or not isinstance(message, str):
        return None
    from app.tools.youtube_transcript_context import find_supported_urls
    from app.tools.youtube_summarizer import extract_video_id

    urls = find_supported_urls(message)
    if not urls:
        return None
    video_id = extract_video_id(urls[0])
    if not video_id:
        return None
    return urls[0], video_id


def apply_char_limit(text: str, limit: int) -> Tuple[str, bool]:
    """Truncate *text* to at most *limit* characters. Returns (text, truncated)."""
    cap = max(1, int(limit))
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def chunk_text(text: str, size: int) -> List[str]:
    """Split *text* into consecutive chunks of at most *size* characters,
    preferring a whitespace boundary so a word is not cut in half. Returns []
    for empty text, and never more chunks than the text needs."""
    body = (text or "").strip()
    if not body:
        return []
    step = max(1, int(size))
    chunks: List[str] = []
    start = 0
    total = len(body)
    while start < total:
        end = min(start + step, total)
        if end < total:
            window = body[start:end]
            cut = window.rfind(" ")
            # Only honour a boundary that keeps the chunk reasonably full,
            # otherwise a pathological text of tiny words would emit hundreds
            # of near-empty chunks.
            if cut > step // 2:
                end = start + cut
        piece = body[start:end].strip()
        if piece:
            chunks.append(piece)
        if end <= start:            # defensive: never loop forever
            end = start + step
        start = end
    return chunks


def parse_facts(raw: str, *, limit: int) -> List[_RawFact]:
    """Parse one model JSON reply into bounded facts.

    Tolerant about SHAPE (the model may wrap the list, or return a bare list),
    strict about CONTENT: an entry is kept only when it carries a non-empty
    string ``fact``. The ``quote`` is a required field in the prompt but is
    deliberately NOT persisted or logged — it exists only to push the model
    toward grounded extraction. Malformed entries are dropped, never guessed.
    """
    if not raw or not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    items: Any = parsed
    if isinstance(parsed, dict):
        items = parsed.get("facts")
    if not isinstance(items, list):
        return []
    out: List[_RawFact] = []
    seen: set = set()
    for item in items:
        if len(out) >= max(0, int(limit)):
            break
        if isinstance(item, str):
            content = item.strip()
        elif isinstance(item, dict):
            content = str(item.get("fact") or "").strip()
        else:
            continue
        if len(content) < 3:
            continue
        key = " ".join(content.lower().split())
        if key in seen:
            continue
        seen.add(key)
        out.append(_RawFact(content=content))
    return out


def build_notice(status: str, *, source_title: Optional[str], url: str,
                 facts_saved: int, truncated: bool) -> str:
    """The bounded user-facing confirmation/notice for one outcome. Fixed
    templates only; the title is the video's own public title (never transcript
    text), and is replaced by the URL when unavailable."""
    label = source_title or url
    if status == STATUS_DUPLICATE:
        return DUPLICATE_NOTICE_TEMPLATE % label
    if status == STATUS_SAVED:
        text = SAVED_NOTICE_TEMPLATE % (facts_saved, label)
        if truncated:
            text = text + " " + TRUNCATION_NOTICE
        return text
    if status == STATUS_EXTRACTION_FAILED:
        return EXTRACTION_FAILED_NOTICE
    if status == STATUS_TRANSCRIPT_UNAVAILABLE:
        return NO_TRANSCRIPT_NOTICE
    return ""


# ── metadata (best effort, no API key) ────────────────────────────────


def fetch_oembed_metadata(url: str, *,
                          timeout_s: Optional[float] = None) -> Dict[str, Optional[str]]:
    """Title / channel via YouTube oEmbed (no API key).

    BEST EFFORT by design: any failure returns empty metadata rather than
    failing the ingest, because the transcript — not the title — is the payload.
    Logs the exception CLASS NAME only.
    """
    timeout = float(timeout_s if timeout_s is not None else
                    getattr(settings, "youtube_ingest_oembed_timeout_s", 5.0))
    out: Dict[str, Optional[str]] = {"title": None, "channel": None, "language": None}
    try:
        import httpx

        resp = httpx.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.info("youtube_ingest: oEmbed returned status=%s", resp.status_code)
            return out
        data = resp.json()
        title = data.get("title")
        channel = data.get("author_name")
        out["title"] = str(title)[:500] if title else None
        out["channel"] = str(channel)[:200] if channel else None
    except Exception as exc:  # noqa: BLE001 - metadata is optional, never fatal
        logger.info("youtube_ingest: oEmbed lookup failed (%s)", type(exc).__name__)
    return out


# ── the ingest entry point ────────────────────────────────────────────


def _default_generate_json(messages: List[Dict[str, str]],
                           system_prompt: str) -> str:
    from app.services.llm_providers import get_provider

    return get_provider().generate_json_response(
        messages=messages, system_prompt=system_prompt)


def ingest_message_youtube(
    db: DBSession,
    *,
    user_id: int,
    session_id: Optional[int],
    message: str,
    fetch_transcript: Optional[Callable] = None,
    generate_json: Optional[Callable] = None,
    fetch_metadata: Optional[Callable] = None,
) -> Optional[YouTubeIngestResult]:
    """Ingest the FIRST supported YouTube link in *message*, if any.

    Returns ``None`` when there is nothing to do at all — the flag is off, or
    the message contains no supported YouTube URL. A ``None`` return means the
    caller must behave EXACTLY as before this feature (no context line, no
    notice, no side effect).

    Otherwise returns a bounded :class:`YouTubeIngestResult`. Never raises: every
    failure becomes a status + bounded notice so chat always continues.

    Injected seams (all optional, defaulting to the real implementations):
    ``fetch_transcript(video_id)``, ``generate_json(messages, system_prompt)``,
    ``fetch_metadata(url)`` — this is what makes the whole flow testable with no
    network and no model.
    """
    if not ingest_enabled():
        return None
    detected = detect_youtube_url(message)
    if detected is None:
        return None
    url, video_id = detected

    transcript_limit = max(1, int(getattr(settings, "youtube_ingest_max_chars", 120000)))
    chunk_size = max(1, int(getattr(settings, "youtube_ingest_chunk_size", 3000)))
    fact_cap = max(1, int(getattr(settings, "youtube_ingest_max_facts_per_video", 20)))
    gen = generate_json or _default_generate_json
    meta_fn = fetch_metadata or fetch_oembed_metadata

    # 1 - transcript (the ONE fetch implementation, by default)
    result = _load_transcript(url, video_id, fetch_transcript)
    if result is None:
        logger.info("youtube_ingest: transcript fetch failed url=%s video=%s", url, video_id)
        return YouTubeIngestResult(
            status=STATUS_TRANSCRIPT_UNAVAILABLE, url=url, video_id=video_id,
            transcript_status="fetch_failed", notice=FETCH_FAILED_NOTICE)
    if not result.ok:
        logger.info("youtube_ingest: no usable transcript url=%s video=%s status=%s",
                    url, video_id, result.status)
        return YouTubeIngestResult(
            status=STATUS_TRANSCRIPT_UNAVAILABLE, url=url, video_id=video_id,
            transcript_status=result.status,
            notice=_TRANSCRIPT_UNAVAILABLE.get(result.status, NO_TRANSCRIPT_NOTICE))

    segments = tuple(result.segments)[:MAX_SEGMENTS]
    transcript = " ".join(seg.text for seg in segments).strip()
    if not transcript:
        logger.info("youtube_ingest: empty transcript url=%s video=%s", url, video_id)
        return YouTubeIngestResult(
            status=STATUS_TRANSCRIPT_UNAVAILABLE, url=url, video_id=video_id,
            transcript_status="empty", notice=NO_TRANSCRIPT_NOTICE)

    transcript, truncated = apply_char_limit(transcript, transcript_limit)
    chunks = chunk_text(transcript, chunk_size)
    if not chunks:
        return YouTubeIngestResult(
            status=STATUS_TRANSCRIPT_UNAVAILABLE, url=url, video_id=video_id,
            transcript_status="empty", notice=NO_TRANSCRIPT_NOTICE)
    logger.info("youtube_ingest: url=%s video=%s segments=%d chars=%d truncated=%s chunks=%d",
                url, video_id, len(segments), len(transcript), truncated, len(chunks))

    # 2 - source metadata (best effort; never fatal)
    meta: Dict[str, Optional[str]] = {}
    try:
        meta = meta_fn(url) or {}
    except Exception as exc:  # noqa: BLE001 - metadata is optional
        logger.info("youtube_ingest: metadata lookup failed (%s)", type(exc).__name__)
        meta = {}
    source_title = meta.get("title")

    # 3 - per-SOURCE dedupe: this exact video was already ingested
    from app.models.models import Memory

    existing = (db.query(Memory)
                .filter(Memory.user_id == int(user_id),
                        Memory.source_url == url,
                        Memory.source_kind == SOURCE_KIND_YOUTUBE)
                .first())
    if existing is not None:
        logger.info("youtube_ingest: duplicate source url=%s video=%s existing_memory_id=%s",
                    url, video_id, existing.id)
        return YouTubeIngestResult(
            status=STATUS_DUPLICATE, url=url, video_id=video_id,
            source_title=source_title, transcript_status=result.status,
            chunks=len(chunks), transcript_chars=len(transcript), truncated=truncated,
            notice=build_notice(STATUS_DUPLICATE, source_title=source_title,
                                url=url, facts_saved=0, truncated=truncated))

    # 4 - extraction, chunk by chunk, bounded by the per-video fact ceiling
    found: List[_RawFact] = []
    extraction_failed = False
    for index, chunk in enumerate(chunks):
        if len(found) >= fact_cap:
            break
        try:
            raw = gen([{"role": "user", "content": chunk}], EXTRACTION_SYSTEM_PROMPT)
        except Exception as exc:  # noqa: BLE001 - bounded reason, never the message
            logger.warning("youtube_ingest: extraction call failed chunk=%d (%s)",
                           index, type(exc).__name__)
            extraction_failed = True
            continue
        batch = parse_facts(raw, limit=min(MAX_FACTS_PER_CHUNK, fact_cap - len(found)))
        logger.info("youtube_ingest: chunk=%d extracted=%d running_total=%d",
                    index, len(batch), len(found) + len(batch))
        found.extend(batch)

    if not found:
        status = STATUS_EXTRACTION_FAILED if extraction_failed else STATUS_EXTRACTION_FAILED
        notice = EXTRACTION_FAILED_NOTICE if extraction_failed else NO_FACTS_NOTICE
        logger.info("youtube_ingest: no facts extracted url=%s video=%s failed=%s",
                    url, video_id, extraction_failed)
        return YouTubeIngestResult(
            status=status, url=url, video_id=video_id, source_title=source_title,
            transcript_status=result.status, chunks=len(chunks),
            transcript_chars=len(transcript), truncated=truncated, notice=notice)

    # 5 - persist, fact-level dedupe by the ONE shared content hash
    from app.services.memory_service import save_memory

    saved = 0
    skipped = 0
    for fact in found:
        if saved >= fact_cap:
            break
        digest = save_content_hash("memory", {"content": fact.content,
                                              "category": FACT_CATEGORY})
        prior = (db.query(Memory)
                 .filter(Memory.user_id == int(user_id),
                         Memory.content_hash == digest)
                 .first())
        if prior is not None:
            skipped += 1
            logger.info("youtube_ingest: fact already present hash=%s", digest[:12])
            continue
        row = save_memory(
            db, int(user_id), fact.content, FACT_CATEGORY, session_id,
            source_url=url,
            source_title=source_title,
            source_kind=SOURCE_KIND_YOUTUBE,
            source_meta=_source_meta(meta, result.status, len(transcript)),
        )
        if row is None:
            # save_memory refuses empty / sensitive content; bounded, no content.
            logger.info("youtube_ingest: save_memory refused a fact hash=%s", digest[:12])
            skipped += 1
            continue
        saved += 1

    status = STATUS_SAVED if saved > 0 else (
        STATUS_DUPLICATE if skipped > 0 else STATUS_SAVE_FAILED)
    logger.info("youtube_ingest: done url=%s video=%s found=%d saved=%d skipped=%d status=%s",
                url, video_id, len(found), saved, skipped, status)
    return YouTubeIngestResult(
        status=status, url=url, video_id=video_id, source_title=source_title,
        transcript_status=result.status, chunks=len(chunks),
        transcript_chars=len(transcript), facts_found=len(found),
        facts_saved=saved, facts_skipped_duplicate=skipped, truncated=truncated,
        notice=build_notice(status, source_title=source_title, url=url,
                            facts_saved=saved, truncated=truncated))


def _source_meta(meta: Dict[str, Optional[str]], transcript_status: str,
                 chars: int) -> str:
    """Bounded, non-secret provenance JSON. Never transcript or fact content."""
    payload = {
        "channel": meta.get("channel"),
        "language": meta.get("language"),
        "fetched_at": _utcnow_iso(),
        "transcript_status": transcript_status,
        "transcript_chars": int(chars),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _load_transcript(url: str, video_id: str, fetch: Optional[Callable]):
    """Delegate to the ONE transcript loader. Returns None when the loader
    itself blew up (mapped by the caller to fetch_failed)."""
    try:
        from app.tools.youtube_transcript_context import load_transcript

        return load_transcript(url, fetch=fetch)
    except Exception as exc:  # noqa: BLE001 - bounded reason, never the message
        logger.warning("youtube_ingest: transcript loader raised (%s)", type(exc).__name__)
        return None


def context_line(result: YouTubeIngestResult) -> Optional[str]:
    """The bounded system-context line prepended for the SAME turn, so the model
    can reference what was just saved. Returns None when nothing was saved.

    Contains the notice (counts + public title/url) and NEVER transcript text.
    """
    if result is None or not result.saved:
        return None
    return ("[youtube_ingest] " + result.notice +
            " Treat the saved knowledge as background facts about the linked "
            "video; do not claim to have watched it.")


__all__ = [
    "STATUS_DISABLED", "STATUS_NO_URL", "STATUS_TRANSCRIPT_UNAVAILABLE",
    "STATUS_EXTRACTION_FAILED", "STATUS_SAVED", "STATUS_DUPLICATE",
    "STATUS_SAVE_FAILED", "SOURCE_KIND_YOUTUBE", "FACT_CATEGORY",
    "MAX_FACTS_PER_CHUNK", "MAX_SEGMENTS", "EXTRACTION_SYSTEM_PROMPT",
    "NO_TRANSCRIPT_NOTICE", "FETCH_FAILED_NOTICE", "EXTRACTION_FAILED_NOTICE",
    "NO_FACTS_NOTICE", "DUPLICATE_NOTICE_TEMPLATE", "TRUNCATION_NOTICE",
    "SAVED_NOTICE_TEMPLATE", "YouTubeIngestResult", "ingest_enabled",
    "detect_youtube_url", "apply_char_limit", "chunk_text", "parse_facts",
    "build_notice", "fetch_oembed_metadata", "ingest_message_youtube",
    "context_line",
]
