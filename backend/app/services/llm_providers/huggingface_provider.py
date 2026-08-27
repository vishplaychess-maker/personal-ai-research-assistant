"""
Hugging Face LLM provider.

Uses Hugging Face's Serverless Inference API (OpenAI-compatible) for chat
generation. A free Hugging Face token gives access to hundreds of free
models.

Base URL: https://router.huggingface.co/v1
API docs: https://huggingface.co/docs/inference-providers/en/index
"""

import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.config import settings
from app.services.llm_providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Connection / timeout settings
CONNECT_TIMEOUT = 15.0
GENERATE_TIMEOUT = 120.0

# Popular free serverless chat models, used as a fallback when the
# /models endpoint returns no usable chat models.
_POPULAR_CHAT_MODELS = [
    "meta-llama/Meta-Llama-3-8B-Instruct",
    "mistralai/Mistral-7B-Instruct-v0.3",
    "google/gemma-2-9b-it",
    "Qwen/Qwen2.5-7B-Instruct",
    "HuggingFaceH4/zephyr-7b-beta",
    "microsoft/Phi-3-mini-4k-instruct",
    "deepseek-ai/deepseek-llm-7b-chat",
]

# Model-id substrings that indicate non-chat models (embedding, rerank,
# sentence-transformers, or image generation) that should be filtered out.
_NON_CHAT_MARKERS = (
    "embed",
    "rerank",
    "sentence-transformers",
    "stable-diffusion",
    "flux",
)


class HuggingFaceProvider(LLMProvider):
    """Provider for Hugging Face Serverless Inference (OpenAI-compatible)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "Hugging Face"

    @property
    def default_model(self) -> str:
        return self._model or settings.huggingface_model

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key or settings.huggingface_api_key}",
            "Content-Type": "application/json",
        }

    def _build_messages(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Build OpenAI-compatible messages array."""
        result = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        result.extend(messages)
        return result

    def _resolve_model(self, model_name: Optional[str] = None) -> str:
        return model_name or self._model or settings.huggingface_model

    def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not (self._api_key or settings.huggingface_api_key):
            raise RuntimeError(
                "Hugging Face API key not configured. "
                "Set HUGGINGFACE_API_KEY in your .env file."
            )

        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.huggingface_max_tokens,
            "temperature": settings.huggingface_temperature,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.huggingface_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Hugging Face. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                "Hugging Face did not respond in time. The request may be too long."
            )
        except httpx.RequestError as exc:
            raise RuntimeError("Hugging Face request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Hugging Face returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Hugging Face returned no choices.")

        return choices[0].get("message", {}).get("content", "").strip()

    def generate_json_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not (self._api_key or settings.huggingface_api_key):
            raise RuntimeError(
                "Hugging Face API key not configured. "
                "Set HUGGINGFACE_API_KEY in your .env file."
            )

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
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.huggingface_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Hugging Face. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError("Hugging Face did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("Hugging Face request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Hugging Face returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Hugging Face returned no choices.")

        return choices[0].get("message", {}).get("content", "").strip()

    async def generate_stream_async(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        if not (self._api_key or settings.huggingface_api_key):
            yield {
                "type": "error",
                "error": (
                    "Hugging Face API key not configured. "
                    "Set HUGGINGFACE_API_KEY in your .env file."
                ),
            }
            return

        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.huggingface_max_tokens,
            "temperature": settings.huggingface_temperature,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{settings.huggingface_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = await response.aread()
                        detail_text = detail.decode("utf-8", errors="replace")[:300]
                        yield {
                            "type": "error",
                            "error": f"Hugging Face returned HTTP {response.status_code}: {detail_text}",
                        }
                        return

                    full_response = []
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        if line.startswith("data: "):
                            line = line[6:]
                        if line.strip() == "[DONE]":
                            yield {
                                "type": "done",
                                "response": "".join(full_response),
                            }
                            return
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        delta = (
                            chunk.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        if delta:
                            full_response.append(delta)
                            yield {"type": "token", "token": delta}

                    yield {
                        "type": "done",
                        "response": "".join(full_response),
                    }

        except httpx.ConnectError:
            yield {
                "type": "error",
                "error": "Cannot connect to Hugging Face. Check your internet connection.",
            }
        except httpx.TimeoutException:
            yield {
                "type": "error",
                "error": "Hugging Face did not respond in time.",
            }
        except httpx.RequestError:
            yield {
                "type": "error",
                "error": "Hugging Face request failed.",
            }

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """
        Dynamically fetch available chat models from Hugging Face's
        OpenAI-compatible ``GET /models`` endpoint.

        Returns None if the API key is not configured (fail-closed).
        Falls back to a curated list of popular free chat models when the
        endpoint returns no usable models or fails.
        """
        if not (self._api_key or settings.huggingface_api_key):
            return None

        chat_models: List[str] = []
        try:
            with httpx.Client(
                timeout=httpx.Timeout(15.0, connect=10.0)
            ) as client:
                resp = client.get(
                    f"{settings.huggingface_base_url}/models",
                    headers={
                        "Authorization": f"Bearer {self._api_key or settings.huggingface_api_key}",
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                models_data = data.get("data", [])
                for model in models_data:
                    model_id = model.get("id", "")
                    if not model_id:
                        continue
                    lowered = model_id.lower()
                    if any(m in lowered for m in _NON_CHAT_MARKERS):
                        continue
                    chat_models.append(model_id)
        except httpx.ConnectError:
            logger.error("Cannot connect to Hugging Face to fetch models.")
        except httpx.TimeoutException:
            logger.error("Hugging Face /models request timed out.")
        except httpx.RequestError as exc:
            logger.error("Hugging Face /models request failed: %s", exc)
        except Exception:
            logger.error("Failed to parse Hugging Face /models response.")

        if not chat_models:
            logger.warning(
                "Hugging Face /models returned no usable chat models; "
                "falling back to a curated popular list."
            )
            chat_models = list(_POPULAR_CHAT_MODELS)

        chat_models.sort()
        logger.info(
            "Fetched %d chat models from Hugging Face.", len(chat_models)
        )
        return chat_models

    def health_check(self) -> bool:
        """Check if Hugging Face API is reachable."""
        if not (self._api_key or settings.huggingface_api_key):
            return False
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{settings.huggingface_base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key or settings.huggingface_api_key}"},
                )
                return resp.status_code == 200
        except Exception:
            return False
