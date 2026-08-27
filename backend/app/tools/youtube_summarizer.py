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
_YOUTUBE_HOST_RE = re.compile(r"(?:youtube\.com|youtu\.be)", re.IGNORECASE)
_VIDEO_ID_RE = re.compile(
    r"(?:v=|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})",
)


def extract_video_id(url: str) -> Optional[str]:
    """Extract an 11-char YouTube video ID from *url*, or None if not found."""
    if not url:
        return None
    match = _VIDEO_ID_RE.search(url)
    if match:
        return match.group(1)
    return None


def is_youtube_url(url: str) -> bool:
    """Return True if *url* is a YouTube URL (watch/shorts/embed/youtu.be)."""
    if not url:
        return False
    return bool(_YOUTUBE_HOST_RE.search(url)) and extract_video_id(url) is not None


def fetch_transcript(url: str) -> str:
    """Fetch and combine a YouTube video's transcript, truncated to ~4000 chars."""
    video_id = extract_video_id(url)
    if not video_id:
        return "[YouTube Summarizer] Error: could not extract a video ID from '%s'." % url

    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        transcript = YouTubeTranscriptApi.get_transcript(video_id)
    except Exception as exc:
        error_msg = (
            "[YouTube Summarizer] Error fetching transcript for %s: %s: %s"
            % (video_id, type(exc).__name__, exc)
        )
        logger.error(error_msg, exc_info=True)
        return error_msg

    if not transcript:
        return "[YouTube Summarizer] No transcript available for video %s." % video_id

    combined = " ".join(item.get("text", "") for item in transcript).strip()

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
