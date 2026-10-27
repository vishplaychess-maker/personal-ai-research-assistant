"""Gemini LLM provider — adapter over app.services.gemini_service.

Bridges the native-SDK Gemini service into the provider-agnostic LLMProvider
interface, so BOTH chat paths (legacy streaming_service and durable
chat_stream) use Gemini automatically when llm_provider == "gemini".

The Gemini SDK integration is non-streaming: the full completion is emitted as a
single token chunk, followed by the terminal done chunk. A bounded error chunk
is emitted on failure (never API text), which the callers already treat as a
fail-closed provider error.
"""

from typing import Any, AsyncGenerator, Dict, List, Optional

from app.config import settings
from app.services.setup_service import resolve_gemini_api_key
from app.services.llm_providers.base import LLMProvider

#: Curated fallback list (no network) for model discovery.
_CURATED_MODELS = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"]


class GeminiProvider(LLMProvider):
    """Provider for the Gemini API (native google-generativeai SDK)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "Gemini"

    @property
    def default_model(self) -> str:
        return self._model or settings.gemini_model

    # ── helpers ────────────────────────────────────────────

    @staticmethod
    def _with_system(messages: List[Dict[str, Any]],
                     system_prompt: Optional[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if system_prompt:
            out.append({"role": "system", "content": system_prompt})
        out.extend(messages or [])
        return out

    def _service(self, model_name: Optional[str] = None):
        from app.services.gemini_service import GeminiService
        return GeminiService(api_key=self._api_key, model=model_name or self._model)

    # ── generation ─────────────────────────────────────────

    def generate_response(self, messages, system_prompt=None, model_name=None) -> str:
        return self._service(model_name).chat_completion(
            self._with_system(messages, system_prompt))

    def generate_json_response(self, messages, system_prompt=None, model_name=None) -> str:
        json_system = (
            (system_prompt + "\n\n") if system_prompt else ""
        ) + "You MUST respond with valid JSON only. No markdown, no explanation."
        return self._service(model_name).chat_completion(
            self._with_system(messages, json_system))

    async def generate_stream_async(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        from app.services.gemini_service import GeminiAPIError

        try:
            text = self.generate_response(
                messages, system_prompt=system_prompt, model_name=model_name)
        except GeminiAPIError as exc:
            yield {"type": "error", "error": exc.code or "gemini_error"}
            return
        except Exception:  # noqa: BLE001 - bounded, never leak API text
            yield {"type": "error", "error": "gemini_error"}
            return
        if text:
            yield {"type": "token", "token": text}
        yield {"type": "done", "response": text}

    # ── discovery / health ─────────────────────────────────

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        models = list(_CURATED_MODELS)
        configured = self.default_model
        if configured and configured not in models:
            models.insert(0, configured)
        return models

    def health_check(self) -> bool:
        return bool(self._api_key or resolve_gemini_api_key())


__all__ = ["GeminiProvider"]
