"""
Token Harbor (tokenharbor.ai) LLM provider.

OpenAI-compatible chat-completions API with Bearer API-key authentication —
the same shape as the Venice/Yeta providers, pointed at a different gateway.
Strictly optional: no network call happens unless the user selects
"token_harbor" in Settings → Providers (or sets LLM_PROVIDER=token_harbor).

Base URL: https://tokenharbor.ai/v1  (verified live: unauthenticated requests
return a standard OpenAI-shaped invalid_api_key 401 naming the
"Authorization: Bearer thk_live_…" scheme; www.tokenharbor.ai explicitly
refuses API traffic and points back at this host)
Auth:     Authorization: Bearer <thk_live_… key>
Free tier: model ids ending in ":free" never charge the balance, so the
catalog keeps them and ``is_free_model`` already flags them.
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
#: provider error) so the Add Provider model dropdown is never empty. These
#: are ids from https://tokenharbor.ai/docs quickstart; the real catalog with
#: live prices comes from GET /models once a key is saved.
DEFAULT_TOKEN_HARBOR_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash:free",
    "tokenharbor/deepseek-v4.1-flash",
)


class TokenHarborProvider(LLMProvider):
    """Provider for Token Harbor (OpenAI-compatible API, Bearer key)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self._api_key = api_key
        self._model = model

    @property
    def name(self) -> str:
        return "Token Harbor"

    @property
    def default_model(self) -> str:
        return self._model or settings.token_harbor_default_model

    def _api_token(self) -> str:
        return (self._api_key or settings.token_harbor_api_key or "").strip()

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
        return model_name or self._model or settings.token_harbor_default_model

    def _missing_key_error(self) -> RuntimeError:
        return RuntimeError(
            "Token Harbor API key not configured. "
            "Set TOKEN_HARBOR_API_KEY or save a key in Settings → Providers."
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
            "max_tokens": settings.token_harbor_max_tokens,
            "temperature": settings.token_harbor_temperature,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                resp = client.post(
                    f"{settings.token_harbor_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Token Harbor. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                "Token Harbor did not respond in time. The request may be too long."
            )
        except httpx.RequestError as exc:
            raise RuntimeError("Token Harbor request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Token Harbor returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Token Harbor returned no choices.")
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
                    f"{settings.token_harbor_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                )
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to Token Harbor. Check your internet connection."
            )
        except httpx.TimeoutException:
            raise TimeoutError("Token Harbor did not respond in time.")
        except httpx.RequestError as exc:
            raise RuntimeError("Token Harbor request failed.") from exc

        if resp.status_code != 200:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Token Harbor returned HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Token Harbor returned no choices.")
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
            "max_tokens": settings.token_harbor_max_tokens,
            "temperature": settings.token_harbor_temperature,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{settings.token_harbor_base_url}/chat/completions",
                    headers=self._build_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        detail = await response.aread()
                        detail_text = detail.decode("utf-8", errors="replace")[:300]
                        yield {
                            "type": "error",
                            "error": (
                                f"Token Harbor returned HTTP {response.status_code}: "
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
                "error": "Cannot connect to Token Harbor. Check your internet connection.",
            }
        except httpx.TimeoutException:
            yield {"type": "error", "error": "Token Harbor did not respond in time."}
        except httpx.RequestError:
            yield {"type": "error", "error": "Token Harbor request failed."}

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Fetch the catalog from ``/models``; fall back to the curated list.

        Free-tier ids (``*:free``) are kept — ``is_free_model`` marks them in
        the UI. Like Venice/Yeta, the curated list is returned before any key
        exists so the Add Provider dropdown is usable right away.
        """
        token = self._api_token()
        if not token:
            return list(DEFAULT_TOKEN_HARBOR_MODELS)

        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
                resp = client.get(
                    f"{settings.token_harbor_base_url}/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Token Harbor /models request failed (%s); using curated list.",
                type(exc).__name__,
            )
            return list(DEFAULT_TOKEN_HARBOR_MODELS)

        if resp.status_code != 200:
            logger.warning(
                "Token Harbor /models returned HTTP %d; using curated list.",
                resp.status_code,
            )
            return list(DEFAULT_TOKEN_HARBOR_MODELS)

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - malformed catalog is not fatal
            logger.warning(
                "Could not parse Token Harbor /models response; using curated list."
            )
            return list(DEFAULT_TOKEN_HARBOR_MODELS)

        names: List[str] = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if model_id:
                names.append(model_id)

        if not names:
            return list(DEFAULT_TOKEN_HARBOR_MODELS)
        # Free-tier ids first — they are the point of this provider.
        ordered = sorted(set(names))
        return sorted(ordered, key=lambda n: (":free" not in n, n))

    def health_check(self) -> bool:
        """Reachable and authenticated when ``/models`` answers 200."""
        token = self._api_token()
        if not token:
            return False
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                resp = client.get(
                    f"{settings.token_harbor_base_url}/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - health probes never raise
            return False
