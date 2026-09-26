"""Phase 1B.2R MP-3C3D4B — focused config-validation tests for the durable
deep-research acquisition bounds added to ``Settings``.

Hermetic: constructs `Settings` directly with overrides — never touches the
real `.env`/process environment, never imports `app.main` (a plain model
construction test).
"""
import math

import pytest

from app.config import Settings


def _base_kwargs(**overrides):
    """Settings requires no external services to construct; only the
    fields under test are overridden per call."""
    return overrides


# ── A1: valid defaults ───────────────────────────────────────


def test_valid_defaults_construct_without_error():
    s = Settings()
    assert s.research_max_queries == 1
    assert s.research_max_query_chars == 300
    assert s.research_max_result_urls == 10
    assert s.research_max_redirects == 3
    assert s.research_fetch_timeout_s == 10.0
    assert s.research_total_deadline_s == 45.0
    assert s.research_max_response_bytes == 2_000_000
    assert s.research_max_extracted_chars == 20_000


# ── A2: zero/negative limits rejected ────────────────────────


@pytest.mark.parametrize("field", [
    "research_max_queries", "research_max_query_chars", "research_max_result_urls",
    "research_max_redirects", "research_max_response_bytes", "research_max_extracted_chars",
])
@pytest.mark.parametrize("bad_value", [0, -1, -1000])
def test_zero_or_negative_int_bounds_rejected(field, bad_value):
    with pytest.raises(ValueError):
        Settings(**{field: bad_value})


# ── A3: NaN/infinite/non-positive timeouts rejected ──────────


@pytest.mark.parametrize("field", ["research_fetch_timeout_s", "research_total_deadline_s"])
@pytest.mark.parametrize("bad_value", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_invalid_timeout_values_rejected(field, bad_value):
    with pytest.raises(ValueError):
        Settings(**{field: bad_value})


def test_non_numeric_string_timeout_rejected():
    with pytest.raises(ValueError):
        Settings(research_fetch_timeout_s="not-a-number")


# ── A4: per-fetch timeout must be strictly less than total deadline ──


def test_fetch_timeout_equal_to_deadline_rejected():
    with pytest.raises(ValueError):
        Settings(research_fetch_timeout_s=45.0, research_total_deadline_s=45.0)


def test_fetch_timeout_greater_than_deadline_rejected():
    with pytest.raises(ValueError):
        Settings(research_fetch_timeout_s=50.0, research_total_deadline_s=45.0)


def test_fetch_timeout_below_deadline_accepted():
    s = Settings(research_fetch_timeout_s=5.0, research_total_deadline_s=30.0)
    assert s.research_fetch_timeout_s == 5.0
    assert s.research_total_deadline_s == 30.0
