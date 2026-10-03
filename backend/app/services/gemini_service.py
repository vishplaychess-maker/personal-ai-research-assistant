"""Gemini API service — primary chat + vision (native google-generativeai SDK).

This is the single Gemini integration point for BOTH chat completions and image
analysis, selected when settings.llm_provider == "gemini". It is distinct from
the OpenAI-compatible google_* provider: here we drive the native
google-generativeai SDK directly.

Fail-closed (HARD invariant)
----------------------------
Every failure — missing SDK, missing key, API error, empty/blocked response —
becomes a bounded GeminiAPIError. Callers convert that into a bounded notice;
the chat and vision paths never raise into a request.

Privacy (HARD invariant)
------------------------
Message content, image bytes and the generated text are NEVER logged. Error
paths log only type(exc).__name__.

Lazy import
-----------
google.generativeai is an OPTIONAL dependency, imported only inside the call
methods, so the application (and the Ollama fallback) runs without it.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_VISION_PROMPT = "Describe this image factually in up to 150 words."


class GeminiAPIError(Exception):
    """A Gemini call failed. Carries NO API text, key or content.

    code is a bounded slug: "gemini_sdk_missing", "gemini_not_configured",
    "gemini_empty_response" or "gemini_error".
    """

    def __init__(self, code: str = "gemini_error") -> None:
        super().__init__(code)
        self.code = code


def _decode_data_url(data_url: str) -> Optional[Dict[str, Any]]:
    """Decode a data:image/...;base64,... URL into a Gemini inline_data part."""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    header, sep, payload = data_url.partition(",")
    if not sep or ";base64" not in header:
        return None
    mime = header[len("data:"):].split(";", 1)[0] or "image/jpeg"
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    return {"mime_type": mime, "data": raw}


class GeminiService:
    """Thin, fail-closed wrapper around google-generativeai."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 vision_model: Optional[str] = None, timeout: Optional[float] = None,
                 max_tokens: Optional[int] = None) -> None:
        self.api_key = api_key if api_key is not None else getattr(settings, "gemini_api_key", "")
        self.model = model or getattr(settings, "gemini_model", "gemini-1.5-flash")
        self.vision_model = vision_model or getattr(settings, "gemini_vision_model", self.model)
        try:
            self.timeout = float(timeout if timeout is not None
                                 else getattr(settings, "gemini_timeout_s", 30.0))
        except (TypeError, ValueError):
            self.timeout = 30.0
        try:
            self.max_tokens = int(max_tokens if max_tokens is not None
                                  else getattr(settings, "gemini_max_tokens", 2048))
        except (TypeError, ValueError):
            self.max_tokens = 2048

    def _client(self):
        """Import + configure the SDK, or raise a bounded error. The import is
        LAZY so the application runs without the optional dependency."""
        try:
            import google.generativeai as genai
        except Exception:  # noqa: BLE001 - optional dependency
            logger.warning("gemini: google-generativeai is not installed")
            raise GeminiAPIError("gemini_sdk_missing") from None
        if not self.api_key:
            raise GeminiAPIError("gemini_not_configured")
        try:
            genai.configure(api_key=self.api_key)
        except Exception as exc:  # noqa: BLE001 - never leak key/API text
            logger.warning("gemini: configure failed (%s)", type(exc).__name__)
            raise GeminiAPIError("gemini_not_configured") from None
        return genai

    @staticmethod
    def _to_parts(content: Any) -> List[Dict[str, Any]]:
        """Convert one OpenAI-style message content into Gemini parts."""
        if isinstance(content, str):
            return [{"text": content}] if content else []
        if not isinstance(content, list):
            return []
        parts: List[Dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                if item["text"]:
                    parts.append({"text": item["text"]})
            elif item.get("type") == "image_url":
                inline = _decode_data_url(
                    ((item.get("image_url") or {}).get("url") or ""))
                if inline:
                    parts.append(inline)
        return parts

    @classmethod
    def _to_gemini_contents(cls, messages: List[Dict[str, Any]],
                            system_prompt: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
        """(system_text, contents) in the native Gemini shape.

        Roles: system -> system instruction, user -> "user", assistant -> "model".
        """
        system_parts: List[str] = []
        if isinstance(system_prompt, str) and system_prompt:
            system_parts.append(system_prompt)
        contents: List[Dict[str, Any]] = []
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "user").strip().lower()
            content = msg.get("content", "")
            if role == "system":
                if isinstance(content, str) and content:
                    system_parts.append(content)
                continue
            g_role = "model" if role in ("assistant", "model") else "user"
            parts = cls._to_parts(content)
            if parts:
                contents.append({"role": g_role, "parts": parts})
        return "\n\n".join(system_parts), contents

    @staticmethod
    def _extract_text(response: Any) -> str:
        try:
            text = getattr(response, "text", None)
        except Exception:  # noqa: BLE001 - .text raises on a blocked/empty response
            text = None
        return text.strip() if isinstance(text, str) else ""

    def chat_completion(self, messages: List[Dict[str, Any]]) -> str:
        """Chat completion. Raises GeminiAPIError (bounded) on any failure."""
        try:
            genai = self._client()
            system_text, contents = self._to_gemini_contents(messages)
            kwargs: Dict[str, Any] = {
                "generation_config": {"max_output_tokens": self.max_tokens}}
            if system_text:
                kwargs["system_instruction"] = system_text
            model = genai.GenerativeModel(self.model, **kwargs)
            response = model.generate_content(
                contents, request_options={"timeout": self.timeout})
            text = self._extract_text(response)
            if not text:
                raise GeminiAPIError("gemini_empty_response")
            return text
        except GeminiAPIError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak API text
            logger.warning("gemini: chat_completion failed (%s)", type(exc).__name__)
            raise GeminiAPIError("gemini_error") from None

    def vision_describe(self, image_bytes: bytes,
                        prompt: str = DEFAULT_VISION_PROMPT) -> str:
        """Describe an image. Raises GeminiAPIError (bounded) on any failure."""
        try:
            genai = self._client()
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes))
            image.load()
            model = genai.GenerativeModel(
                self.vision_model,
                generation_config={"max_output_tokens": self.max_tokens})
            response = model.generate_content(
                [prompt, image], request_options={"timeout": self.timeout})
            text = self._extract_text(response)
            if not text:
                raise GeminiAPIError("gemini_empty_response")
            return text
        except GeminiAPIError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak API text / image bytes
            logger.warning("gemini: vision_describe failed (%s)", type(exc).__name__)
            raise GeminiAPIError("gemini_error") from None


__all__ = ["GeminiService", "GeminiAPIError", "DEFAULT_VISION_PROMPT"]
