"""
LLM Provider Factory.

Selects the appropriate provider based on the LLM_PROVIDER config setting
(or per-user settings) and exposes a module-level singleton so the rest of
the application can simply import ``get_provider()`` or ``llm``.

Usage:
    from app.services.llm_providers import get_provider, llm

    # Module-level singleton (lazy-initialized, global .env config)
    response = llm.generate_response(messages, system_prompt="...")

    # Per-user provider (built fresh from stored settings)
    provider = get_provider(
        config={"provider": "openrouter", "api_key": "...", "model": "..."}
    )
"""

import logging
from typing import Optional

from app.config import settings
from app.services.llm_providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Lazy singleton
_provider_instance: Optional[LLMProvider] = None


def _build_provider(
    provider_name: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> LLMProvider:
    """Instantiate a provider by name, with optional per-user overrides."""
    if provider_name == "ollama":
        from app.services.llm_providers.ollama_provider import OllamaProvider
        return OllamaProvider(model=model)
    elif provider_name == "openrouter":
        from app.services.llm_providers.openrouter_provider import OpenRouterProvider
        return OpenRouterProvider(api_key=api_key, model=model)
    elif provider_name == "nvidia":
        from app.services.llm_providers.nvidia_provider import NvidiaProvider
        return NvidiaProvider(api_key=api_key, model=model)
    else:
        logger.warning(
            "Unknown LLM provider '%s', falling back to Ollama", provider_name
        )
        from app.services.llm_providers.ollama_provider import OllamaProvider
        return OllamaProvider(model=model)


def get_provider(config: Optional[dict] = None) -> LLMProvider:
    """Return an LLM provider instance.

    When ``config`` is provided (a dict with optional keys ``provider``,
    ``api_key``, ``model``), a FRESH provider instance is built with those
    overrides (not cached) so per-user settings take effect immediately.

    When ``config`` is None, the global ``settings.llm_provider`` singleton
    is used (cached, backward compatible).
    """
    global _provider_instance

    if config is not None:
        provider_name = (config.get("provider") or settings.llm_provider).lower().strip()
        return _build_provider(
            provider_name,
            api_key=config.get("api_key"),
            model=config.get("model"),
        )

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
        _provider_instance = _build_provider(provider_name)
        logger.info("LLM provider initialized: %s", _provider_instance.name)

    return _provider_instance


def reset_provider() -> None:
    """Reset the cached provider (useful for testing)."""
    global _provider_instance
    _provider_instance = None


# Module-level convenience alias
llm = get_provider()
