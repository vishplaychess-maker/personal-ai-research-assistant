"""
AI Research Agent — tools package.

Available tools:
  web_scraper         — Playwright-based web page scraper (LangChain tool).
  terminal_executor   — Command execution with human-in-the-loop approval.
  youtube_summarizer  — Fetch a YouTube video transcript (LangChain tool).
  python_sandbox      — Code interpreter with a 15s timeout (LangChain tool).
"""

from app.tools.web_scraper import web_scraper, scrape_url, extract_urls  # noqa: F401
from app.tools.terminal_executor import (  # noqa: F401
    extract_proposed_command,
    run_command,
    format_approval_message,
    format_deny_message,
    format_result_message,
)
from app.tools.youtube_summarizer import (  # noqa: F401
    youtube_summarizer,
    fetch_transcript,
    extract_video_id,
    is_youtube_url,
)
from app.tools.python_sandbox import (  # noqa: F401
    execute_python_code,
    run_python_code,
    extract_python_code,
    format_code_result,
)

__all__ = [
    "web_scraper",
    "scrape_url",
    "extract_urls",
    "extract_proposed_command",
    "run_command",
    "format_approval_message",
    "format_deny_message",
    "format_result_message",
    "youtube_summarizer",
    "fetch_transcript",
    "extract_video_id",
    "is_youtube_url",
    "execute_python_code",
    "run_python_code",
    "extract_python_code",
    "format_code_result",
]
