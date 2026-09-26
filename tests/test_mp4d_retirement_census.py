"""MP-4D precondition: reference census + no-dead-flags. Deletes nothing."""
from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
APP = REPO / "backend" / "app"
CONFIG = APP / "config.py"
CANDIDATES = ("run_research_workflow", "stream_chat_response",
              "stream_multi_agent_response")


def _mods():
    return sorted(p for p in APP.rglob("*.py") if p.is_file())


def _rel(p):
    return p.relative_to(REPO).as_posix()


def _refs(sym):
    rx = re.compile("(?<![A-Za-z0-9_])" + re.escape(sym) + "(?![A-Za-z0-9_])")
    hits = []
    for p in _mods():
        if rx.search(p.read_text(encoding="utf-8", errors="replace")):
            hits.append(_rel(p))
    return hits


def test_census_records_candidates_and_the_engine_boundary():
    cen = {s: _refs(s) for s in CANDIDATES}
    for s, r in cen.items():
        assert r, s + " vanished; update the census"
    bad = sorted({x for s in CANDIDATES for x in cen[s]
                  if x.startswith("backend/app/run_engine/")})
    assert bad == [], "legacy candidate reachable from run_engine: " + repr(bad)


def test_no_run_flag_is_dead():
    tree = ast.parse(CONFIG.read_text(encoding="utf-8"))
    names = []
    for n in ast.walk(tree):
        t = getattr(n, "target", None)
        if t is None and isinstance(n, ast.Assign) and n.targets:
            t = n.targets[0]
        if isinstance(t, ast.Name) and t.id.startswith("run_"):
            names.append(t.id)
    assert names, "expected run_* fields in config.py"
    blob = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in _mods() if p != CONFIG)
    q = chr(34)
    dead = [n for n in sorted(set(names))
            if ("settings." + n) not in blob and (q + n + q) not in blob]
    assert dead == [], "dead run_* flags: " + repr(dead)