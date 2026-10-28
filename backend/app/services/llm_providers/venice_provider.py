"""
Venice.ai LLM provider.

Uses Venice's OpenAI-compatible REST API with Bearer API-key authentication.
No OAuth: the key is pasted in Settings and stored encrypted like every other
provider key.

Base URL: https://api.venice.ai/api/v1
API docs: https://docs.venice.ai/
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

#: Shown when the live catalog cannot be fetched (no key yet, offline, or a
#: provider error) so the Add Provider model dropdown is never empty. These are
#: current ids from https://docs.venice.ai/overview/models (text models only).
DEFAULT_VENICE_MODELS: tuple[str, ...] = (
    "llama-3.3-70b",
    "qwen3-235b-a22b-instruct-2507",
    "mistral-small-3-2-24b-instruct",
)


class VeniceProvider(LLMProvider):
    """Provider for Venice.ai (OpenAI-compatible API, Bearer key)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "Venice.ai"

    @property
    def default_model(self) -> str:
        return self._model or settings.venice_model

    def _api_token(self) -> str:
        return (self._api_key or settings.venice_api_key or "").strip()

    def _build_headers(self) -> dict:
        # Venice uses a plain Bearer API key, identical to the OpenAI shape.
        return {
            "Authorization": f"Bearer {self._api_token()}",
            "Content-Type": "application/json",
        }

    def _build_messages(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build OpenAI-compatible messages, preserving multimodal parts."""
        result = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        for msg in messages:
            result.append({"role": msg["role"], "content": msg.get("content", "")})
        return result

    def _resolve_model(self, model_name: Optional[str] = None) -> str:
        return model_name or self._model or settings.venice_model

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not self._api_token():
            raise RuntimeError(
                "Venice.ai API key not configured. "
                "Set VENICE_API_KEY or save a key in Settings → Providers."
            )

        payload = {
            "model": self._resolve_model(model_name),
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.venice_max_tokens,
            "temperature": settings.venice_temperature,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.venice_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Venice.ai. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                "Venice.ai did not respond in time. The request may be too long."
            )
        except httpx.RequestError as exc:
            raise RuntimeError("Venice.ai request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Venice.ai returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Venice.ai returned no choices.")
        return choices[0].get("message", {}).get("content", "").strip()

    def generate_json_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not self._api_token():
            raise RuntimeError(
                "Venice.ai API key not configured. "
                "Set VENICE_API_KEY or save a key in Settings → Providers."
            )

        json_system = (
            (system_prompt + "\n\n" if system_prompt else "")
            + "You MUST respond with valid JSON only. No markdown, no explanation."
        )
        payload = {
            "model": self._resolve_model(model_name),
            "messages": self._build_messages(messages, json_system),
            "max_tokens": 512,
            "temperature": 0.1,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.venice_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Venice.ai. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError("Venice.ai did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("Venice.ai request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Venice.ai returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Venice.ai returned no choices.")
        return choices[0].get("message", {}).get("content", "").strip()

    async def generate_stream_async(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        if not self._api_token():
            yield {
                "type": "error",
                "error": (
                    "Venice.ai API key not configured. "
                    "Set VENICE_API_KEY or save a key in Settings → Providers."
                ),
            }
            return

        payload = {
            "model": self._resolve_model(model_name),
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.venice_max_tokens,
            "temperature": settings.venice_temperature,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{settings.venice_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = await response.aread()
                        detail_text = detail.decode("utf-8", errors="replace")[:300]
                        yield {
                            "type": "error",
                            "error": (
                                f"Venice.ai returned HTTP {response.status_code}: "
                                f"{detail_text}"
                            ),
                        }
                        return

                    full_response: List[str] = []
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
                            (chunk.get("choices") or [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        if delta:
                            full_response.append(delta)
                            yield {"type": "token", "token": delta}

                    yield {"type": "done", "response": "".join(full_response)}

        except httpx.ConnectError:
            yield {
                "type": "error",
                "error": "Cannot connect to Venice.ai. Check your internet connection.",
            }
        except httpx.TimeoutException:
            yield {"type": "error", "error": "Venice.ai did not respond in time."}
        except httpx.RequestError:
            yield {"type": "error", "error": "Venice.ai request failed."}

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Fetch text models from ``/models``; fall back to a curated list.

        Unlike most providers, Venice returns the curated list (not None/[])
        when no key is configured or the catalog call fails, so the Add
        Provider dropdown is usable before a key is saved.
        """
        token = self._api_token()
        if not token:
            return list(DEFAULT_VENICE_MODELS)

        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
                resp = client.get(
                    f"{settings.venice_base_url}/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Venice.ai /models request failed (%s); using curated list.",
                type(exc).__name__,
            )
            return list(DEFAULT_VENICE_MODELS)

        if resp.status_code != 200:
            logger.warning(
                "Venice.ai /models returned HTTP %d; using curated list.",
                resp.status_code,
            )
            return list(DEFAULT_VENICE_MODELS)

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - malformed catalog is not fatal
            logger.warning("Could not parse Venice.ai /models response; using curated list.")
            return list(DEFAULT_VENICE_MODELS)

        names: List[str] = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            model_type = str(item.get("type") or "").strip().lower()
            # Venice mixes text/image/video models in one catalog.
            if model_id and model_type in ("", "text"):
                names.append(model_id)

        if not names:
            return list(DEFAULT_VENICE_MODELS)
        return sorted(set(names))

    def health_check(self) -> bool:
        """Reachable and authenticated when ``/models`` answers 200."""
        token = self._api_token()
        if not token:
            return False
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                resp = client.get(
                    f"{settings.venice_base_url}/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - health probes never raise
            return False
