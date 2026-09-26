"""D6C-J2/J3 scenario-wiring contract tests (RED first, test-only harness).

The approve/deny journeys are only meaningful if the SWITCH itself is
sound. These tests pin the structural wiring that the browser journeys
depend on:

* the scenario is child-process-env only, never client-suppliable;
* the marker provider and the fake MCP transport are module-level
  singletons, so every worker shares exactly one of each;
* the default (production-shaped) worker path is unchanged;
* the trusted tool row is seeded before any submission can exist;
* seeding is idempotent;
* the seven durable production defaults are still False.

Structural assertions use the ast module rather than importing
tests.smoke.durable_smoke_app: importing it builds a FastAPI app, engines
and tables as a side effect, which does not belong in a focused unit test.

Run: PYTHONPATH=backend python -m pytest tests/smoke/test_scenario_wiring.py -q
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.models.models import TrustedTool
from tests.run_engine_helpers import ensure_user, mk_engine, mk_session
from tests.smoke.mcp_fakes import seed_trusted_echo_tool

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_APP = REPO_ROOT / "tests" / "smoke" / "durable_smoke_app.py"
RUNNER = REPO_ROOT / "tests" / "smoke" / "run_smoke.py"

DURABLE_DEFAULTS = (
    "run_engine_enabled",
    "run_worker_enabled",
    "run_public_api_enabled",
    "run_chat_stream_enabled",
    "run_messages_stream_durable_enabled",
    "run_deep_research_enabled",
    "run_multi_agent_enabled",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_scope(tree: ast.Module):
    """Top-level statements, flattening module-level if/else bodies.

    The scenario switch assigns its seams inside a module-level if/else, so
    those names are still constructed exactly once per process - they are
    just not direct children of tree.body.
    """
    out = []
    for stmt in tree.body:
        out.append(stmt)
        if isinstance(stmt, ast.If):
            out.extend(stmt.body)
            out.extend(stmt.orelse)
    return out


def _module_assign(tree: ast.Module, name: str) -> ast.Assign:
    """The single module-level assignment to name (not a local one)."""
    hits = [
        node for node in _module_scope(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]
    assert len(hits) == 1, "expected exactly one module-level %s, got %d" % (name, len(hits))
    return hits[0]


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    hits = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(hits) == 1, "expected exactly one %s(), got %d" % (name, len(hits))
    return hits[0]


def test_scenario_switch_is_read_only_from_the_child_process_env():
    node = _module_assign(_tree(SMOKE_APP), "SCENARIO")
    src = ast.unparse(node.value)
    assert "os.getenv('THUNDER_SMOKE_SCENARIO')" in src, src
    # No client-reachable surface may set it: the switch must not exist
    # anywhere in the shipped application package.
    offenders = [
        str(p) for p in (REPO_ROOT / "backend" / "app").rglob("*.py")
        if "THUNDER_SMOKE_SCENARIO" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == [], "scenario switch leaked into production: %s" % offenders


def test_marker_provider_and_transport_are_module_level_singletons():
    """Module scope means constructed once per process, so one shared object."""
    tree = _tree(SMOKE_APP)
    arms = [n for n in tree.body if isinstance(n, ast.If)
            and ast.unparse(n.test) == "MCP_SCENARIO"]
    assert len(arms) == 1, "expected one module-level scenario switch"
    arm = arms[0]

    def _assigned(stmts):
        out = {}
        for node in stmts:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = ast.unparse(node.value)
        return out

    scenario = _assigned(arm.body)
    assert scenario.get("MCP_PROVIDER") == "MarkerProvider()", scenario
    assert scenario.get("MCP_TRANSPORT") == "FakeMcpTransport()", scenario

    # the default arm must not construct a fake provider or transport at all
    default = _assigned(arm.orelse)
    assert default.get("MCP_PROVIDER") == "None", default
    assert default.get("MCP_TRANSPORT") == "None", default


def test_worker_factory_selects_scenario_seams_and_leaves_default_untouched():
    fn = _func(_tree(SMOKE_APP), "_build_worker")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(returns) == 6, "expected one arm per scenario plus the default"
    for r in returns:
        assert isinstance(r.value, ast.Call), ast.dump(r.value)
        assert ast.unparse(r.value.func) == "RunWorker", ast.unparse(r.value.func)

    branches = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
    assert [ast.unparse(b.test) for b in branches] == [
        "MULTI_AGENT_SCENARIO", "DEEP_RESEARCH_SCENARIO", "CANCEL_SCENARIO",
        "MCP_SCENARIO", "PYTHON_SCENARIO"]
    for b in branches:
        # each scenario arm returns early, so the default arm stays the fall-through
        assert len(b.body) == 1 and isinstance(b.body[0], ast.Return)
        assert b.orelse == []

    ma_kwargs = {k.arg: ast.unparse(k.value) for k in branches[0].body[0].value.keywords}
    assert ma_kwargs == {"chat_provider": "MA_PROVIDER"}, ma_kwargs

    dr_kwargs = {k.arg: ast.unparse(k.value) for k in branches[1].body[0].value.keywords}
    assert dr_kwargs == {"chat_provider": "DR_PROVIDER",
                         "research_search_adapter": "DR_SEARCH",
                         "research_fetch_adapter": "DR_FETCH"}, dr_kwargs

    cancel_kwargs = {k.arg: ast.unparse(k.value) for k in branches[2].body[0].value.keywords}
    assert cancel_kwargs == {"chat_provider": "CANCEL_PROVIDER"}, cancel_kwargs

    mcp_kwargs = {k.arg: ast.unparse(k.value) for k in branches[3].body[0].value.keywords}
    assert mcp_kwargs == {"chat_provider": "MCP_PROVIDER",
                          "mcp_transport": "MCP_TRANSPORT"}, mcp_kwargs

    py_kwargs = {k.arg: ast.unparse(k.value) for k in branches[4].body[0].value.keywords}
    assert py_kwargs == {"chat_provider": "PY_PROVIDER",
                         "python_executor": "PY_EXECUTOR",
                         "mcp_transport": "PY_TRANSPORT"}, py_kwargs

    # the default arm is the fall-through return: unchanged, ONE kwarg, no transport
    fallthrough = [n for n in fn.body if isinstance(n, ast.Return)]
    assert len(fallthrough) == 1, "the default path must be a bare fall-through return"
    default_kwargs = {k.arg: ast.unparse(k.value) for k in fallthrough[0].value.keywords}
    assert default_kwargs == {"chat_provider": "CHAT_PROVIDER"}, default_kwargs


def test_cancel_fixture_is_scenario_scoped_and_records_inside_the_smoke_root():
    tree = _tree(SMOKE_APP)
    arms = [n for n in tree.body if isinstance(n, ast.If)
            and ast.unparse(n.test) == "CANCEL_SCENARIO"]
    assert len(arms) == 1, "expected one module-level CANCEL_SCENARIO switch"
    arm = arms[0]
    src = ast.unparse(arm)

    assert "CancellableProviderFactory" in src, src
    assert "cancel_provider_calls.jsonl" in src, src
    assert "SMOKE_ROOT" in src, src
    assert "max_invocations" in src, src

    # the fixture must NEVER be imported on the default (non-scenario) path
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert "cancel_fakes" not in ast.unparse(node)

    def _assigned(stmts):
        out = {}
        for node in stmts:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = ast.unparse(node.value)
        return out

    assert _assigned(arm.orelse).get("CANCEL_PROVIDER") == "None", _assigned(arm.orelse)


def test_runner_injects_the_scenario_into_the_backend_child_env_only():
    fn = _func(_tree(RUNNER), "_start_backend")
    assigns = [
        ast.unparse(n) for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and ast.unparse(n.targets[0]) == "env['THUNDER_SMOKE_SCENARIO']"
    ]
    assert assigns == ["env['THUNDER_SMOKE_SCENARIO'] = scenario"], assigns

    src = RUNNER.read_text(encoding="utf-8")
    # the child still needs both roots importable (app package, tests package)
    assert 'str(REPO_ROOT / "backend") + os.pathsep + str(REPO_ROOT)' in src


def test_trusted_tool_is_seeded_before_any_submission_can_exist():
    lines = RUNNER.read_text(encoding="utf-8").splitlines()

    def _line(needle: str) -> int:
        hits = [i for i, l in enumerate(lines) if needle in l]
        assert hits, "not found: %s" % needle
        return hits[0]

    seed = _line("seed_trusted_echo_tool(_db")
    await_browser = _line('result["stage"] = "await_browser"')
    manifest = _line("atomic_write_json(manifest_path")
    # the external browser controller only learns the URLs from the manifest,
    # so a seeded row strictly precedes any possible submission.
    assert seed < await_browser < manifest, (seed, await_browser, manifest)


def test_seeding_is_idempotent_and_preserves_identity():
    eng = mk_engine()
    try:
        db = mk_session(eng)
        ensure_user(db, 7)
        first = seed_trusted_echo_tool(db, owner_user_id=7)
        identity = (first.tool_schema_hash, first.server_fingerprint, first.policy_version)
        second = seed_trusted_echo_tool(db, owner_user_id=7)
        assert db.query(TrustedTool).count() == 1
        assert (second.tool_schema_hash, second.server_fingerprint,
                second.policy_version) == identity
        assert second.locked is False and second.enabled is True
        db.close()
    finally:
        eng.dispose()


def test_durable_production_defaults_are_still_false():
    tree = _tree(REPO_ROOT / "backend" / "app" / "config.py")
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in DURABLE_DEFAULTS and node.value is not None:
                seen[node.target.id] = node.value
    missing = [n for n in DURABLE_DEFAULTS if n not in seen]
    assert missing == [], "flags not found in Settings: %s" % missing
    for name, value in seen.items():
        assert isinstance(value, ast.Constant) and value.value is False,             "%s default is not False: %s" % (name, ast.unparse(value))
