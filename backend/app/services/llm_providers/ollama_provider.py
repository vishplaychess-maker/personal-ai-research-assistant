"""
Ollama LLM provider.

Wraps the existing ollama_client.py functions to implement the LLMProvider
interface. This preserves all existing Ollama behavior while enabling
provider-agnostic code elsewhere.
"""

from typing import Any, AsyncGenerator, Dict, List, Optional

from app.config import settings
from app.services.llm_providers.base import LLMProvider


class OllamaProvider(LLMProvider):
    """Provider for local Ollama instances."""

    def __init__(self, model: Optional[str] = None):
        self._model = model

    @property
    def name(self) -> str:
        return "Ollama"

    @property
    def default_model(self) -> str:
        return self._model or settings.default_model

    def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        # Lazy import to avoid circular imports at module level
        from app.services.ollama_client import (
            generate_response as _ollama_generate,
        )

        return _ollama_generate(
            messages=messages,
            system_prompt=system_prompt,
            model_name=model_name or self._model,
        )

    def generate_json_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        from app.services.ollama_client import (
            generate_json_response as _ollama_generate_json,
        )

        return _ollama_generate_json(
            messages=messages,
            system_prompt=system_prompt,
            model_name=model_name or self._model,
        )

    async def generate_stream_async(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        from app.services.ollama_client import (
            generate_stream_async as _ollama_stream,
        )

        async for chunk in _ollama_stream(
            messages=messages,
            system_prompt=system_prompt,
            model_name=model_name or self._model,
        ):
            yield chunk

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        from app.services.ollama_client import (
            fetch_available_chat_models as _ollama_models,
        )

        return _ollama_models()

    def health_check(self) -> bool:
        from app.services.ollama_client import (
            check_ollama_health as _ollama_health,
        )

        return _ollama_health()
