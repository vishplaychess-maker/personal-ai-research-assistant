r"""RFC 8785 (JSON Canonicalization Scheme) — full contract.

Engine: **rfc8785** (PyPI ``rfc8785==0.1.4``), the pure-Python reference
implementation by Trail of Bits — Apache-2.0, zero runtime dependencies,
``Requires-Python >=3.8``. Chosen over a hand-rolled ECMAScript
double-to-string because number formatting (exponent thresholds, rounding
boundaries, subnormals, negative zero, integer/float equivalence) is exactly
where custom implementations go wrong, and this one is maintained by a security
firm and validated against the RFC's own vectors. See
``docs/superpowers/plans/2026-09-07-phase-1b1-approval-domain.md`` §canonicalization.

Contract (all delegated to ``rfc8785.dumps`` and asserted by tests against RFC
8785 Appendix B vectors + a differential test vs the reference package):

* **Numbers** — every finite JSON number representable as IEEE-754 double.
  ECMAScript ``Number.prototype.toString`` form: ``4.5`` -> ``4.5``,
  ``2e-3`` -> ``0.002``, ``1e30`` -> ``1e+30``, ``1e-27`` -> ``1e-27``,
  ``333333333.33333329`` -> ``333333333.3333333``. ``-0.0`` -> ``0``.
  ``4`` and ``4.0`` produce identical bytes.
* **NaN / +Inf / -Inf** — rejected (``CanonicalizationError``).
* **Integers** — only the I-JSON safe range ``[-(2**53 - 1), 2**53 - 1]``.
  A larger integer is rejected; supply it as a string if it must be carried.
* **Decimal / other numeric objects** — rejected. Convert before calling.
* **Strings** — preserved exactly. **No NFC/NFD or any Unicode normalization**
  (RFC 8785 forbids altering parsed string data). Minimal escaping only.
* **Lone surrogates** — rejected.
* **Arrays** — order preserved.
* **Object keys** — recursive sort by UTF-16 code units.
* **Output** — deterministic UTF-8 bytes, identical across Windows/Linux and
  supported Python versions.

Accepted container/leaf types: ``dict`` (str keys), ``list`` / ``tuple``,
``str``, ``bool``, ``int``, ``float``, ``None``. ``bytes``, ``set``, arbitrary
objects and non-str mapping keys are rejected.
"""

from __future__ import annotations

import rfc8785

from app.approval.errors import CanonicalizationError

# Guard against a pathological nesting depth exhausting the C stack before
# rfc8785 does — turns RecursionError into our typed error.
_MAX_DEPTH = 200


def _depth_ok(value: object, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise CanonicalizationError(f"structure nested deeper than {_MAX_DEPTH}")
    if isinstance(value, dict):
        for v in value.values():
            _depth_ok(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _depth_ok(v, depth + 1)


def canonicalize(value: object) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes of ``value``.

    Deterministic: equivalent parsed JSON values (any object-key ordering,
    ``4`` vs ``4.0``) -> identical bytes. Raises ``CanonicalizationError`` for
    any value outside the accepted domain (NaN/Infinity, unsafe integer,
    Decimal, bytes, set, object, non-str key, lone surrogate, over-deep).
    """
    _depth_ok(value)
    try:
        return rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as exc:
        # includes FloatDomainError (NaN/Inf) and IntegerDomainError (unsafe int)
        raise CanonicalizationError(str(exc) or type(exc).__name__) from exc
    except RecursionError as exc:
        raise CanonicalizationError("structure too deeply nested") from exc


def canonical_text(value: object) -> str:
    """``canonicalize`` as ``str`` (UTF-8 decoded) — handy for logging/tests."""
    return canonicalize(value).decode("utf-8")
