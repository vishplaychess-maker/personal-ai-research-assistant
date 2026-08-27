"""
Google AI Studio (Gemini) LLM provider.

Uses Google's OpenAI-compatible endpoint for Gemini models.
Base URL: https://generativelanguage.googleapis.com/v1beta/openai/
Docs:     https://ai.google.dev/gemini-api/docs/openai
"""

import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.config import settings
from app.services.llm_providers.base import LLMProvider

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15.0
GENERATE_TIMEOUT = 120.0

# Fallback list of popular Gemini chat models if the /models call fails/empty.
_POPULAR_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]

_NON_CHAT_MARKERS = ("embed", "rerank", "aqa")


class GoogleProvider(LLMProvider):
    """Provider for Google Gemini (OpenAI-compatible API)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "Google AI"

    @property
    def default_model(self) -> str:
        return self._model or settings.google_model

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key or settings.google_api_key}",
            "Content-Type": "application/json",
        }

    def _build_messages(self, messages, system_prompt=None):
        result = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        result.extend(messages)
        return result

    def _resolve_model(self, model_name: Optional[str] = None) -> str:
        return model_name or self._model or settings.google_model

    def generate_response(self, messages, system_prompt=None, model_name=None):
        if not (self._api_key or settings.google_api_key):
            raise RuntimeError("Google AI API key not configured. Set GOOGLE_API_KEY in your .env file.")
        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.google_max_tokens,
            "temperature": settings.google_temperature,
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
                resp = client.post(
                    f"{settings.google_base_url}chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError("Cannot connect to Google AI. Check your internet connection.")
        except httpx.TimeoutException:
            raise TimeoutError("Google AI did not respond in time. The request may be too long.")
        except httpx.RequestError as exc:
            raise RuntimeError("Google AI request failed.") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"Google AI returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Google AI returned no choices.")
        return choices[0].get("message", {}).get("content", "").strip()

    def generate_json_response(self, messages, system_prompt=None, model_name=None):
        if not (self._api_key or settings.google_api_key):
            raise RuntimeError("Google AI API key not configured. Set GOOGLE_API_KEY in your .env file.")
        model = self._resolve_model(model_name)
        json_system = (
            (system_prompt + "\n\n" if system_prompt else "")
            + "You MUST respond with valid JSON only. No markdown, no explanation."
        )
        payload = {
            "model": model,
            "messages": self._build_messages(messages, json_system),
            "max_tokens": 512,
            "temperature": 0.1,
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
                resp = client.post(
                    f"{settings.google_base_url}chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError("Cannot connect to Google AI. Check your internet connection.")
        except httpx.TimeoutException:
            raise TimeoutError("Google AI did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("Google AI request failed.") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"Google AI returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Google AI returned no choices.")
        return choices[0].get("message", {}).get("content", "").strip()

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        if not (self._api_key or settings.google_api_key):
            yield {"type": "error", "error": "Google AI API key not configured. Set GOOGLE_API_KEY in your .env file."}
            return
        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.google_max_tokens,
            "temperature": settings.google_temperature,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
                async with client.stream(
                    "POST",
                    f"{settings.google_base_url}chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = (await response.aread()).decode("utf-8", errors="replace")[:300]
                        yield {"type": "error", "error": f"Google AI returned HTTP {response.status_code}: {detail}"}
                        return
                    full_response = []
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        if line.startswith("data: "):
                            line = line[6:]
                        if line.strip() == "[DONE]":
                            yield {"type": "done", "response": "".join(full_response)}
                            return
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        delta = (
                            chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        )
                        if delta:
                            full_response.append(delta)
                            yield {"type": "token", "token": delta}
                    yield {"type": "done", "response": "".join(full_response)}
        except httpx.ConnectError:
            yield {"type": "error", "error": "Cannot connect to Google AI. Check your internet connection."}
        except httpx.TimeoutException:
            yield {"type": "error", "error": "Google AI did not respond in time."}
        except httpx.RequestError:
            yield {"type": "error", "error": "Google AI request failed."}

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Dynamically fetch Gemini models from the OpenAI-compatible /models endpoint."""
        if not (self._api_key or settings.google_api_key):
            return None
        chat_models: List[str] = []
        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
                resp = client.get(
                    f"{settings.google_base_url}models",
                    headers={"Authorization": f"Bearer {self._api_key or settings.google_api_key}"},
                )
            if resp.status_code == 200:
                data = resp.json()
                # OpenAI-compat shape: {"data": [{"id": "..."}]}
                for model in data.get("data", []):
                    mid = model.get("id", "")
                    if not mid:
                        continue
                    lowered = mid.lower()
                    if any(m in lowered for m in _NON_CHAT_MARKERS):
                        continue
                    chat_models.append(mid)
                # Fallback shape: {"models": [{"name": "models/gemini-..."}]}
                if not chat_models:
                    for model in data.get("models", []):
                        mid = model.get("name", "")
                        if mid.startswith("models/"):
                            mid = mid[len("models/"):]
                        if mid and not any(m in mid.lower() for m in _NON_CHAT_MARKERS):
                            chat_models.append(mid)
        except Exception as exc:
            logger.error("Google AI /models request failed: %s", exc)

        if not chat_models:
            logger.warning("Google AI /models returned nothing; using curated Gemini list.")
            chat_models = list(_POPULAR_GEMINI_MODELS)

        chat_models.sort()
        logger.info("Fetched %d chat models from Google AI.", len(chat_models))
        return chat_models

    def health_check(self) -> bool:
        if not (self._api_key or settings.google_api_key):
            return False
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{settings.google_base_url}models",
                    headers={"Authorization": f"Bearer {self._api_key or settings.google_api_key}"},
                )
                return resp.status_code == 200
        except Exception:
            return False
