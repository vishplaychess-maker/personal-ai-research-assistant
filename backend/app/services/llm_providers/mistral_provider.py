"""
Mistral LLM provider.

Uses Mistral's OpenAI-compatible REST API for chat generation.

Base URL: https://api.mistral.ai/v1
API docs: https://docs.mistral.ai/api/
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


class MistralProvider(LLMProvider):
    """Provider for Mistral (OpenAI-compatible API)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "Mistral"

    @property
    def default_model(self) -> str:
        return self._model or settings.mistral_model

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key or settings.mistral_api_key}",
            "Content-Type": "application/json",
        }

    def _build_messages(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build OpenAI-compatible messages array.
        Handles multimodal messages where content can be a string or array of content parts.
        """
        result = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                # Multimodal content - Mistral uses OpenAI-compatible format
                result.append({"role": msg["role"], "content": content})
            else:
                result.append({"role": msg["role"], "content": content})
        return result

    def _resolve_model(self, model_name: Optional[str] = None) -> str:
        return model_name or self._model or settings.mistral_model

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not (self._api_key or settings.mistral_api_key):
            raise RuntimeError(
                "Mistral API key not configured. "
                "Set MISTRAL_API_KEY in your .env file."
            )

        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.mistral_max_tokens,
            "temperature": settings.mistral_temperature,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.mistral_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Mistral. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                "Mistral did not respond in time. The request may be too long."
            )
        except httpx.RequestError as exc:
            raise RuntimeError("Mistral request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Mistral returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Mistral returned no choices.")

        return choices[0].get("message", {}).get("content", "").strip()

    def generate_json_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not (self._api_key or settings.mistral_api_key):
            raise RuntimeError(
                "Mistral API key not configured. "
                "Set MISTRAL_API_KEY in your .env file."
            )

        model = self._resolve_model(model_name)
        # Add JSON formatting instruction to system prompt
        json_system = (
            (system_prompt + "\n\n" if system_prompt else "")
            + "You MUST respond with valid JSON only. No markdown, no explanation."
        )
        payload = {
            "model": model,
            "messages": self._build_messages(messages, json_system),
            "max_tokens": 512,
            "temperature": 0.1,  # low temp for structured output
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.mistral_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Mistral. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError("Mistral did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("Mistral request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Mistral returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Mistral returned no choices.")

        return choices[0].get("message", {}).get("content", "").strip()

    async def generate_stream_async(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        if not (self._api_key or settings.mistral_api_key):
            yield {
                "type": "error",
                "error": (
                    "Mistral API key not configured. "
                    "Set MISTRAL_API_KEY in your .env file."
                ),
            }
            return

        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.mistral_max_tokens,
            "temperature": settings.mistral_temperature,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{settings.mistral_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = await response.aread()
                        detail_text = detail.decode("utf-8", errors="replace")[:300]
                        yield {
                            "type": "error",
                            "error": f"Mistral returned HTTP {response.status_code}: {detail_text}",
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

                    # If we get here without [DONE], yield what we have
                    yield {
                        "type": "done",
                        "response": "".join(full_response),
                    }

        except httpx.ConnectError:
            yield {
                "type": "error",
                "error": "Cannot connect to Mistral. Check your internet connection.",
            }
        except httpx.TimeoutException:
            yield {
                "type": "error",
                "error": "Mistral did not respond in time.",
            }
        except httpx.RequestError:
            yield {
                "type": "error",
                "error": "Mistral request failed.",
            }

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """
        Dynamically fetch available chat models from Mistral's /models endpoint.

        Returns None if the API key is not configured (fail-closed).
        """
        if not (self._api_key or settings.mistral_api_key):
            return None

        try:
            with httpx.Client(
                timeout=httpx.Timeout(15.0, connect=10.0)
            ) as client:
                resp = client.get(
                    f"{settings.mistral_base_url}/models",
                    headers={
                        "Authorization": f"Bearer {self._api_key or settings.mistral_api_key}",
                    },
                )
        except httpx.ConnectError:
            logger.error("Cannot connect to Mistral to fetch models.")
            return []
        except httpx.TimeoutException:
            logger.error("Mistral /models request timed out.")
            return []
        except httpx.RequestError as exc:
            logger.error("Mistral /models request failed: %s", exc)
            return []

        if resp.status_code != 200:
            logger.error(
                "Mistral /models returned HTTP %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return []

        try:
            data = resp.json()
        except Exception:
            logger.error("Failed to parse Mistral /models response.")
            return []

        models_data = data.get("data", [])
        free_models: List[str] = []

        for model in models_data:
            model_id = model.get("id", "")
            pricing = model.get("pricing", {})
            prompt_price = str(pricing.get("prompt", "1"))
            completion_price = str(pricing.get("completion", "1"))

            # Free models have "0" for both prompt and completion pricing
            if prompt_price == "0" and completion_price == "0":
                free_models.append(model_id)

        # Sort alphabetically for consistent ordering
        free_models.sort()

        logger.info(
            "Fetched %d free models from Mistral.", len(free_models)
        )
        return free_models

    def health_check(self) -> bool:
        """Check if Mistral API is reachable."""
        if not (self._api_key or settings.mistral_api_key):
            return False
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{settings.mistral_base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key or settings.mistral_api_key}"},
                )
                return resp.status_code == 200
        except Exception:
            return False
