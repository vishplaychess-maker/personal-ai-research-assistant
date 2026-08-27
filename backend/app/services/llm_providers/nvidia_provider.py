"""
NVIDIA NIM LLM provider.

Uses NVIDIA's Inference Microservices (NIM) OpenAI-compatible REST API
for chat generation. NVIDIA provides free credits for new accounts
with access to models like Llama 3.2.

API docs: https://docs.api.nvidia.com/nim/reference/
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


class NvidiaProvider(LLMProvider):
    """Provider for NVIDIA NIM (OpenAI-compatible API)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "NVIDIA NIM"

    @property
    def default_model(self) -> str:
        return self._model or settings.nvidia_model

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key or settings.nvidia_api_key}",
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
        return model_name or self._model or settings.nvidia_model

    def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not (self._api_key or settings.nvidia_api_key):
            raise RuntimeError(
                "NVIDIA NIM API key not configured. "
                "Set NVIDIA_API_KEY in your .env file."
            )

        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.nvidia_max_tokens,
            "temperature": settings.nvidia_temperature,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.nvidia_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to NVIDIA NIM. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                "NVIDIA NIM did not respond in time. The request may be too long."
            )
        except httpx.RequestError as exc:
            raise RuntimeError("NVIDIA NIM request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"NVIDIA NIM returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("NVIDIA NIM returned no choices.")

        return choices[0].get("message", {}).get("content", "").strip()

    def generate_json_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not (self._api_key or settings.nvidia_api_key):
            raise RuntimeError(
                "NVIDIA NIM API key not configured. "
                "Set NVIDIA_API_KEY in your .env file."
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
                    f"{settings.nvidia_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to NVIDIA NIM. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError("NVIDIA NIM did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("NVIDIA NIM request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"NVIDIA NIM returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("NVIDIA NIM returned no choices.")

        return choices[0].get("message", {}).get("content", "").strip()

    async def generate_stream_async(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        if not (self._api_key or settings.nvidia_api_key):
            yield {
                "type": "error",
                "error": (
                    "NVIDIA NIM API key not configured. "
                    "Set NVIDIA_API_KEY in your .env file."
                ),
            }
            return

        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.nvidia_max_tokens,
            "temperature": settings.nvidia_temperature,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{settings.nvidia_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = await response.aread()
                        detail_text = detail.decode("utf-8", errors="replace")[:300]
                        yield {
                            "type": "error",
                            "error": f"NVIDIA NIM returned HTTP {response.status_code}: {detail_text}",
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
                "error": "Cannot connect to NVIDIA NIM. Check your internet connection.",
            }
        except httpx.TimeoutException:
            yield {
                "type": "error",
                "error": "NVIDIA NIM did not respond in time.",
            }
        except httpx.RequestError:
            yield {
                "type": "error",
                "error": "NVIDIA NIM request failed.",
            }

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """
        NVIDIA NIM doesn't have a simple local model list.

        Returns a curated list of known free-tier models, or None
        if the API key is not configured (fail-closed).
        """
        if not (self._api_key or settings.nvidia_api_key):
            return None

        # Known NVIDIA NIM models — updated from build.nvidia.com
        return [
            "meta/llama-3.2-3b-instruct",
            "meta/llama-3.2-1b-instruct",
            "meta/llama-3.1-8b-instruct",
            "google/gemma-2-9b-it",
            "microsoft/phi-3-mini-128k-instruct",
        ]

    def health_check(self) -> bool:
        """Check if NVIDIA NIM API is reachable."""
        if not (self._api_key or settings.nvidia_api_key):
            return False
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{settings.nvidia_base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key or settings.nvidia_api_key}"},
                )
                return resp.status_code == 200
        except Exception:
            return False
