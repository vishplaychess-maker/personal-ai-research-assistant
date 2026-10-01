"""
YouTube Summarizer Tool for the AI Research Agent.

Fetches the transcript of a YouTube video using `youtube-transcript-api`
and returns the combined text so the LLM can summarize its key points.

This tool is designed to be invoked by the LLM agent when a user provides
a YouTube URL. It can also be called directly.

Usage (as a LangChain-style tool):
    from app.tools.youtube_summarizer import youtube_summarizer

    result = youtube_summarizer.invoke({"url": "https://youtu.be/..."})

Usage (standalone):
    from app.tools.youtube_summarizer import fetch_transcript
    text = fetch_transcript("https://www.youtube.com/watch?v=...")
"""

import logging
import re
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Approximate transcript limit to avoid LLM context overflow (~4000 chars).
MAX_TRANSCRIPT_CHARS = 4_000

# Patterns to extract a YouTube video ID from common URL forms:
#   - https://www.youtube.com/watch?v=VIDEO_ID
#   - https://youtu.be/VIDEO_ID
#   - https://www.youtube.com/shorts/VIDEO_ID
#   - https://www.youtube.com/embed/VIDEO_ID
#   - https://www.youtube.com/live/VIDEO_ID
_YOUTUBE_HOST_RE = re.compile(
    r"(?:https?://|//|\.)(?:[a-z0-9-]+\.)*youtu(?:be\.com|\.be)(?![a-z0-9-])"
    r"(?=[/:?#]|$)",
    re.IGNORECASE)
_VIDEO_ID_RE = re.compile(
    r"(?:\bv=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})",
)


def extract_video_id(url: str) -> Optional[str]:
    """Extract an 11-char YouTube video ID from *url*, or None if not found.

    The id is the LAST occurrence found, so a trailing URL decoration the
    token-stripper could not remove (an unbalanced markdown bracket such as
    "youtu.be/<id>)" whose opening "(" was consumed as part of a link label)
    can never win over the real ``watch?v=`` id earlier in the same string."""
    if not url:
        return None
    matches = _VIDEO_ID_RE.findall(url)
    if matches:
        return matches[-1]
    return None


def is_youtube_url(url: str) -> bool:
    """Return True if *url* is a YouTube URL (watch/shorts/embed/youtu.be)."""
    if not url:
        return False
    return bool(_YOUTUBE_HOST_RE.search(url)) and extract_video_id(url) is not None


def fetch_transcript(url: str) -> str:
    """Fetch and combine a YouTube video's transcript, truncated to ~4000 chars.

    Delegates the ENTIRE fetch to ``app.tools.youtube_transcript_context`` — the
    one implementation: it validates the URL and video ID BEFORE any network
    call, probes whichever ``youtube-transcript-api`` surface is actually
    installed, and classifies the outcome. This function only renders that
    result as the tool's free-text contract, so there is no second fetch.

    The previous direct ``YouTubeTranscriptApi.get_transcript(video_id)`` call
    was dead against the installed 1.x library (the classmethod was removed),
    which made this tool unusable for every real URL; failures now come back as
    a clear, bounded message instead of an upstream exception string.
    """
    video_id = extract_video_id(url or "")
    if not video_id:
        return "[YouTube Summarizer] Error: could not extract a video ID from '%s'." % url

    from app.tools.youtube_transcript_context import (
        STATUS_FETCH_FAILED, STATUS_NO_TRANSCRIPT, STATUS_OK, load_transcript,
    )

    result = load_transcript(url)
    if result.status == STATUS_NO_TRANSCRIPT:
        return ("[YouTube Summarizer] No transcript available for video %s."
                % result.video_id)
    if result.status == STATUS_FETCH_FAILED:
        return ("[YouTube Summarizer] Error fetching transcript for %s: %s"
                % (result.video_id, result.reason))
    if result.status != STATUS_OK:
        return "[YouTube Summarizer] Error: no transcript was used for '%s'." % url

    combined = " ".join(seg.text for seg in result.segments).strip()

    if len(combined) > MAX_TRANSCRIPT_CHARS:
        combined = (
            combined[:MAX_TRANSCRIPT_CHARS]
            + "\n\n[Transcript truncated at %d chars]" % MAX_TRANSCRIPT_CHARS
        )

    logger.info(
        "YouTube summarizer: extracted %d chars for %s", len(combined), video_id
    )
    return combined


@tool
def youtube_summarizer(url: str) -> str:
    """Fetch the transcript of a YouTube video so it can be summarized.

    Use this tool when the user provides a YouTube URL and asks you to
    summarize the video or extract its key points. The tool returns the
    video's transcript text (truncated to ~4000 chars); summarize the key
    points from that text.

    Args:
        url: The full YouTube URL (e.g. "https://www.youtube.com/watch?v=...").

    Returns:
        The transcript text, or an error message.
    """
    if not url or not is_youtube_url(url):
        return (
            "[YouTube Summarizer] Error: invalid YouTube URL '%s'. "
            "Expected a youtube.com or youtu.be link." % url
        )

    try:
        return fetch_transcript(url)
    except Exception as exc:
        error_msg = "[YouTube Summarizer] Error: %s: %s" % (type(exc).__name__, exc)
        logger.error(error_msg, exc_info=True)
        return error_msg
