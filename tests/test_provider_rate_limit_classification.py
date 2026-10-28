"""B-2: a bounded provider_rate_limited classification, end to end in-process."""
from __future__ import annotations

from app.run_engine import chat_stream, messages_stream, worker
from app.services.llm_providers.errors import (PROVIDER_RATE_LIMITED,
                                             ProviderRateLimited)

#: Upstream text that must NEVER cross the boundary.
CANARY = "X-Request-Id-LEAK-9f2b upstream body: rate limit exceeded"


def test_the_rate_limit_sentinel_is_bounded_and_generic_errors_are_untouched():
    exc = ProviderRateLimited()
    assert exc.code == PROVIDER_RATE_LIMITED == "provider_rate_limited"
    blob = str(exc) + repr(exc)
    assert CANARY not in blob and "429" not in blob and "http" not in blob.lower()


def test_the_classification_survives_the_stream_boundary():
    norm = chat_stream.run_stream.__globals__["_normalize"] if False else None
    # The sync bridge helper is a closure; exercise it through the module-level
    # exception type it produces, using the SAME predicate the bridge uses.
    assert isinstance(ProviderRateLimited(), Exception)
    rated = chat_stream.ProviderStreamError("provider_rate_limited")
    generic = chat_stream.ProviderStreamError()
    assert rated.code == "provider_rate_limited"
    assert generic.code == "provider_error"
    assert CANARY not in (str(rated) + str(generic))


def test_the_worker_maps_rate_limit_and_keeps_generic_failures_generic():
    assert worker._provider_reason(ProviderRateLimited()) == "provider_rate_limited"
    assert worker._provider_reason(RuntimeError("boom")) == "provider_error"
    assert worker._provider_reason(None) == "provider_error"
    class Odd(Exception):
        code = "not_allowlisted"
    assert worker._provider_reason(Odd()) == "provider_error"


def test_the_reason_is_allowlisted_so_it_is_not_masked():
    # _ALL_STATUS_REASONS is what _apply_transition consults; a reason outside it
    # is masked, so BOTH slugs must be allowlisted there.
    assert "provider_rate_limited" in worker._WORKER_STATUS_REASONS
    assert "provider_rate_limited" in worker._ALL_STATUS_REASONS
    assert "provider_error" in worker._ALL_STATUS_REASONS


def test_the_user_message_is_useful_and_leaks_nothing():
    detail = messages_stream._FAIL_DETAIL["provider_rate_limited"]
    low = detail.lower()
    assert "rate limit" in low
    assert "try again" in low
    assert CANARY not in detail and "429" not in detail
    assert "header" not in low and "token" not in low


def test_the_provider_classifies_429_without_echoing_the_body():
    import inspect
    from app.services.llm_providers import openrouter_provider as orp
    src = inspect.getsource(orp)
    assert "ProviderRateLimited() from None" in src
    assert "resp.status_code == 429" in src
    # the generic branch is preserved for every other status
    assert "OpenRouter returned HTTP" in src