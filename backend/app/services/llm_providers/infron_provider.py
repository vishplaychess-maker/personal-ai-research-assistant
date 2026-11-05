"""
Infron AI (infron.ai) LLM provider.

OpenAI-compatible chat-completions API with Bearer API-key authentication —
the same shape as the Venice/Yeta/Token Harbor providers, pointed at a
different gateway. Strictly optional: no network call happens unless the
user selects "infron" in Settings → Providers (or sets LLM_PROVIDER=infron).

Base URL: https://api.infron.ai/v1  (verified live: GET /models answers 200
with a public catalog; POST /chat/completions without a key returns an
OpenAI-shaped 401 "No token provided")
Auth:     Authorization: Bearer <key>
Free tier: model ids ending in ":free" (e.g. qwen/qwen3.8-27b:free,
apodex/apodex-1.1-mini:free) — ``is_free_model`` already flags them.
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

#: Curated free-tier ids read from the live public catalog
#: (GET https://api.infron.ai/v1/models). Used when no key exists or the
#: catalog call fails, so the Add Provider dropdown is never empty.
DEFAULT_INFRON_MODELS: tuple[str, ...] = (
    "qwen/qwen3.8-27b:free",
    "internlm/atria-dawn-preview:free",
    "apodex/apodex-1.1-mini:free",
)


class InfronProvider(LLMProvider):
    """Provider for Infron AI (OpenAI-compatible API, Bearer key)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "Infron AI"

    @property
    def default_model(self) -> str:
        return self._model or settings.infron_default_model

    def _api_token(self) -> str:
        return (self._api_key or settings.infron_api_key or "").strip()

    def _build_headers(self) -> dict:
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
        return model_name or self._model or settings.infron_default_model

    def _missing_key_error(self) -> RuntimeError:
        return RuntimeError(
            "Infron AI API key not configured. "
            "Set INFRON_API_KEY or save a key in Settings → Providers."
        )

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not self._api_token():
            raise self._missing_key_error()

        payload = {
            "model": self._resolve_model(model_name),
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.infron_max_tokens,
            "temperature": settings.infron_temperature,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.infron_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Infron AI. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                "Infron AI did not respond in time. The request may be too long."
            )
        except httpx.RequestError as exc:
            raise RuntimeError("Infron AI request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(f"Infron AI returned HTTP {resp.status_code}: {detail}")

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Infron AI returned no choices.")
        return choices[0].get("message", {}).get("content", "").strip()

    def generate_json_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        if not self._api_token():
            raise self._missing_key_error()

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
                    f"{settings.infron_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Infron AI. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError("Infron AI did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("Infron AI request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(f"Infron AI returned HTTP {resp.status_code}: {detail}")

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Infron AI returned no choices.")
        return choices[0].get("message", {}).get("content", "").strip()

    async def generate_stream_async(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        if not self._api_token():
            yield {"type": "error", "error": str(self._missing_key_error())}
            return

        payload = {
            "model": self._resolve_model(model_name),
            "messages": self._build_messages(messages, system_prompt),
            "max_tokens": settings.infron_max_tokens,
            "temperature": settings.infron_temperature,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{settings.infron_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = await response.aread()
                        detail_text = detail.decode("utf-8", errors="replace")[:300]
                        yield {
                            "type": "error",
                            "error": (
                                f"Infron AI returned HTTP {response.status_code}: "
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
                "error": "Cannot connect to Infron AI. Check your internet connection.",
            }
        except httpx.TimeoutException:
            yield {"type": "error", "error": "Infron AI did not respond in time."}
        except httpx.RequestError:
            yield {"type": "error", "error": "Infron AI request failed."}

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Fetch the catalog from ``/models``; fall back to the curated list.

        Free-tier ids (``*:free``) are kept and ordered first — they are the
        point of this provider. ``/models`` is public, so the live catalog
        works even before a key is saved.
        """
        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
                headers = {}
                if self._api_token():
                    headers["Authorization"] = f"Bearer {self._api_token()}"
                resp = client.get(f"{settings.infron_base_url}/models", headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Infron AI /models request failed (%s); using curated list.",
                type(exc).__name__,
            )
            return list(DEFAULT_INFRON_MODELS)

        if resp.status_code != 200:
            logger.warning(
                "Infron AI /models returned HTTP %d; using curated list.",
                resp.status_code,
            )
            return list(DEFAULT_INFRON_MODELS)

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - malformed catalog is not fatal
            logger.warning("Could not parse Infron AI /models response; using curated list.")
            return list(DEFAULT_INFRON_MODELS)

        names: List[str] = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if model_id:
                names.append(model_id)

        if not names:
            return list(DEFAULT_INFRON_MODELS)
        ordered = sorted(set(names))
        # Cap the dropdown: free ids first, then the rest alphabetically.
        free = [n for n in ordered if ":free" in n]
        paid = [n for n in ordered if ":free" not in n]
        return (free + paid)[:80]

    def health_check(self) -> bool:
        """Reachable and authenticated when ``/models`` answers 200."""
        token = self._api_token()
        if not token:
            return False
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                resp = client.get(
                    f"{settings.infron_base_url}/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - health probes never raise
            return False
