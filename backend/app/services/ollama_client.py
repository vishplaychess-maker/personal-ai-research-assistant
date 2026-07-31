"""
Sync client for calling Ollama's generate API.

Uses httpx.Client (synchronous) since the entire LangGraph workflow
and message route are synchronous. This avoids fragile asyncio.run() patterns.

The async streaming function generate_stream_async uses httpx.AsyncClient
and is designed for the SSE streaming endpoint.
"""

import json
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

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
    model_name: Optional[str] = None,
) -> str:
    """
    Call Ollama to generate a response given recent conversation history.

    Args:
        messages: List of dicts with 'role' and 'content' keys.
        system_prompt: Optional system instruction prepended to the prompt.
        model_name: Optional model name (defaults to OLLAMA_MODEL).

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
    model = model_name or OLLAMA_MODEL

    request_body = {
        "model": model,
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
    except httpx.RequestError as exc:
        raise RuntimeError(
            "Ollama request failed. Make sure Ollama is running and "
            "the model is installed."
        ) from exc

    if resp.status_code != 200:
        detail = resp.text[:200]
        raise RuntimeError(f"Ollama returned HTTP {resp.status_code}: {detail}")

    data = resp.json()
    return data.get("response", "").strip()


def generate_json_response(
    messages: List[Dict[str, str]],
    system_prompt: Optional[str] = None,
    model_name: Optional[str] = None,
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
    model = model_name or OLLAMA_MODEL

    request_body = {
        "model": model,
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
    except httpx.RequestError as exc:
        raise RuntimeError(
            "Ollama request failed. Make sure Ollama is running and "
            "the model is installed."
        ) from exc

    if resp.status_code != 200:
        detail = resp.text[:200]
        raise RuntimeError(f"Ollama returned HTTP {resp.status_code}: {detail}")

    data = resp.json()
    return data.get("response", "").strip()


# ── Async streaming (for SSE endpoint) ────────────────────


async def generate_stream_async(
    messages: List[Dict[str, str]],
    system_prompt: Optional[str] = None,
    model_name: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Call Ollama's generate API with streaming and yield tokens as they arrive.

    Uses httpx.AsyncClient for non-blocking HTTP. Yields dicts:

      {"type": "token", "token": "Hello"}
      {"type": "done", "response": "Hello world"}
      {"type": "error", "error": "Cannot connect to Ollama..."}

    Args:
        messages: Conversation history (list of {"role": ..., "content": ...}).
        system_prompt: Optional system instruction prepended to the prompt.
        model_name: Optional model name (defaults to OLLAMA_MODEL).

    Yields:
        Dicts with streaming events as described above.
    """
    full_messages = list(messages)
    if system_prompt:
        full_messages.insert(0, {"role": "system", "content": system_prompt})

    prompt = _build_prompt(full_messages)
    model = model_name or OLLAMA_MODEL

    request_body = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {
            "num_predict": 2048,
            "temperature": 0.7,
        },
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
        ) as client:
            async with client.stream("POST", OLLAMA_GENERATE_URL, json=request_body) as response:
                if response.status_code != 200:
                    detail = await response.aread()
                    detail_text = detail.decode("utf-8", errors="replace")[:200]
                    yield {
                        "type": "error",
                        "error": f"Ollama returned HTTP {response.status_code}: {detail_text}",
                    }
                    return

                full_response = []
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    token = chunk.get("response", "")
                    full_response.append(token)

                    if chunk.get("done"):
                        yield {"type": "done", "response": "".join(full_response)}
                        return

                    yield {"type": "token", "token": token}

    except httpx.ConnectError:
        yield {
            "type": "error",
            "error": "Cannot connect to Ollama. Make sure Ollama is running and "
                     "the model is installed (`ollama pull llama3.2:3b`).",
        }
    except httpx.TimeoutException:
        yield {
            "type": "error",
            "error": "Ollama did not respond in time. The model might still be "
                     "loading or the prompt was too long.",
        }
    except httpx.RequestError as exc:
        yield {
            "type": "error",
            "error": "Ollama request failed. Make sure Ollama is running and "
                     "the model is installed.",
        }
