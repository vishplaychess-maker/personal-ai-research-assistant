"""
LLM Provider Factory.

Selects the appropriate provider based on the LLM_PROVIDER config setting
and exposes a module-level singleton so the rest of the application can
simply import ``get_provider()`` or ``llm``.

Usage:
    from app.services.llm_providers import get_provider, llm

    # Module-level singleton (lazy-initialized)
    response = llm.generate_response(messages, system_prompt="...")

    # Or get a fresh instance
    provider = get_provider()
    response = provider.generate_response(messages)
"""

import logging
from typing import Optional

from app.config import settings
from app.services.llm_providers.base import LLMProvider

logger = logging.getLogger(__name__)

# ── Lazy singleton ────────────────────────────────────────

_provider_instance: Optional[LLMProvider] = None


def get_provider() -> LLMProvider:
    """
    Return the configured LLM provider instance.

    Creates a new instance on first call, then caches it.
    Reads ``settings.llm_provider`` each time to allow runtime changes
    (useful for tests), but the singleton is reused for performance.
    """
    global _provider_instance
    provider_name = settings.llm_provider.lower().strip()

    # If provider changed, recreate
    if _provider_instance is not None:
        expected = _provider_instance.name.lower()
        alias_map = {
            "ollama": "ollama",
            "openrouter": "openrouter",
            "nvidia": "nvidia nim",
        }
        if alias_map.get(provider_name, provider_name) != expected:
            _provider_instance = None

    if _provider_instance is None:
        if provider_name == "ollama":
            from app.services.llm_providers.ollama_provider import OllamaProvider
            _provider_instance = OllamaProvider()
        elif provider_name == "openrouter":
            from app.services.llm_providers.openrouter_provider import OpenRouterProvider
            _provider_instance = OpenRouterProvider()
        elif provider_name == "nvidia":
            from app.services.llm_providers.nvidia_provider import NvidiaProvider
            _provider_instance = NvidiaProvider()
        else:
            logger.warning(
                "Unknown LLM_PROVIDER '%s', falling back to Ollama",
                provider_name,
            )
            from app.services.llm_providers.ollama_provider import OllamaProvider
            _provider_instance = OllamaProvider()

        logger.info("LLM provider initialized: %s", _provider_instance.name)

    return _provider_instance


def reset_provider() -> None:
    """Reset the cached provider (useful for testing)."""
    global _provider_instance
    _provider_instance = None


# Module-level convenience alias
llm = get_provider()
