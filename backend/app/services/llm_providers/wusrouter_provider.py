"""
WusRouter (api.wusrouter.com) LLM provider.

OpenAI-compatible chat-completions API with Bearer API-key authentication —
the same shape as the Venice/Yeta/Token Harbor/Infron/Zro/Experiential
providers. The gateway is a free unified AI gateway with 70+ hosted and
community models (GLM, DeepSeek, Kimi, …) and standing ``:free`` tiers.
Strictly optional: no network call happens unless the user selects
"wusrouter" in Settings → Providers (or sets LLM_PROVIDER=wusrouter).

Base URL: https://api.wusrouter.com/v1  (verified live: /models is public and
answers 200 keyless; chat/completions with a Bearer header returns an
OpenAI-shaped 401 invalid_api_key for a bad key — keyless POSTs get a
misleading 404 endpoint_not_found, so the Bearer header is always sent)
Auth:     Authorization: Bearer <key from the wusrouter.com dashboard>
Catalog:  GET /v1/models is public; the curated ids below keep the dropdown
          usable when the catalog is unreachable (e.g. a Cloudflare 403).
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

#: Shown when the live catalog cannot be fetched (offline, Cloudflare 403,
#: or a provider error). These ids were verified against the public
#: GET /v1/models catalog on 2026-11-10.
DEFAULT_WUSROUTER_MODELS: tuple[str, ...] = (
    "glm-5.3-flash",
    "deepseek-v4-flash",
    "deepseek-v4.1-flash",
    "kimi-k3",
    "wusrouter/qwen3.8-flash:free",
    "wr/gpt-6-luna:free",
)


class WusRouterProvider(LLMProvider):
    """Provider for WusRouter (OpenAI-compatible gateway, Bearer key)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "WusRouter"

    @property
    def default_model(self) -> str:
        return self._model or settings.wusrouter_default_model

    def _api_token(self) -> str:
        return (self._api_key or settings.wusrouter_api_key or "").strip()

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
        return model_name or self._model or settings.wusrouter_default_model

    def _missing_key_error(self) -> RuntimeError:
        return RuntimeError(
            "WusRouter API key not configured. "
            "Set WUSROUTER_API_KEY or save a key in Settings → Providers."
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
            "max_tokens": settings.wusrouter_max_tokens,
            "temperature": settings.wusrouter_temperature,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.wusrouter_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to WusRouter. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                "WusRouter did not respond in time. The request may be too long."
            )
        except httpx.RequestError as exc:
            raise RuntimeError("WusRouter request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"WusRouter returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("WusRouter returned no choices.")
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
                    f"{settings.wusrouter_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to WusRouter. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError("WusRouter did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("WusRouter request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"WusRouter returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("WusRouter returned no choices.")
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
            "max_tokens": settings.wusrouter_max_tokens,
            "temperature": settings.wusrouter_temperature,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{settings.wusrouter_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = await response.aread()
                        detail_text = detail.decode("utf-8", errors="replace")[:300]
                        yield {
                            "type": "error",
                            "error": (
                                f"WusRouter returned HTTP "
                                f"{response.status_code}: {detail_text}"
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
                "error": (
                    "Cannot connect to WusRouter. "
                    "Check your internet connection."
                ),
            }
        except httpx.TimeoutException:
            yield {"type": "error", "error": "WusRouter did not respond in time."}
        except httpx.RequestError:
            yield {"type": "error", "error": "WusRouter request failed."}

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Fetch the callable models from ``/models``; fall back to the
        curated ids on any failure. ``/models`` is public (verified), so the
        live catalog works even before a key is saved.
        """
        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
                headers = {}
                if self._api_token():
                    headers["Authorization"] = f"Bearer {self._api_token()}"
                resp = client.get(f"{settings.wusrouter_base_url}/models", headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "WusRouter /models request failed (%s); using curated list.",
                type(exc).__name__,
            )
            return list(DEFAULT_WUSROUTER_MODELS)

        if resp.status_code != 200:
            logger.warning(
                "WusRouter /models returned HTTP %d; using curated list.",
                resp.status_code,
            )
            return list(DEFAULT_WUSROUTER_MODELS)

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - malformed catalog is not fatal
            logger.warning("Could not parse WusRouter /models response; using curated list.")
            return list(DEFAULT_WUSROUTER_MODELS)

        names: List[str] = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if model_id:
                names.append(model_id)

        if not names:
            return list(DEFAULT_WUSROUTER_MODELS)
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
                    f"{settings.wusrouter_base_url}/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - health probes never raise
            return False