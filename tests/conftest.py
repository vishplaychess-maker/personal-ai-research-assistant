"""Shared pytest fixtures and session-scoped setup for integration tests.

Pre-warms the Ollama model once per test session to avoid cold-start
timeouts during streaming tests. The warmup sends a minimal request to
load the model into memory; the response is discarded.
"""

import logging

import httpx
import pytest

logger = logging.getLogger(__name__)

OLLAMA_WARMUP_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"


def warmup_ollama() -> None:
    """Send a minimal generation request to load the Ollama model into memory.

    This is a best-effort warmup. If Ollama is unavailable, the model will
    be loaded on first actual request (cold start), which is slower but
    functionally correct.
    """
    try:
        with httpx.Client(timeout=60.0) as client:
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": "Hello",
                "stream": False,
                "options": {"num_predict": 1},
            }
            resp = client.post(OLLAMA_WARMUP_URL, json=payload)
            if resp.status_code == 200:
                logger.info("Ollama model %s pre-warmed successfully", OLLAMA_MODEL)
            else:
                logger.warning(
                    "Ollama warmup responded with HTTP %s: %.100s",
                    resp.status_code, resp.text,
                )
    except Exception as exc:
        logger.warning(
            "Ollama warmup failed (model will load on first request): %s", exc,
        )


@pytest.fixture(scope="session", autouse=True)
def _ollama_warmup() -> None:
    """Session-scoped autouse fixture: pre-warms Ollama once per test run."""
    warmup_ollama()
