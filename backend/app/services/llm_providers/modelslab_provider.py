"""
ModelsLab LLM provider.

Assumed OpenAI-compatible API (per task instructions).
Base URL: https://modelslab.com/api/v1
Docs:     https://modelslab.com/api
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

# Fallback list of well-known ModelsLab chat models if /models fails/empty.
_POPULAR_MODELSLAB_MODELS = [
    "meta-llama/Llama-3.3-70B-Instruct",
    "meta-llama/Llama-3.1-8B-Instruct",
    "deepseek-ai/DeepSeek-R1",
    "Qwen/Qwen2.5-72B-Instruct",
]

_NON_CHAT_MARKERS = ("embed", "rerank", "stable-diffusion", "flux", "sdxl", "txt2img", "img2img")


class ModelsLabProvider(LLMProvider):
    """Provider for ModelsLab (OpenAI-compatible API)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "ModelsLab"

    @property
    def default_model(self) -> str:
        return self._model or settings.modelslab_model

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key or settings.modelslab_api_key}",
            "Content-Type": "application/json",
        }

    def _build_messages(self, messages, system_prompt=None):
        result = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        result.extend(messages)
        return result

    def _resolve_model(self, model_name: Optional[str] = None) -> str:
        return model_name or self._model or settings.modelslab_model

    def generate_response(self, messages, system_prompt=None, model_name=None):
        if not (self._api_key or settings.modelslab_api_key):
            raise RuntimeError("ModelsLab API key not configured. Set MODELSLAB_API_KEY in your .env file.")
        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.modelslab_max_tokens,
            "temperature": settings.modelslab_temperature,
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
                resp = client.post(
                    f"{settings.modelslab_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError("Cannot connect to ModelsLab. Check your internet connection.")
        except httpx.TimeoutException:
            raise TimeoutError("ModelsLab did not respond in time. The request may be too long.")
        except httpx.RequestError as exc:
            raise RuntimeError("ModelsLab request failed.") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"ModelsLab returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("ModelsLab returned no choices.")
        return choices[0].get("message", {}).get("content", "").strip()

    def generate_json_response(self, messages, system_prompt=None, model_name=None):
        if not (self._api_key or settings.modelslab_api_key):
            raise RuntimeError("ModelsLab API key not configured. Set MODELSLAB_API_KEY in your .env file.")
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
                    f"{settings.modelslab_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError("Cannot connect to ModelsLab. Check your internet connection.")
        except httpx.TimeoutException:
            raise TimeoutError("ModelsLab did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("ModelsLab request failed.") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"ModelsLab returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("ModelsLab returned no choices.")
        return choices[0].get("message", {}).get("content", "").strip()

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        if not (self._api_key or settings.modelslab_api_key):
            yield {"type": "error", "error": "ModelsLab API key not configured. Set MODELSLAB_API_KEY in your .env file."}
            return
        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.modelslab_max_tokens,
            "temperature": settings.modelslab_temperature,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
                async with client.stream(
                    "POST",
                    f"{settings.modelslab_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = (await response.aread()).decode("utf-8", errors="replace")[:300]
                        yield {"type": "error", "error": f"ModelsLab returned HTTP {response.status_code}: {detail}"}
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
            yield {"type": "error", "error": "Cannot connect to ModelsLab. Check your internet connection."}
        except httpx.TimeoutException:
            yield {"type": "error", "error": "ModelsLab did not respond in time."}
        except httpx.RequestError:
            yield {"type": "error", "error": "ModelsLab request failed."}

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Fetch models from ModelsLab's OpenAI-compatible /models endpoint.

        Falls back to a curated list when the endpoint is unavailable or
        returns no usable chat models.
        """
        if not (self._api_key or settings.modelslab_api_key):
            return None
        chat_models: List[str] = []
        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
                resp = client.get(
                    f"{settings.modelslab_base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key or settings.modelslab_api_key}"},
                )
            if resp.status_code == 200:
                data = resp.json()
                for model in data.get("data", []):
                    mid = model.get("id", "")
                    if not mid:
                        continue
                    lowered = mid.lower()
                    if any(m in lowered for m in _NON_CHAT_MARKERS):
                        continue
                    chat_models.append(mid)
        except Exception as exc:
            logger.error("ModelsLab /models request failed: %s", exc)

        if not chat_models:
            logger.warning("ModelsLab /models returned nothing; using curated list.")
            chat_models = list(_POPULAR_MODELSLAB_MODELS)

        chat_models.sort()
        logger.info("Fetched %d chat models from ModelsLab.", len(chat_models))
        return chat_models

    def health_check(self) -> bool:
        if not (self._api_key or settings.modelslab_api_key):
            return False
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{settings.modelslab_base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key or settings.modelslab_api_key}"},
                )
                return resp.status_code == 200
        except Exception:
            return False
