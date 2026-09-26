"""YouTube transcript grounding - the smallest INDEPENDENT functional slice.

Turns a YouTube URL into a BOUNDED, TIMESTAMPED transcript context that a chat
answer can be grounded in. Reuses the existing URL helpers in
`app.tools.youtube_summarizer`; adds no second regex and no second grammar.

Guarantees:
  * the video ID is validated/extracted BEFORE any fetch;
  * an unavailable transcript is a CLEAR status, never an empty string;
  * content is never invented - the context says the contents are unknown;
  * this module performs NO writes of any kind: it never touches long-term
    memory or the knowledge graph (it has no DB session at all).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

from app.tools.youtube_summarizer import extract_video_id, is_youtube_url

STATUS_OK = "ok"
STATUS_INVALID_URL = "invalid_url"
STATUS_NO_TRANSCRIPT = "no_transcript"
STATUS_FETCH_FAILED = "fetch_failed"

MAX_CONTEXT_CHARS = 4000

TRUNCATION_MARKER = "[... transcript truncated ...]"

NO_TRANSCRIPT_NOTICE = ("No transcript is available for this video, so its "
                        "contents are unknown. Do not guess or invent content.")
FETCH_FAILED_NOTICE = ("The transcript could not be retrieved, so the video "
                       "contents are unknown. Do not guess or invent content.")
INVALID_URL_NOTICE = "No transcript was used: no valid YouTube URL was found."


@dataclass(frozen=True)
class TranscriptSegment:
    start_s: float
    text: str


@dataclass(frozen=True)
class TranscriptResult:
    status: str
    video_id: Optional[str] = None
    segments: Tuple[TranscriptSegment, ...] = ()
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def _default_fetch(video_id: str) -> Sequence:
    """Fetch through whatever API surface is ACTUALLY installed.

    youtube-transcript-api 1.x REMOVED the legacy
    YouTubeTranscriptApi.get_transcript classmethod (the surface is now
    fetch/list on an INSTANCE); pins below 1.0 expose only the classmethod.
    Calling the classmethod unconditionally - which is what the older
    youtube_summarizer.fetch_transcript still does - raises AttributeError on
    1.2.4, so the live path silently degraded to fetch_failed for every real
    URL. Verified live against 1.2.4.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    legacy = getattr(YouTubeTranscriptApi, "get_transcript", None)
    if callable(legacy):                      # < 1.0
        return legacy(video_id)
    return YouTubeTranscriptApi().fetch(video_id)   # >= 1.0, instance API


def load_transcript(url: str, *, fetch: Optional[Callable] = None,
                    max_chars: int = MAX_CONTEXT_CHARS) -> TranscriptResult:
    """Validate the URL, fetch the transcript, and classify the outcome.

    `fetch` is injected so this is testable without network access. The default
    uses the installed library. A failure carries only the exception TYPE name -
    never its message, which may quote upstream text.
    """
    if not url or not is_youtube_url(url):
        return TranscriptResult(STATUS_INVALID_URL, reason="not_a_youtube_url")
    video_id = extract_video_id(url)
    if not video_id:
        return TranscriptResult(STATUS_INVALID_URL, reason="no_video_id")
    try:
        raw = (fetch or _default_fetch)(video_id)
    except Exception as exc:  # noqa: BLE001 - bounded reason, never the message
        return TranscriptResult(STATUS_FETCH_FAILED, video_id, (),
                                type(exc).__name__)
    segments = []
    for item in raw or ():
        # Two shapes: the legacy API yields dicts, the 1.x API yields
        # FetchedTranscriptSnippet objects with .text/.start. Accept BOTH so
        # the classifier never depends on which pin is installed.
        if isinstance(item, dict):
            raw_text, raw_start = item.get("text"), item.get("start")
        else:
            raw_text = getattr(item, "text", None)
            raw_start = getattr(item, "start", None)
        text = (raw_text or "").strip()
        if not text:
            continue
        try:
            start = float(raw_start or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        segments.append(TranscriptSegment(start, text))
    if not segments:
        return TranscriptResult(STATUS_NO_TRANSCRIPT, video_id, (),
                                "empty_transcript")
    return TranscriptResult(STATUS_OK, video_id, tuple(segments), None)


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return "%02d:%02d" % (total // 60, total % 60)


# -- message-level resolution: ONE supported URL per Run, bounded ------

MULTIPLE_URLS_NOTICE = (
    "More than one YouTube link was found in this message. Only the FIRST "
    "link was read; ask about the others in a separate message."
)

# Stripped from each whitespace token before the EXISTING predicate runs, so
# "watch?v=...)" or a markdown "[label](url)" still matches.
_SURROUNDING = "\"'<>()[]{},.;:!?*"


def find_supported_urls(message: str) -> Tuple[str, ...]:
    """Every DISTINCT supported YouTube URL in *message*, in first-seen order.

    Whitespace tokens are tested with the EXISTING is_youtube_url predicate -
    no second URL grammar, regex, or host list is introduced here."""
    if not message:
        return ()
    seen: dict = {}
    for token in message.split():
        candidate = token.strip(_SURROUNDING)
        if not is_youtube_url(candidate):
            continue
        video_id = extract_video_id(candidate)
        if video_id and video_id not in seen:
            seen[video_id] = candidate
    return tuple(seen.values())


@dataclass(frozen=True)
class MessageTranscript:
    """The bounded transcript context for ONE user message."""
    status: str
    url: str
    video_id: Optional[str]
    urls_found: int
    text: str          # never empty when this object exists


def resolve_message_transcript(message: str, *,
                               fetch: Optional[Callable] = None,
                               max_chars: int = MAX_CONTEXT_CHARS) -> Optional[MessageTranscript]:
    """Resolve the ONE supported YouTube URL in *message* into a bounded,
    timestamped context block - or None when the message contains no
    supported YouTube URL at all (a plain chat turn: nothing is fetched,
    nothing is injected, and no failure is reported).

    Deliberately bounded behavior:
      * the video ID is validated BEFORE any fetch (find_supported_urls
        filters to supported URLs, and load_transcript re-validates the ID
        before calling the fetcher), so a non-URL message never hits the
        network and there is no fetch-then-discard;
      * AT MOST ONE url is fetched - the first - even when several are pasted;
      * an unusable transcript yields an explicit "contents unknown, do not
        guess" block plus a machine-readable status - never empty text,
        never invented content;
      * text stays within max_chars with an explicit truncation marker.
    """
    urls = find_supported_urls(message)
    if not urls:
        return None
    url = urls[0]
    result = load_transcript(url, fetch=fetch, max_chars=max_chars)
    text = build_grounded_context(result, max_chars=max_chars)
    if len(urls) > 1:
        text = MULTIPLE_URLS_NOTICE + chr(10) + chr(10) + text
    cap = max_chars + len(MULTIPLE_URLS_NOTICE) + 2
    return MessageTranscript(status=result.status, url=url,
                             video_id=result.video_id, urls_found=len(urls),
                             text=text[:cap])


def build_grounded_context(result: TranscriptResult, *_,
                           max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """A bounded context block with [mm:ss] markers, or a clear unknown notice."""
    if result.status == STATUS_INVALID_URL:
        return INVALID_URL_NOTICE
    if result.status == STATUS_NO_TRANSCRIPT:
        return NO_TRANSCRIPT_NOTICE
    if result.status == STATUS_FETCH_FAILED:
        return FETCH_FAILED_NOTICE
    header = ("Below is the AVAILABLE TRANSCRIPT of the video. Ground every "
              "claim in it, cite the [mm:ss] timestamps, and never state "
              "anything the transcript does not contain.")
    # max_chars bounds the WHOLE returned block - header and truncation marker
    # included - so the caller can rely on it as a hard cap. The marker gets
    # its room reserved up front, and the final slice is belt-and-braces.
    budget = max_chars - len(header) - 2
    if budget < len(TRUNCATION_MARKER):
        # The cap is too small to hold even the header plus a marker. Emit an
        # explicit, bounded marker - never a silently empty block, and never a
        # block that quietly overruns the cap the caller asked for.
        short = "[truncated]"
        return short if max_chars >= len(short) else short[:max(0, max_chars)]
    reserve = len(TRUNCATION_MARKER) + 1
    lines = []
    used = 0
    truncated = False
    for seg in result.segments:
        line = "[%s] %s" % (format_timestamp(seg.start_s), seg.text)
        step = len(line) + (1 if lines else 0)
        if used + step > budget - reserve:
            truncated = True
            break
        lines.append(line)
        used += step
    if truncated:
        lines.append(TRUNCATION_MARKER)
    return (header + chr(10) + chr(10) + chr(10).join(lines))[:max_chars]
