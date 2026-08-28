"""
Abstract base class for LLM providers.

All providers (Ollama, OpenRouter, NVIDIA NIM, Google) implement this interface
so the rest of the application can remain provider-agnostic.

Embedding logic is intentionally excluded — embeddings always use Ollama
regardless of the chat provider.

Multimodal support: Messages can contain text and/or images.
Message format: List[Dict[str, Any]] where each message is:
  - {"role": "user", "content": "text only"}  # traditional
  - {"role": "user", "content": [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]}  # multimodal
"""

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional


class LLMProvider(ABC):
    """Interface that every LLM provider must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name (e.g. 'Ollama', 'OpenRouter')."""

    @property
    @abstractmethod
    def default_model(self) -> str:
        """Default model identifier for this provider."""

    # ── Synchronous generation ─────────────────────────────

    @abstractmethod
    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        """
        Generate a chat response from conversation history.

        Args:
            messages: Conversation history as [{"role": ..., "content": ...}].
            system_prompt: Optional system instruction.
            model_name: Optional model override (provider-specific).

        Returns:
            The generated text response.

        Raises:
            ConnectionError: Provider unreachable.
            TimeoutError: Request timed out.
            RuntimeError: API returned an error.
        """

    @abstractmethod
    def generate_json_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        """
        Generate a response intended to be valid JSON.

        Uses lower temperature for structured output.
        """

    # ── Async streaming ────────────────────────────────────

    @abstractmethod
    async def generate_stream_async(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream tokens as they arrive.

        Yields dicts with keys:
            {"type": "token", "token": "..."}
            {"type": "done", "response": "full text"}
            {"type": "error", "error": "message"}
        """

    # ── Model discovery ────────────────────────────────────

    @abstractmethod
    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """
        Return model names available for chat on this provider.

        Returns None if the provider is unreachable or cannot list models.
        Callers treat None as "cannot verify availability" and fail closed.
        """

    # ── Health ─────────────────────────────────────────────

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the provider is reachable and healthy."""
