"""
GLM 5.3 Flash LLM provider (local Verdent Free Router).

Talks to the local Verdent Free Router at ``http://127.0.0.1:8320/v1`` using
its OpenAI-compatible ``/chat/completions`` endpoint. Keyless by default —
no API key is required. If a key is ever needed, set ``VERDENT_API_KEY``
in the environment and it is sent as a Bearer token automatically.

API docs: OpenAI-compatible (see the router's /docs page).
"""

import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.config import settings
from app.services.llm_providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Connection / timeout settings (matches the OpenRouter provider pattern)
CONNECT_TIMEOUT = 15.0
GENERATE_TIMEOUT = 120.0


def _verdent_api_key() -> str:
    """Optional Bearer key (keyless router needs none; env override supported)."""
    return (
        (os.environ.get("VERDENT_API_KEY") or "").strip()
        or (settings.glm_api_key or "").strip()
    )


class GLMProvider(LLMProvider):
    """Provider for GLM 5.3 Flash via the local Verdent Free Router (free)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        # Keyless by default; an explicit api_key (per-user override) or the
        # VERDENT_API_KEY env var is only sent when actually set.
        self._api_key = api_key or _verdent_api_key() or None
        self._model = model

    @property
    def name(self) -> str:
        return "GLM"

    @property
    def default_model(self) -> str:
        return self._model or settings.glm_model

    def _build_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_messages(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build OpenAI-compatible messages array (string or multimodal parts)."""
        result = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                result.append({"role": msg["role"], "content": content})
            else:
                result.append({"role": msg["role"], "content": content})
        return result

    def _resolve_model(self, model_name: Optional[str] = None) -> str:
        return model_name or self._model or settings.glm_model

    def _base_url(self) -> str:
        return settings.glm_base_url.rstrip("/")

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.glm_max_tokens,
            "temperature": settings.glm_temperature,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{self._base_url()}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to the GLM Free Router at "
                f"{self._base_url()}. Is the local router running?"
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                "GLM Free Router did not respond in time. The request may be too long."
            )
        except httpx.RequestError as exc:
            raise RuntimeError("GLM Free Router request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"GLM Free Router returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("GLM Free Router returned no choices.")

        return choices[0].get("message", {}).get("content", "").strip()

    def generate_json_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        model = self._resolve_model(model_name)
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
                    f"{self._base_url()}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to the GLM Free Router at "
                f"{self._base_url()}. Is the local router running?"
            )
        except httpx.TimeoutException:
            raise TimeoutError("GLM Free Router did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("GLM Free Router request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"GLM Free Router returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("GLM Free Router returned no choices.")

        return choices[0].get("message", {}).get("content", "").strip()

    async def generate_stream_async(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        model = self._resolve_model(model_name)
        payload = {
            "model": model,
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.glm_max_tokens,
            "temperature": settings.glm_temperature,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url()}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = await response.aread()
                        detail_text = detail.decode("utf-8", errors="replace")[:300]
                        yield {
                            "type": "error",
                            "error": (
                                f"GLM Free Router returned HTTP "
                                f"{response.status_code}: {detail_text}"
                            ),
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
                "error": (
                    "Cannot connect to the GLM Free Router at "
                    f"{self._base_url()}. Is the local router running?"
                ),
            }
        except httpx.TimeoutException:
            yield {
                "type": "error",
                "error": "GLM Free Router did not respond in time.",
            }
        except httpx.RequestError:
            yield {
                "type": "error",
                "error": "GLM Free Router request failed.",
            }

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Fetch the model catalog from the router's OpenAI-compatible /models.

        Returns None only when the endpoint cannot be reached (fail-closed for
        callers that treat None as "cannot verify availability").
        """
        try:
            with httpx.Client(
                timeout=httpx.Timeout(15.0, connect=10.0)
            ) as client:
                resp = client.get(
                    f"{self._base_url()}/models",
                    headers=self._build_headers(),
                )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Cannot reach GLM Free Router /models: %s", exc)
            return None

        if resp.status_code != 200:
            logger.error(
                "GLM Free Router /models returned HTTP %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return None

        try:
            data = resp.json()
        except Exception:
            logger.error("Failed to parse GLM Free Router /models response.")
            return None

        models_data = data.get("data", [])
        models = [m.get("id", "") for m in models_data if isinstance(m, dict) and m.get("id")]
        models.sort()
        if not models:
            # Router reachable but listing nothing useful — expose the default.
            models = [settings.glm_model]
        return models

    def health_check(self) -> bool:
        """Ping the router's /models endpoint (lightweight reachability check)."""
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self._base_url()}/models",
                    headers=self._build_headers(),
                )
                return resp.status_code == 200
        except Exception:
            return False
