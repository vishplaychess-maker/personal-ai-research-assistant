"""
Media Generation Service for Thunder AI.

Provides image and video generation using multiple providers with fallback:
- Venice AI (primary): OpenAI-compatible, supports flux models and seedance video
- NexaAPI (fallback): OpenAI-compatible, free $5 credits
- Agnes AI (fallback): OpenAI-compatible, free tier for flash models

All providers use OpenAI-compatible APIs for consistency.
"""

import asyncio
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class GenerationResult:
    """Result of a media generation request."""
    success: bool
    url: Optional[str] = None
    task_id: Optional[str] = None
    error: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None


class MediaProvider(ABC):
    """Abstract base class for media generation providers."""

    def __init__(self, name: str, base_url: str, api_key_env: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env

    def _get_api_key(self) -> str:
        """Get API key from environment (supports both os.environ and settings)."""
        # First check os.environ (live process env, including Docker secrets)
        key = os.environ.get(self.api_key_env, "")
        if key:
            return key
        # Fall back to pydantic settings (covers .env file)
        return getattr(settings, f"{self.name}_api_key", "") or ""

    def _get_headers(self) -> dict:
        """Get request headers with authentication."""
        api_key = self._get_api_key()
        if not api_key:
            raise RuntimeError(f"No API key configured for {self.name} ({self.api_key_env})")
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @abstractmethod
    async def generate_image(
        self,
        prompt: str,
        model: Optional[str] = None,
        width: int = 1024,
        height: int = 1024,
    ) -> GenerationResult:
        """Generate an image. Returns URL or base64 data."""
        pass

    @abstractmethod
    async def generate_video(
        self,
        prompt: str,
        model: Optional[str] = None,
        duration: str = "5s",
    ) -> GenerationResult:
        """Generate a video. Returns task_id for async providers or URL for sync."""
        pass

    @abstractmethod
    async def poll_video(self, task_id: str) -> GenerationResult:
        """Poll video generation status. Returns final result when complete."""
        pass


class VeniceProvider(MediaProvider):
    """Venice AI provider - OpenAI-compatible API."""

    def __init__(self):
        super().__init__("venice", settings.venice_base_url, "VENICE_API_KEY")

    async def generate_image(
        self,
        prompt: str,
        model: Optional[str] = None,
        width: int = 1024,
        height: int = 1024,
    ) -> GenerationResult:
        model = model or settings.venice_image_model
        url = f"{self.base_url}/images/generations"
        payload = {
            "prompt": prompt,
            "model": model,
            "n": 1,
            "size": f"{width}x{height}",
            "response_format": "url",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
                image_url = data.get("data", [{}])[0].get("url")
                if not image_url:
                    return GenerationResult(success=False, error="No image URL in response", provider=self.name, model=model)
                return GenerationResult(success=True, url=image_url, provider=self.name, model=model)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name, model=model)
        except Exception as e:
            logger.warning("Venice image generation failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name, model=model)

    async def generate_video(
        self,
        prompt: str,
        model: Optional[str] = None,
        duration: str = "5s",
    ) -> GenerationResult:
        model = model or settings.venice_video_model
        # Venice uses async queue: POST /video/queue -> returns queue_id
        url = f"{self.base_url}/video/queue"
        payload = {
            "prompt": prompt,
            "model": model,
            "duration": duration,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("queue_id") or data.get("id") or data.get("task_id")
                if not task_id:
                    return GenerationResult(success=False, error="No task_id in Venice video queue response", provider=self.name, model=model)
                return GenerationResult(success=True, task_id=task_id, provider=self.name, model=model)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name, model=model)
        except Exception as e:
            logger.warning("Venice video queue failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name, model=model)

    async def poll_video(self, task_id: str) -> GenerationResult:
        # Venice uses GET /video/retrieve?queue_id=...
        url = f"{self.base_url}/video/retrieve"
        params = {"queue_id": task_id}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=self._get_headers(), params=params)
                resp.raise_for_status()
                data = resp.json()
                status_val = data.get("status", "unknown")
                if status_val == "completed":
                    video_url = data.get("video_url") or data.get("url") or data.get("download_url")
                    if not video_url:
                        return GenerationResult(success=False, error="No video URL in completed response", provider=self.name)
                    return GenerationResult(success=True, url=video_url, provider=self.name)
                elif status_val in ("failed", "error"):
                    return GenerationResult(success=False, error=data.get("error", "Video generation failed"), provider=self.name)
                else:
                    # Still processing
                    return GenerationResult(success=True, task_id=task_id, provider=self.name)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name)
        except Exception as e:
            logger.warning("Venice video poll failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name)


class NexaProvider(MediaProvider):
    """NexaAPI provider - OpenAI-compatible, free $5 credits."""

    def __init__(self):
        super().__init__("nexa", settings.nexa_base_url, "NEXA_API_KEY")

    async def generate_image(
        self,
        prompt: str,
        model: Optional[str] = None,
        width: int = 1024,
        height: int = 1024,
    ) -> GenerationResult:
        model = model or settings.nexa_image_model
        url = f"{self.base_url}/images/generations"
        payload = {
            "prompt": prompt,
            "model": model,
            "n": 1,
            "size": f"{width}x{height}",
            "response_format": "url",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
                image_url = data.get("data", [{}])[0].get("url")
                if not image_url:
                    return GenerationResult(success=False, error="No image URL in response", provider=self.name, model=model)
                return GenerationResult(success=True, url=image_url, provider=self.name, model=model)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name, model=model)
        except Exception as e:
            logger.warning("Nexa image generation failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name, model=model)

    async def generate_video(
        self,
        prompt: str,
        model: Optional[str] = None,
        duration: str = "5s",
    ) -> GenerationResult:
        model = model or settings.nexa_video_model
        # Nexa uses async generation similar to Venice
        url = f"{self.base_url}/video/generations"
        payload = {
            "prompt": prompt,
            "model": model,
            "duration": duration,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
                # Try multiple possible task_id fields
                task_id = data.get("id") or data.get("task_id") or data.get("generation_id")
                if not task_id:
                    return GenerationResult(success=False, error="No task_id in Nexa video response", provider=self.name, model=model)
                return GenerationResult(success=True, task_id=task_id, provider=self.name, model=model)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name, model=model)
        except Exception as e:
            logger.warning("Nexa video generation failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name, model=model)

    async def poll_video(self, task_id: str) -> GenerationResult:
        # Nexa typically uses GET /video/generations/{id}
        url = f"{self.base_url}/video/generations/{task_id}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                resp.raise_for_status()
                data = resp.json()
                status_val = data.get("status", "unknown")
                if status_val in ("completed", "succeeded"):
                    video_url = data.get("video_url") or data.get("url") or data.get("download_url")
                    if not video_url:
                        return GenerationResult(success=False, error="No video URL in completed response", provider=self.name)
                    return GenerationResult(success=True, url=video_url, provider=self.name)
                elif status_val in ("failed", "error"):
                    return GenerationResult(success=False, error=data.get("error", "Video generation failed"), provider=self.name)
                else:
                    return GenerationResult(success=True, task_id=task_id, provider=self.name)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name)
        except Exception as e:
            logger.warning("Nexa video poll failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name)


class AgnesProvider(MediaProvider):
    """Agnes AI provider - OpenAI-compatible, free tier available."""

    def __init__(self):
        super().__init__("agnes", settings.agnes_base_url, "AGNES_API_KEY")

    async def generate_image(
        self,
        prompt: str,
        model: Optional[str] = None,
        width: int = 1024,
        height: int = 1024,
    ) -> GenerationResult:
        model = model or settings.agnes_image_model
        url = f"{self.base_url}/images/generations"
        payload = {
            "prompt": prompt,
            "model": model,
            "n": 1,
            "size": f"{width}x{height}",
            "response_format": "url",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
                image_url = data.get("data", [{}])[0].get("url")
                if not image_url:
                    return GenerationResult(success=False, error="No image URL in response", provider=self.name, model=model)
                return GenerationResult(success=True, url=image_url, provider=self.name, model=model)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name, model=model)
        except Exception as e:
            logger.warning("Agnes image generation failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name, model=model)

    async def generate_video(
        self,
        prompt: str,
        model: Optional[str] = None,
        duration: str = "5s",
    ) -> GenerationResult:
        model = model or settings.agnes_video_model
        # Agnes uses async video generation
        url = f"{self.base_url}/video/generations"
        payload = {
            "prompt": prompt,
            "model": model,
            "duration": duration,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
                task_id = data.get("id") or data.get("task_id") or data.get("generation_id")
                if not task_id:
                    return GenerationResult(success=False, error="No task_id in Agnes video response", provider=self.name, model=model)
                return GenerationResult(success=True, task_id=task_id, provider=self.name, model=model)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name, model=model)
        except Exception as e:
            logger.warning("Agnes video generation failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name, model=model)

    async def poll_video(self, task_id: str) -> GenerationResult:
        url = f"{self.base_url}/video/generations/{task_id}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                resp.raise_for_status()
                data = resp.json()
                status_val = data.get("status", "unknown")
                if status_val in ("completed", "succeeded"):
                    video_url = data.get("video_url") or data.get("url") or data.get("download_url")
                    if not video_url:
                        return GenerationResult(success=False, error="No video URL in completed response", provider=self.name)
                    return GenerationResult(success=True, url=video_url, provider=self.name)
                elif status_val in ("failed", "error"):
                    return GenerationResult(success=False, error=data.get("error", "Video generation failed"), provider=self.name)
                else:
                    return GenerationResult(success=True, task_id=task_id, provider=self.name)
        except httpx.HTTPStatusError as e:
            return GenerationResult(success=False, error=f"HTTP {e.response.status_code}: {e.response.text}", provider=self.name)
        except Exception as e:
            logger.warning("Agnes video poll failed: %s", e)
            return GenerationResult(success=False, error=str(e), provider=self.name)


class MediaGenerationService:
    """Orchestrates media generation with provider fallback."""

    def __init__(self):
        self.providers: dict[str, MediaProvider] = {
            "venice": VeniceProvider(),
            "nexa": NexaProvider(),
            "agnes": AgnesProvider(),
        }
        # Fallback order
        self.fallback_order = ["venice", "nexa", "agnes"]

    def _get_provider_order(self, preferred: Optional[str] = None) -> list[str]:
        """Get ordered list of providers to try."""
        if preferred and preferred in self.providers:
            # Put preferred first, then fallback order
            order = [preferred]
            for p in self.fallback_order:
                if p != preferred:
                    order.append(p)
            return order
        return self.fallback_order

    def get_available_providers(self) -> list[str]:
        """Return list of providers that have API keys configured."""
        available = []
        for name, provider in self.providers.items():
            if provider._get_api_key():
                available.append(name)
        return available

    async def generate_image(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        width: int = 1024,
        height: int = 1024,
    ) -> GenerationResult:
        """Generate image with fallback across providers."""
        # Sanitize prompt
        prompt = self._sanitize_prompt(prompt)
        if not prompt:
            return GenerationResult(success=False, error="Empty prompt after sanitization")

        order = self._get_provider_order(provider)
        last_error = None

        for name in order:
            prov = self.providers[name]
            if not prov._get_api_key():
                logger.debug("Skipping %s: no API key configured", name)
                continue
            logger.info("Trying image generation with %s (%s)", name, model or "default")
            result = await prov.generate_image(prompt, model, width, height)
            if result.success:
                logger.info("Image generated successfully with %s", name)
                return result
            last_error = result.error
            logger.warning("%s image generation failed: %s", name, result.error)

        return GenerationResult(success=False, error=f"All providers failed. Last error: {last_error}")

    async def generate_video(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        duration: str = "5s",
    ) -> GenerationResult:
        """Generate video with fallback across providers."""
        prompt = self._sanitize_prompt(prompt)
        if not prompt:
            return GenerationResult(success=False, error="Empty prompt after sanitization")

        order = self._get_provider_order(provider)
        last_error = None

        for name in order:
            prov = self.providers[name]
            if not prov._get_api_key():
                logger.debug("Skipping %s: no API key configured", name)
                continue
            logger.info("Trying video generation with %s (%s)", name, model or "default")
            result = await prov.generate_video(prompt, model, duration)
            if result.success:
                logger.info("Video generation started with %s (task_id: %s)", name, result.task_id)
                return result
            last_error = result.error
            logger.warning("%s video generation failed: %s", name, result.error)

        return GenerationResult(success=False, error=f"All providers failed. Last error: {last_error}")

    async def poll_video(self, task_id: str, provider: str) -> GenerationResult:
        """Poll video generation status for a specific provider."""
        prov = self.providers.get(provider)
        if not prov:
            return GenerationResult(success=False, error=f"Unknown provider: {provider}")
        return await prov.poll_video(task_id)

    async def wait_for_video(
        self,
        task_id: str,
        provider: str,
        timeout_s: Optional[int] = None,
        poll_interval_s: Optional[int] = None,
    ) -> GenerationResult:
        """Wait for video generation to complete with polling."""
        timeout_s = timeout_s or settings.media_video_timeout_s
        poll_interval_s = poll_interval_s or settings.media_video_poll_interval_s

        prov = self.providers.get(provider)
        if not prov:
            return GenerationResult(success=False, error=f"Unknown provider: {provider}")

        start = asyncio.get_event_loop().time()
        while True:
            result = await prov.poll_video(task_id)
            if result.success and result.url:
                return result
            if result.success and not result.url:
                # Still processing
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed >= timeout_s:
                    return GenerationResult(success=False, error="Video generation timeout", provider=provider, task_id=task_id)
                await asyncio.sleep(poll_interval_s)
                continue
            # Error
            return result

    def _sanitize_prompt(self, prompt: str) -> str:
        """Sanitize prompt to prevent injection attacks."""
        if not prompt:
            return ""
        # Remove potential instruction injection patterns
        forbidden = [
            "ignore previous",
            "ignore all",
            "system:",
            "assistant:",
            "you are",
            "act as",
            "pretend to",
            "roleplay",
            "<|",
            "|>",
            "[INST]",
            "[/INST]",
            "<<SYS>>",
            "<</SYS>>",
        ]
        sanitized = prompt
        for pattern in forbidden:
            # Case-insensitive: a lowercase-only filter is trivially bypassed
            # with "System:" / "IGNORE PREVIOUS".
            sanitized = re.sub(re.escape(pattern), "", sanitized, flags=re.IGNORECASE)
        # Limit length
        return sanitized[:4000].strip()


# Singleton instance
_media_service: Optional[MediaGenerationService] = None


def get_media_service() -> MediaGenerationService:
    """Get the global media generation service instance."""
    global _media_service
    if _media_service is None:
        _media_service = MediaGenerationService()
    return _media_service