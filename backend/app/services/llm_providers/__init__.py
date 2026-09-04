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
from typing import Any, Dict, List, Optional

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
    if provider_name == "glm":
        from app.services.llm_providers.glm_provider import GLMProvider
        return GLMProvider(api_key=api_key, model=model)
    if provider_name == "local":
        from app.services.llm_providers.local_provider import LocalProvider
        return LocalProvider(api_key=api_key, model=model)
    if provider_name == "ollama":
        from app.services.llm_providers.ollama_provider import OllamaProvider
        return OllamaProvider(model=model)
    elif provider_name == "openrouter":
        from app.services.llm_providers.openrouter_provider import OpenRouterProvider
        return OpenRouterProvider(api_key=api_key, model=model)
    elif provider_name == "nvidia":
        from app.services.llm_providers.nvidia_provider import NvidiaProvider
        return NvidiaProvider(api_key=api_key, model=model)
    elif provider_name == "huggingface":
        from app.services.llm_providers.huggingface_provider import HuggingFaceProvider
        return HuggingFaceProvider(api_key=api_key, model=model)
    elif provider_name == "google":
        from app.services.llm_providers.google_provider import GoogleProvider
        return GoogleProvider(api_key=api_key, model=model)
    elif provider_name == "modelslab":
        from app.services.llm_providers.modelslab_provider import ModelsLabProvider
        return ModelsLabProvider(api_key=api_key, model=model)
    elif provider_name == "groq":
        from app.services.llm_providers.groq_provider import GroqProvider
        return GroqProvider(api_key=api_key, model=model)
    elif provider_name == "together":
        from app.services.llm_providers.together_provider import TogetherProvider
        return TogetherProvider(api_key=api_key, model=model)
    elif provider_name == "mistral":
        from app.services.llm_providers.mistral_provider import MistralProvider
        return MistralProvider(api_key=api_key, model=model)
    elif provider_name == "cohere":
        from app.services.llm_providers.cohere_provider import CohereProvider
        return CohereProvider(api_key=api_key, model=model)
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
            "glm": "glm",
            "local": "local",
            "ollama": "ollama",
            "openrouter": "openrouter",
            "nvidia": "nvidia nim",
            "huggingface": "hugging face",
            "google": "google ai",
            "modelslab": "modelslab",
            "groq": "groq",
            "together": "together ai",
            "mistral": "mistral",
            "cohere": "cohere",
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


# ── Free-tier fallback chain (Phase 4) ─────────────────────
# Minimal, fixed chain: primary provider -> GLM 5.3 Flash (free local router)
# -> globally-configured free cloud provider (OpenRouter/Groq) -> Ollama.
# Deliberately NOT a general cross-provider chain.

_FALLBACK_FREE_PROVIDERS = ("openrouter", "groq")
_OLLAMA_FALLBACK_MODEL = "dolphin-mistral"


def build_fallback_chain(provider_config: Optional[dict] = None) -> List[Optional[dict]]:
    """Return the ordered provider-config chain used when the primary fails.

    1. The user's configured provider (``provider_config``, or None meaning
       the global .env default).
    2. GLM 5.3 Flash (free local router) — unless it already IS the primary.
    3. A globally-configured free cloud provider (OpenRouter/Groq), if the
       global default points at one and it is not already the primary.
    4. Ollama ``dolphin-mistral`` — last resort.
    """
    primary_name = (
        (provider_config or {}).get("provider") or settings.llm_provider
    ).lower().strip()

    chain: List[Optional[dict]] = [provider_config]

    if settings.glm_enable and primary_name != "glm":
        chain.append({"provider": "glm"})

    if (
        primary_name not in _FALLBACK_FREE_PROVIDERS
        and settings.llm_provider in _FALLBACK_FREE_PROVIDERS
    ):
        chain.append({"provider": settings.llm_provider})

    if primary_name != "ollama":
        chain.append({"provider": "ollama", "model": _OLLAMA_FALLBACK_MODEL})

    return chain


def generate_response_with_fallback(
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str] = None,
    model_name: Optional[str] = None,
    provider_config: Optional[dict] = None,
) -> str:
    """Generate a response, falling down the free-tier chain on provider failure.

    The caller's ``model_name`` is honoured only on the primary provider;
    fallback entries use their own default model (a session model pinned to
    the primary provider is meaningless on a different backend).
    Raises the last provider error when every entry in the chain fails.
    """
    chain = build_fallback_chain(provider_config)
    last_error: Optional[Exception] = None
    for i, cfg in enumerate(chain):
        try:
            provider = get_provider(config=cfg)
            return provider.generate_response(
                messages=messages,
                system_prompt=system_prompt,
                model_name=model_name if i == 0 else None,
            )
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            logger.warning(
                "LLM provider '%s' failed (%s) — trying next fallback",
                (cfg or {}).get("provider") or settings.llm_provider,
                exc,
            )
            last_error = exc
    raise last_error or RuntimeError(
        "All LLM providers in the fallback chain failed."
    )


# Module-level convenience alias
llm = get_provider()
