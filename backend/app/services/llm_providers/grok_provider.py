"""Grok (xAI) provider — API key *or* an OAuth subscription sign-in.

Two transport shapes, because the two credentials are not interchangeable:

* ``api_key`` mode talks to the developer API (``https://api.x.ai/v1``), which
  is the ordinary OpenAI-compatible ``/chat/completions`` surface.
* OAuth mode targets the Grok CLI chat proxy
  (``https://cli-chat-proxy.grok.com/v1``). A subscription bearer is refused by
  ``api.x.ai`` with 402/403, the proxy speaks the OpenAI **Responses** API
  rather than chat-completions, and it only serves a request that identifies
  itself as the Grok CLI (the headers below).

Which models the account may use is decided by what the authenticated catalog
returns, not by a plan claim: xAI's userinfo endpoint publishes no tier field.

Fail-closed contract: when this provider is built in OAuth mode it never reads
an API key. A dead grant raises "sign in again" instead of quietly switching
credential, because a silent switch would spend money the user did not approve.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.config import settings
from app.services.llm_providers.base import LLMProvider
from app.services.oauth_token_store import GROK, TokenRefreshError, token_store

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15.0
GENERATE_TIMEOUT = 120.0
CATALOG_TIMEOUT = 20.0

#: The proxy fingerprints the client before it will accept a subscription
#: bearer. Pinned; if xAI tightens the gate, this is the value to update.
CLIENT_IDENTITY_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-identifier": "grok-shell",
    "x-grok-client-version": "1.0.46",
    "x-grok-client-surface": "grok-build",
    "x-grok-client-mode": "grok-shell",
}

#: Models the subscription proxy does not serve even when they appear in a
#: catalog; advertising them would offer a model that 400s on first use.
NOT_SUBSCRIPTION_MODELS = frozenset({"grok-build-0.1"})

#: Non-chat surfaces that share the catalog endpoint.
_NON_CHAT_HINTS = ("imagine", "embed", "voice", "video", "image")

#: Statuses whose upstream body is replaced by fixed copy. An auth or billing
#: body can name the account, a plan, or a spending limit; the code and a
#: bounded message are enough for the user and the log.
_REDACTED_STATUSES = (401, 402, 403, 429)

_SUBSCRIPTION_MESSAGE = (
    "Grok refused this account's subscription access. Check that the SuperGrok "
    "or X Premium+ plan is still active."
)
_RATE_LIMIT_MESSAGE = (
    "Grok rate limit reached for this subscription. Try again in a moment."
)
_REAUTH_MESSAGE = (
    "Your Grok sign-in is no longer valid. Sign in with Grok again."
)


class GrokProvider(LLMProvider):
    """Grok through an API key or a subscription sign-in (never both)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        *,
        user_id: Optional[int] = None,
        store=None,
        oauth_service=None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._user_id = user_id
        self._store = store or token_store
        self._oauth_service = oauth_service

    # ── identity ─────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Grok"

    @property
    def default_model(self) -> str:
        return self._model or settings.grok_model

    @property
    def uses_oauth(self) -> bool:
        return self._user_id is not None

    # ── auth ─────────────────────────────────────────────────

    def _access_token(self) -> str:
        """The subscription bearer, refreshed by the store if it is near expiry."""
        if self._user_id is None:
            raise RuntimeError(_REAUTH_MESSAGE)
        try:
            return self._store.get_valid_token(
                self._user_id, GROK, service=self._oauth_service
            )
        except TokenRefreshError as exc:
            raise RuntimeError(str(exc)) from exc

    async def _access_token_async(self) -> str:
        if self._user_id is None:
            raise RuntimeError(_REAUTH_MESSAGE)
        try:
            return await self._store.get_valid_token_async(
                self._user_id, GROK, service=self._oauth_service
            )
        except TokenRefreshError as exc:
            raise RuntimeError(str(exc)) from exc

    def _api_key_or_config(self) -> str:
        return self._api_key or settings.grok_api_key

    def _keyless(self) -> str:
        return (
            "Grok is not configured. Add an xAI API key on the Grok card, or "
            "use Sign in with Grok to spend a subscription."
        )

    def _base_url(self) -> str:
        return (
            settings.grok_oauth_subscription_base_url
            if self.uses_oauth
            else settings.grok_base_url.rstrip("/")
        )

    def _headers(self, token: str, *, streaming: bool = False) -> Dict[str, str]:
        if self.uses_oauth:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                **CLIENT_IDENTITY_HEADERS,
            }
            if streaming:
                headers["Accept"] = "text/event-stream"
            return headers
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── transport seams (the only outbound edges, mockable in tests) ──

    def _client(self, *, timeout: float = GENERATE_TIMEOUT) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)
        )

    def _async_client(self, *, timeout: float = GENERATE_TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)
        )

    # ── payload shapes ───────────────────────────────────────

    def _resolve_model(self, model_name: Optional[str] = None) -> str:
        return model_name or self.default_model

    def _messages(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        for message in messages:
            result.append(
                {"role": message.get("role", "user"), "content": message.get("content", "")}
            )
        return result

    def _chat_payload(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model: str,
        *,
        stream: bool,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """OpenAI chat-completions body (API-key mode)."""
        payload: Dict[str, Any] = {
            "model": model,
            "messages": self._messages(messages, system_prompt),
            "max_tokens": max_tokens or settings.grok_max_tokens,
            "temperature": (
                settings.grok_temperature if temperature is None else temperature
            ),
        }
        if stream:
            payload["stream"] = True
        return payload

    def _responses_payload(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model: str,
        *,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """OpenAI Responses body (subscription mode).

        The proxy takes ``instructions`` for the system prompt and
        ``max_output_tokens``; it rejects ``max_tokens``, ``service_tier``, and
        ``reasoning`` on several models, so none are sent. Non-text content
        parts are dropped (with a warning, not in silence): image input is not
        verified on this transport, and a chat that quietly ignored an attached
        picture would be worse than one that says it used only the text.
        """
        input_items: List[Dict[str, Any]] = []
        instructions = system_prompt or ""
        for message in messages:
            role = message.get("role", "user")
            if role == "system":
                instructions = (
                    f"{instructions}\n\n{message.get('content', '')}".strip()
                )
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                part_type = "input_text" if role == "user" else "output_text"
                parts = [
                    {"type": part_type, "text": str(part.get("text", ""))}
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                dropped = len(content) - len(parts)
                if dropped:
                    logger.warning(
                        "Grok subscription transport sent %d text part(s) and "
                        "dropped %d non-text part(s) from one message",
                        len(parts),
                        dropped,
                    )
                content_parts = parts or [{"type": part_type, "text": ""}]
            else:
                content_parts = [
                    {
                        "type": "input_text" if role == "user" else "output_text",
                        "text": str(content),
                    }
                ]
            input_items.append(
                {"role": "assistant" if role == "assistant" else "user",
                 "content": content_parts}
            )
        payload: Dict[str, Any] = {
            "model": model,
            "input": input_items,
            "stream": True,
            "max_output_tokens": settings.grok_max_tokens,
            "temperature": (
                settings.grok_temperature if temperature is None else temperature
            ),
        }
        if instructions:
            payload["instructions"] = instructions
        return payload

    # ── response parsing ─────────────────────────────────────

    @staticmethod
    def _error_for_status(status: int) -> str:
        if status in (401, 403):
            return _SUBSCRIPTION_MESSAGE if status == 403 else _REAUTH_MESSAGE
        if status == 402:
            return _SUBSCRIPTION_MESSAGE
        if status == 429:
            return _RATE_LIMIT_MESSAGE
        return f"Grok returned HTTP {status}."

    @staticmethod
    def _detail(response: httpx.Response, limit: int = 300) -> str:
        if response.status_code in _REDACTED_STATUSES:
            return GrokProvider._error_for_status(response.status_code)
        text = response.text or ""
        return text[:limit]

    @staticmethod
    def _completion_text(data: Dict[str, Any]) -> str:
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        return (message.get("content") or "").strip()

    @staticmethod
    def _responses_text(data: Dict[str, Any]) -> str:
        """Concatenate the output text of a completed Responses object."""
        response = data.get("response") or data
        chunks: List[str] = []
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        if chunks:
            return "".join(chunks).strip()
        output = response.get("output_text")
        if isinstance(output, str):
            return output.strip()
        return ""

    # ── synchronous generation ───────────────────────────────

    def generate_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> str:
        return self._generate(
            messages, system_prompt, model_name, json_mode=False
        )

    def generate_json_response(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> str:
        json_system = (
            (system_prompt + "\n\n" if system_prompt else "")
            + "You MUST respond with valid JSON only. No markdown, no explanation."
        )
        return self._generate(messages, json_system, model_name, json_mode=True)

    def _generate(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model_name: Optional[str],
        *,
        json_mode: bool,
    ) -> str:
        model = self._resolve_model(model_name)
        temperature = 0.1 if json_mode else None

        if self.uses_oauth:
            return self._generate_via_responses(
                messages, system_prompt, model, temperature=temperature
            )

        api_key = self._api_key_or_config()
        if not api_key:
            raise RuntimeError(self._keyless())
        payload = self._chat_payload(
            messages,
            system_prompt,
            model,
            stream=False,
            temperature=temperature,
            max_tokens=512 if json_mode else None,
        )
        data = self._post_json(
            f"{self._base_url()}/chat/completions",
            self._headers(api_key),
            payload,
            provider_label="Grok",
        )
        return self._completion_text(data)

    def _post_json(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        *,
        provider_label: str,
    ) -> Dict[str, Any]:
        """One blocking POST with the repo's provider error taxonomy."""
        try:
            with self._client() as client:
                response = client.post(url, headers=headers, json=payload)
        except httpx.ConnectError as exc:
            raise ConnectionError(
                "Cannot connect to Grok. Check your internet connection."
            ) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                "Grok did not respond in time. The request may be too long."
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError("Grok request failed.") from exc

        if response.status_code != 200:
            detail = self._detail(response)
            raise RuntimeError(f"{provider_label} returned HTTP {response.status_code}: {detail}")
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"{provider_label} returned an unreadable response.") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"{provider_label} returned an unexpected response shape.")
        return data

    def _generate_via_responses(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model: str,
        *,
        temperature: Optional[float] = None,
    ) -> str:
        """Blocking generation over the subscription proxy (Responses API)."""
        url = f"{self._base_url()}/responses"
        payload = self._responses_payload(
            messages, system_prompt, model, temperature=temperature
        )
        return self._responses_attempt(url, payload, retried=False)

    def _responses_attempt(
        self, url: str, payload: Dict[str, Any], *, retried: bool
    ) -> str:
        """POST to the proxy once; a 401 forces one refresh and one retry.

        The retry exists because a token can expire between the store's skew
        check and the proxy's clock.
        """
        token = self._access_token()
        try:
            with self._client() as client:
                response = client.post(url, headers=self._headers(token), json=payload)
        except httpx.ConnectError as exc:
            raise ConnectionError(
                "Cannot connect to Grok. Check your internet connection."
            ) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                "Grok did not respond in time. The request may be too long."
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError("Grok request failed.") from exc

        if response.status_code == 401 and not retried:
            logger.info("Grok rejected the access token; refreshing once")
            self._store.refresh_now(
                self._user_id, GROK, service=self._oauth_service
            )
            return self._responses_attempt(url, payload, retried=True)
        if response.status_code != 200:
            detail = self._detail(response)
            raise RuntimeError(
                f"Grok returned HTTP {response.status_code}: {detail}"
            )
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("Grok returned an unreadable response.") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Grok returned an unexpected response shape.")
        return self._responses_text(data)

    # ── async streaming ──────────────────────────────────────

    async def generate_stream_async(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        model = self._resolve_model(model_name)
        if self.uses_oauth:
            async for event in self._stream_responses(
                self._responses_payload(messages, system_prompt, model)
            ):
                yield event
            return

        api_key = self._api_key_or_config()
        if not api_key:
            yield {"type": "error", "error": self._keyless()}
            return
        payload = self._chat_payload(messages, system_prompt, model, stream=True)
        async for event in self._stream_chat_completions(
            f"{self._base_url()}/chat/completions", self._headers(api_key), payload
        ):
            yield event

    async def _stream_chat_completions(
        self, url: str, headers: Dict[str, str], payload: Dict[str, Any]
    ) -> AsyncGenerator[Dict[str, Any], None]:
        try:
            async with self._async_client() as client:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        status = response.status_code
                        detail = (
                            self._error_for_status(status)
                            if status in _REDACTED_STATUSES
                            else body.decode("utf-8", errors="replace")[:300]
                        )
                        yield {
                            "type": "error",
                            "error": f"Grok returned HTTP {status}: {detail}",
                        }
                        return
                    collected: List[str] = []
                    async for line in response.aiter_lines():
                        event = _sse_data(line)
                        if event is _SSE_DONE:
                            yield {"type": "done", "response": "".join(collected)}
                            return
                        if event is None:
                            continue
                        try:
                            chunk = json.loads(event)
                        except json.JSONDecodeError:
                            continue
                        delta = (chunk.get("choices") or [{}])[0].get("delta", {}).get(
                            "content", ""
                        )
                        if delta:
                            collected.append(delta)
                            yield {"type": "token", "token": delta}
                    yield {"type": "done", "response": "".join(collected)}
        except httpx.ConnectError:
            yield {
                "type": "error",
                "error": "Cannot connect to Grok. Check your internet connection.",
            }
        except httpx.TimeoutException:
            yield {"type": "error", "error": "Grok did not respond in time."}
        except httpx.RequestError:
            yield {"type": "error", "error": "Grok request failed."}
        except RuntimeError as exc:
            yield {"type": "error", "error": str(exc)}

    async def _stream_responses(
        self, payload: Dict[str, Any], *, retried: bool = False
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream from the subscription proxy, refreshing once on a 401."""
        url = f"{self._base_url()}/responses"
        try:
            token = await self._access_token_async()
        except RuntimeError as exc:
            yield {"type": "error", "error": str(exc)}
            return

        try:
            async with self._async_client() as client:
                async with client.stream(
                    "POST", url, headers=self._headers(token, streaming=True), json=payload
                ) as response:
                    if response.status_code == 401 and not retried:
                        await response.aread()
                        logger.info(
                            "Grok rejected the streamed request's token; refreshing once"
                        )
                        try:
                            # Forced, not skew-based: the proxy said this token
                            # is dead even if its own clock still calls it valid.
                            await self._store.refresh_now_async(
                                self._user_id, GROK, service=self._oauth_service
                            )
                        except TokenRefreshError as exc:
                            yield {"type": "error", "error": str(exc)}
                            return
                        async for event in self._stream_responses(payload, retried=True):
                            yield event
                        return
                    if response.status_code != 200:
                        body = await response.aread()
                        status = response.status_code
                        detail = (
                            self._error_for_status(status)
                            if status in _REDACTED_STATUSES
                            else body.decode("utf-8", errors="replace")[:300]
                        )
                        yield {
                            "type": "error",
                            "error": f"Grok returned HTTP {status}: {detail}",
                        }
                        return

                    collected: List[str] = []
                    completed: Optional[str] = None
                    async for line in response.aiter_lines():
                        event = _sse_data(line)
                        if event is _SSE_DONE:
                            break
                        if event is None:
                            continue
                        try:
                            data = json.loads(event)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        kind = data.get("type")
                        if kind == "response.output_text.delta":
                            delta = data.get("delta") or ""
                            if delta:
                                collected.append(delta)
                                yield {"type": "token", "token": delta}
                        elif kind == "response.completed":
                            completed = self._responses_text(data)
                        elif kind in ("response.failed", "response.incomplete"):
                            logger.info("Grok response ended as %s", kind)
                            yield {
                                "type": "error",
                                "error": "Grok stopped generating early. Try again.",
                            }
                            return
                        elif kind == "error":
                            logger.info("Grok stream reported an error event")
                            yield {
                                "type": "error",
                                "error": "Grok could not complete the request.",
                            }
                            return

                    text = completed if completed else "".join(collected)
                    yield {"type": "done", "response": text}
        except httpx.ConnectError:
            yield {
                "type": "error",
                "error": "Cannot connect to Grok. Check your internet connection.",
            }
        except httpx.TimeoutException:
            yield {"type": "error", "error": "Grok did not respond in time."}
        except httpx.RequestError:
            yield {"type": "error", "error": "Grok request failed."}
        except TokenRefreshError as exc:
            yield {"type": "error", "error": str(exc)}

    # ── model discovery ──────────────────────────────────────

    def fetch_available_chat_models(self) -> Optional[List[str]]:
        """Models this account may actually use.

        OAuth mode reads the authenticated catalog: presence there *is* the
        entitlement signal, because xAI publishes no subscription-tier claim.
        Returns None when there is no credential to ask with (fail-closed, the
        same contract every other provider follows).
        """
        if self.uses_oauth:
            try:
                token = self._access_token()
            except RuntimeError as exc:
                logger.info("Grok catalog skipped: %s", exc)
                return None
            return self._fetch_catalog(
                f"{self._base_url()}/models-v2", self._headers(token)
            )
        api_key = self._api_key_or_config()
        if not api_key:
            return None
        return self._fetch_catalog(f"{self._base_url()}/models", self._headers(api_key))

    def _fetch_catalog(self, url: str, headers: Dict[str, str]) -> List[str]:
        request_headers = {
            key: value for key, value in headers.items() if key != "Content-Type"
        }
        try:
            with self._client(timeout=CATALOG_TIMEOUT) as client:
                response = client.get(url, headers=request_headers)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Grok model catalog fetch failed (%s)", type(exc).__name__)
            return []
        if response.status_code != 200:
            logger.error("Grok catalog returned HTTP %s", response.status_code)
            return []
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            logger.error("Grok catalog response was not JSON.")
            return []
        return _catalog_ids(data)

    def health_check(self) -> bool:
        """Reachable and entitled: the catalog must answer for the credential."""
        return bool(self.fetch_available_chat_models())


# ── SSE helpers ──────────────────────────────────────────────

_SSE_DONE = object()


def _sse_data(line: str) -> Optional[Any]:
    """The payload of an SSE ``data:`` line, [DONE] sentinel, or None."""
    text = (line or "").strip()
    if not text or text.startswith(":"):
        return None
    if not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if payload == "[DONE]":
        return _SSE_DONE
    return payload or None


def _catalog_ids(data: Any) -> List[str]:
    """Model ids from a Grok catalog response, defensively.

    The proxy wraps its list differently from the developer API (``data`` vs
    ``models`` vs a bare array), and the shape is not a published contract.
    """
    if isinstance(data, dict):
        entries = data.get("data") or data.get("models") or data.get("items") or []
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
    ids: List[str] = []
    for entry in entries:
        if isinstance(entry, str):
            candidate = entry
        elif isinstance(entry, dict):
            candidate = str(entry.get("id") or entry.get("name") or entry.get("model") or "")
        else:
            continue
        candidate = candidate.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if candidate in NOT_SUBSCRIPTION_MODELS:
            continue
        if any(hint in lowered for hint in _NON_CHAT_HINTS):
            continue
        if candidate not in ids:
            ids.append(candidate)
    ids.sort()
    return ids
