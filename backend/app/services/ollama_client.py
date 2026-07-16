"""
Sync client for calling Ollama's generate API.

Uses httpx.Client (synchronous) since the entire LangGraph workflow
and message route are synchronous. This avoids fragile asyncio.run() patterns.
"""

from typing import List, Dict, Optional

import httpx

from app.config import settings


# ── Configuration ──────────────────────────────────────────

OLLAMA_GENERATE_URL = f"{settings.ollama_url}/api/generate"
OLLAMA_MODEL = "llama3.2:3b"

# Connection timeout for establishing the socket
CONNECT_TIMEOUT = 10.0
# Total timeout for the full generation (3b model should complete within 60s)
GENERATE_TIMEOUT = 120.0


def _build_prompt(messages: List[Dict[str, str]]) -> str:
    """
    Build a prompt string from conversation messages.
    Uses a simple chat format compatible with llama3.2.
    """
    parts: List[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            parts.append(f"System: {content}")
        elif role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
    parts.append("Assistant:")  # prompt the model to complete
    return "\n\n".join(parts)


def check_ollama_health() -> bool:
    """Check if Ollama is reachable."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{settings.ollama_url}/")
            return resp.status_code == 200 and "Ollama" in resp.text
    except Exception:
        return False


def generate_response(
    messages: List[Dict[str, str]],
    system_prompt: Optional[str] = None,
) -> str:
    """
    Call Ollama to generate a response given recent conversation history.

    Args:
        messages: List of dicts with 'role' and 'content' keys.
        system_prompt: Optional system instruction prepended to the prompt.

    Returns:
        The generated text response.

    Raises:
        ConnectionError: If Ollama is unreachable.
        TimeoutError: If the generation times out.
        RuntimeError: If the API returns an error.
    """
    full_messages = list(messages)
    if system_prompt:
        full_messages.insert(0, {"role": "system", "content": system_prompt})

    prompt = _build_prompt(full_messages)

    request_body = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": 2048,
            "temperature": 0.7,
        },
    }

    try:
        with httpx.Client(
            timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
        ) as client:
            resp = client.post(OLLAMA_GENERATE_URL, json=request_body)
    except httpx.ConnectError:
        raise ConnectionError(
            "Cannot connect to Ollama. Make sure Ollama is running on Windows "
            "and llama3.2:3b is installed (`ollama pull llama3.2:3b`)."
        )
    except httpx.TimeoutException:
        raise TimeoutError(
            "Ollama did not respond in time. The model might still be loading "
            "or the prompt was too long."
        )

    if resp.status_code != 200:
        detail = resp.text[:200]
        raise RuntimeError(f"Ollama returned HTTP {resp.status_code}: {detail}")

    data = resp.json()
    return data.get("response", "").strip()


def generate_json_response(
    messages: List[Dict[str, str]],
    system_prompt: Optional[str] = None,
) -> str:
    """
    Call Ollama and request a JSON response.

    Uses a lower temperature and explicit JSON formatting instruction.
    Returns the raw response string (caller should parse as JSON).

    Raises same exceptions as generate_response.
    """
    full_messages = list(messages)
    if system_prompt:
        full_messages.insert(0, {"role": "system", "content": system_prompt})

    prompt = _build_prompt(full_messages)

    request_body = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": 512,
            "temperature": 0.1,  # low temp for structured output
        },
    }

    try:
        with httpx.Client(
            timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
        ) as client:
            resp = client.post(OLLAMA_GENERATE_URL, json=request_body)
    except httpx.ConnectError:
        raise ConnectionError(
            "Cannot connect to Ollama. Make sure Ollama is running on Windows "
            "and llama3.2:3b is installed (`ollama pull llama3.2:3b`)."
        )
    except httpx.TimeoutException:
        raise TimeoutError(
            "Ollama did not respond in time."
        )

    if resp.status_code != 200:
        detail = resp.text[:200]
        raise RuntimeError(f"Ollama returned HTTP {resp.status_code}: {detail}")

    data = resp.json()
    return data.get("response", "").strip()
