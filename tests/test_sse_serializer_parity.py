"""MP-4B — canonical SSE serializer parity (RED first).

The durable SSE reader (run_engine/sse.py, run_engine/messages_stream.py) must not depend
on the legacy services/streaming_service module. format_sse moves to the neutral durable
module with byte-for-byte identical output, and streaming_service keeps a RE-EXPORT so
existing legacy callers and tests are unaffected during the transition.

Run: PYTHONPATH=backend python -m pytest tests/test_sse_serializer_parity.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_ENGINE = REPO_ROOT / "backend" / "app" / "run_engine"

from app.run_engine import sse as durable_sse  # noqa: E402
from app.services import streaming_service as legacy_ss  # noqa: E402

CASES = [
    ("plain", {"a": 1}),
    ("empty", {}),
    ("unicode", {"token": "\u0c28\u0c2e\u0c38\u0c4d\u0c15\u0c3e\u0c30\u0c02 \U0001f32a"}),
    ("multiline", {"text": "line1\nline2\tend"}),
    ("quotes", {"text": 'he said "hi" \\ and /'}),
    ("nested", {"outer": {"inner": [1, 2, {"k": None}]}, "b": True}),
]


def test_neutral_serializer_exists():
    """RED before MP-4B: the durable module had no serializer of its own."""
    assert hasattr(durable_sse, "format_sse"), "run_engine.sse must own format_sse"


def test_byte_for_byte_parity_with_the_legacy_helper():
    for name, data in CASES:
        assert durable_sse.format_sse(name, data) == legacy_ss.format_sse(name, data), name


def test_exact_wire_framing():
    out = durable_sse.format_sse("token", {"token": "hi"})
    assert out == 'event: token\ndata: {"token": "hi"}\n\n'
    assert out.endswith("\n\n")
    assert out.count("\n\n") == 1
    assert not out.endswith("\n\n\n")


def test_unicode_is_not_escaped_and_parses_back():
    out = durable_sse.format_sse("token", {"token": "\u0c28\u0c2e\u0c38\u0c4d"})
    body = out.split("data: ", 1)[1].rsplit("\n\n", 1)[0]
    assert json.loads(body) == {"token": "\u0c28\u0c2e\u0c38\u0c4d"}
    # json.dumps defaults to ensure_ascii=True, so non-ASCII is escaped in the
    # wire bytes. That is the EXISTING behaviour and parity requires keeping it.
    assert "\\u0c28" in body, "ensure_ascii must stay default so parity holds"


def test_legacy_reexport_identity():
    """The legacy symbol must remain importable and identical."""
    assert legacy_ss.format_sse is durable_sse.format_sse


def test_durable_package_has_zero_streaming_service_imports():
    """AST-based: no ACTUAL import of the legacy service from durable code.

    A docstring or comment may still NAME the module; what must be zero is a real
    Import / ImportFrom reference, because that is what creates the reverse
    dependency at runtime.
    """
    import ast

    offenders = []
    for p in RUN_ENGINE.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for n in names:
                if n and "streaming_service" in n:
                    offenders.append(p.name + ":" + n)
    assert offenders == [], "durable code must not import the legacy service: %s" % offenders


def test_serializer_module_has_no_db_or_provider_logic():
    text = (RUN_ENGINE / "sse.py").read_text(encoding="utf-8")
    for bad in ("Session", "select(", "RunWorker", "provider", "requests", "httpx"):
        assert bad not in text.split("def format_sse", 1)[1][:400], bad
