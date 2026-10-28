"""Assertion kinds for the eval harness. Pure functions, no I/O."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class AssertResult:
    ok: bool
    kind: str
    detail: str = ""


_PATH_SEG = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve_path(payload: dict, path: str):
    """Resolve 'a.b[0].c' against payload. '$json' -> json.dumps(payload)."""
    if path == "$json":
        return json.dumps(payload, sort_keys=True)
    cur = payload
    for name, idx in _PATH_SEG.findall(path):
        if name:
            cur = cur[name]
        else:
            cur = cur[int(idx)]
    return cur


def _text(payload: dict, where: str) -> str:
    try:
        val = resolve_path(payload, where)
    except (KeyError, IndexError, TypeError):
        return ""
    return val if isinstance(val, str) else json.dumps(val, sort_keys=True)


def _flags(spec: str) -> int:
    out = 0
    for ch in spec or "":
        out |= {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}.get(ch, 0)
    return out


_MARKER_RE = re.compile(r"\[Source:[^\]]+\]|\[\d+\]")


def run_assert(a, payload: dict) -> AssertResult:
    kind = a.kind
    d = a.model_dump()
    where = d.get("in", "response_text")
    if kind == "contains":
        hay = _text(payload, where).lower()
        ok = d["value"].lower() in hay
        return AssertResult(ok, kind, f"{d['value']!r} in {where}")
    if kind == "not_contains":
        hay = _text(payload, where).lower()
        ok = d["value"].lower() not in hay
        return AssertResult(ok, kind, f"{d['value']!r} not in {where}")
    if kind == "regex":
        ok = re.search(d["pattern"], _text(payload, where), _flags(d.get("flags", ""))) is not None
        return AssertResult(ok, kind, f"/{d['pattern']}/ on {where}")
    if kind == "json_path_exists":
        try:
            resolve_path(payload, d["path"])
            return AssertResult(True, kind, d["path"])
        except (KeyError, IndexError, TypeError) as exc:
            return AssertResult(False, kind, f"{d['path']}: {exc!r}")
    if kind == "citation_present":
        need = int(d["min"])
        cites = payload.get("citations") or []
        if len(cites) >= need:
            return AssertResult(True, kind, f"{len(cites)} citation objects >= {need}")
        markers = set(_MARKER_RE.findall(payload.get("response_text", "")))
        ok = len(markers) >= need
        return AssertResult(ok, kind, f"{len(markers)} text markers vs need {need}")
    if kind == "marker_absent":
        ok = d["marker"].lower() not in payload.get("response_text", "").lower()
        return AssertResult(ok, kind, f"{d['marker']!r} absent")
    if kind == "status_code":
        ok = payload.get("status_code") == int(d["equals"])
        return AssertResult(ok, kind, f"status_code == {d['equals']}")
    return AssertResult(False, kind, "unknown assert kind")
