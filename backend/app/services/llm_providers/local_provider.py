"""
Local mock LLM provider for tests and offline development.

Returns deterministic responses without any network calls, so the full
chat pipeline (LangGraph workflow, streaming service, memory extraction,
model discovery) can run in CI or a test container without Ollama or any
cloud provider being reachable.

Enable with:
    LLM_PROVIDER=local

Behavior:
    - generate_response      → deterministic echo-style text
    - generate_json_response → valid JSON ({'should_save': False}) so
      memory extraction parses cleanly and never saves anything
    - generate_stream_async  → yields a few tokens then done
    - fetch_available_chat_models → a fixed list containing the models
      used by the test-suite (llama3.2:3b, llama3.2:1b, mistral:7b)
    - health_check           → always True (no external service needed)
"""

import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.config import settings
from app.services.llm_providers.base import LLMProvider

# Deterministic model catalog used for discovery/validation in local mode.
# Includes every model name the integration tests select or reject.
LOCAL_CHAT_MODELS: List[str] = [
    "llama3.2:3b",
    "llama3.2:1b",
    "mistral:7b",
    "qwen2.5:7b",
]

# Response returned by generate_response.
LOCAL_RESPONSE_TEXT = (
    "This is a mocked response from the local provider. "
    "LLM_PROVIDER=local is active, so no external model is called."
)

# JSON returned by generate_json_response. Memory extraction expects a
# {"should_save": ...} object; should_save=False keeps the pipeline clean.
LOCAL_RESPONSE_JSON = '{"should_save": false}'


class LocalProvider(LLMProvider):
    """Deterministic in-process provider used for tests and offline dev."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        # api_key/model are accepted (and ignored) so the provider factory
        # can build this provider from any config dict without branching.
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "Local"

    @property
    def default_model(self) -> str:
        return self._model or LOCAL_CHAT_MODELS[0]

    # ── Synchronous generation ─────────────────────────────

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        return LOCAL_RESPONSE_TEXT

    def generate_json_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        return LOCAL_RESPONSE_JSON

    # ── Async streaming ────────────────────────────────────

    async def generate_stream_async(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        # Simulate a realistic SSE stream: start tokens, then done.
        for token in ("This ", "is ", "a ", "local ", "mock ", "response."):
            yield {"type": "token", "token": token}
            # Yield control so cancellation checks run promptly.
            await asyncio.sleep(0)
        yield {"type": "done", "response": "This is a local mock response."}

    # ── Model discovery ────────────────────────────────────

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Return the fixed local catalog — never touches the network."""
        return list(LOCAL_CHAT_MODELS)

    # ── Health ────────────────────────────────

    def health_check(self) -> bool:
        return True
