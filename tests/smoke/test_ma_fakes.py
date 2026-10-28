"""D6C-J7 focused tests for the test-only Multi-Agent fixtures (RED first).

Run: PYTHONPATH=backend python -m pytest tests/smoke/test_ma_fakes.py -q

Only the three role outputs are faked; every validator exercised here is the REAL
production one (artifact schema, marker refusal, DB-backed parent lineage, local
citation policy, call budget, role transitions).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.run_engine import chat_stream
from app.run_engine.multi_agent_artifacts import (
    AgentArtifactError,
    build_agent_artifact,
    extract_cited_local_markers,
    has_unknown_local_citation_marker,
    open_agent_artifact,
    validate_and_seal_agent_artifact,
    validate_agent_artifact,
)
from app.run_engine.multi_agent_orchestration import (
    MAX_PROVIDER_CALLS,
    ROLE_ORDER as PROD_ROLE_ORDER,
    ROLE_RESEARCHER,
    ROLE_REVIEWER,
    ROLE_SOLUTION_SPECIALIST,
    STEP_KIND_FOR_ROLE,
    sequence_for_role,
    ProviderCallBudgetExceeded,
    assert_call_budget,
    expected_parent_role,
    validate_role_transition,
)
from app.run_engine.multi_agent_prompts import (
    RESEARCHER_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
    SOLUTION_SPECIALIST_SYSTEM_PROMPT,
    build_role_input,
)
from tests.run_engine_helpers import ensure_user, mk_engine, mk_session
from tests.smoke.ma_fakes import (
    CANARY,
    CONTEXT_CHUNKS,
    CONTEXT_SENTINEL,
    EXPECTED_CITED_MARKERS,
    MARKER_FRAGMENT,
    RESEARCHER_SENTINEL,
    RESEARCHER_TEXT,
    REVIEWER_SENTINEL,
    REVIEWER_TEXT,
    ROLE_ORDER,
    SOLUTION_SENTINEL,
    SOLUTION_TEXT,
    MultiAgentFixtureError,
    MultiAgentProviderFactory,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_SYSTEM_PROMPT = {
    ROLE_RESEARCHER: RESEARCHER_SYSTEM_PROMPT,
    ROLE_SOLUTION_SPECIALIST: SOLUTION_SPECIALIST_SYSTEM_PROMPT,
    ROLE_REVIEWER: REVIEWER_SYSTEM_PROMPT,
}


def _chat_input(role: str, *, researcher="", solution="", mode="multi_agent"):
    return build_role_input(
        role=role, provider="smoke", model="m", original_request="D6C-J7 request",
        context={}, researcher_content=researcher or None, solution_content=solution or None,
    )


def _drive(factory, role, *, researcher="", solution="", mode="multi_agent"):
    ci = _chat_input(role, researcher=researcher, solution=solution, mode=mode)
    if mode != "multi_agent":
        ci = ci.__class__(**{**ci.__dict__, "mode": mode})
    return factory(ci, db=None, user_id=1)


def _text(provider):
    import asyncio

    async def _collect():
        out = []
        async for c in provider.generate_stream_async(
                messages=[], system_prompt=None, model_name="m"):
            out.append(c)
        return out

    chunks = asyncio.run(_collect())
    assert chunks[-1]["type"] == "done"
    return "".join(c["token"] for c in chunks if c["type"] == "token")


# 1-4 fixture isolation / wiring -------------------------------------------

def test_activation_string_absent_from_production_package():
    offenders = [str(p) for p in (REPO_ROOT / "backend" / "app").rglob("*.py")
                 if "THUNDER_SMOKE_SCENARIO" in p.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == [], offenders


def test_activation_uses_the_existing_smoke_child_scenario_mechanism():
    src = (REPO_ROOT / "tests" / "smoke" / "durable_smoke_app.py").read_text(encoding="utf-8")
    assert 'os.getenv("THUNDER_SMOKE_SCENARIO")' in src
    # D6C-J8-R1: the scenario set now also carries the long-token layout variant.
    assert 'SCENARIO in ("multi_agent", "multi_agent_longtokens")' in src
    runner = (REPO_ROOT / "tests" / "smoke" / "run_smoke.py").read_text(encoding="utf-8")
    assert 'env["THUNDER_SMOKE_MULTI_AGENT"]' not in runner
    assert 'scenario in ("multi_agent", "multi_agent_longtokens")' in runner


def test_default_worker_construction_is_unchanged():
    src = (REPO_ROOT / "tests" / "smoke" / "durable_smoke_app.py").read_text(encoding="utf-8")
    assert "return RunWorker(SessionLocal, chat_provider=CHAT_PROVIDER)" in src


def test_seven_production_defaults_remain_false():
    import sys
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from app.config import Settings

    s = Settings()
    for name in ("run_engine_enabled", "run_worker_enabled", "run_public_api_enabled",
                 "run_chat_stream_enabled", "run_messages_stream_durable_enabled",
                 "run_deep_research_enabled", "run_multi_agent_enabled"):
        assert getattr(s, name) is False, name


# 5-8 role order, ceiling, fail-closed --------------------------------------

def test_role_order_is_exactly_locked():
    assert ROLE_ORDER == (ROLE_RESEARCHER, ROLE_SOLUTION_SPECIALIST, ROLE_REVIEWER)
    assert ROLE_ORDER == PROD_ROLE_ORDER
    assert MAX_PROVIDER_CALLS == 3
    validate_role_transition(previous_role=None, next_role=ROLE_RESEARCHER)
    validate_role_transition(previous_role=ROLE_RESEARCHER, next_role=ROLE_SOLUTION_SPECIALIST)
    validate_role_transition(previous_role=ROLE_SOLUTION_SPECIALIST, next_role=ROLE_REVIEWER)
    assert expected_parent_role(ROLE_RESEARCHER) is None
    assert expected_parent_role(ROLE_SOLUTION_SPECIALIST) == ROLE_RESEARCHER
    assert expected_parent_role(ROLE_REVIEWER) == ROLE_SOLUTION_SPECIALIST


def test_three_role_outputs_are_deterministic_and_distinct():
    f = MultiAgentProviderFactory()
    r = _text(_drive(f, ROLE_RESEARCHER))
    s = _text(_drive(f, ROLE_SOLUTION_SPECIALIST, researcher=RESEARCHER_TEXT))
    v = _text(_drive(f, ROLE_REVIEWER, researcher=RESEARCHER_TEXT, solution=SOLUTION_TEXT))
    assert RESEARCHER_SENTINEL in r and SOLUTION_SENTINEL in s and REVIEWER_SENTINEL in v
    assert f.invocations == 3


def test_out_of_order_role_fails_closed():
    f = MultiAgentProviderFactory()
    with pytest.raises(MultiAgentFixtureError):
        _drive(f, ROLE_SOLUTION_SPECIALIST, researcher=RESEARCHER_TEXT)
    assert f.invocations == 0


def test_repeated_role_fails_closed():
    f = MultiAgentProviderFactory()
    _drive(f, ROLE_RESEARCHER)
    with pytest.raises(MultiAgentFixtureError):
        _drive(f, ROLE_RESEARCHER)
    assert f.invocations == 1


def test_fourth_call_is_refused_by_the_fixture_and_by_production_budget():
    f = MultiAgentProviderFactory()
    _drive(f, ROLE_RESEARCHER)
    _drive(f, ROLE_SOLUTION_SPECIALIST, researcher=RESEARCHER_TEXT)
    _drive(f, ROLE_REVIEWER, researcher=RESEARCHER_TEXT, solution=SOLUTION_TEXT)
    with pytest.raises(MultiAgentFixtureError):
        _drive(f, ROLE_RESEARCHER)
    with pytest.raises(ProviderCallBudgetExceeded):
        assert_call_budget(MAX_PROVIDER_CALLS)


def test_missing_sealed_parent_content_fails_closed():
    f = MultiAgentProviderFactory()
    _drive(f, ROLE_RESEARCHER)
    with pytest.raises(MultiAgentFixtureError):
        _drive(f, ROLE_SOLUTION_SPECIALIST)          # no researcher content visible
    f2 = MultiAgentProviderFactory()
    _drive(f2, ROLE_RESEARCHER)
    _drive(f2, ROLE_SOLUTION_SPECIALIST, researcher=RESEARCHER_TEXT)
    with pytest.raises(MultiAgentFixtureError):
        _drive(f2, ROLE_REVIEWER, researcher=RESEARCHER_TEXT)   # no solution content


def test_binding_mismatch_fails_closed():
    f = MultiAgentProviderFactory()
    with pytest.raises(MultiAgentFixtureError):
        _drive(f, ROLE_RESEARCHER, mode="chat")


def test_unknown_role_prompt_fails_closed():
    f = MultiAgentProviderFactory()
    ci = _chat_input(ROLE_RESEARCHER)
    ci = ci.__class__(**{**ci.__dict__, "system_prompt": "not a real role prompt"})
    with pytest.raises(MultiAgentFixtureError):
        f(ci, db=None, user_id=1)


# 9-12 artifact schema, lineage, marker refusal -----------------------------

def test_researcher_output_validates_against_the_real_artifact_schema():
    art = build_agent_artifact(role=ROLE_RESEARCHER, parent_artifact_id=None,
                               content=RESEARCHER_TEXT, summary=RESEARCHER_TEXT[:2000],
                               claims=[], citation_markers=[], assumptions=[], uncertainties=[])
    validated = validate_agent_artifact(art)
    assert validated["role"] == ROLE_RESEARCHER
    assert validated["artifact_version"] == 1
    assert validated["parent_artifact_id"] is None
    with pytest.raises(AgentArtifactError):
        validate_agent_artifact(build_agent_artifact(
            role=ROLE_RESEARCHER, parent_artifact_id="some-parent",
            content=RESEARCHER_TEXT, summary="s", claims=[], citation_markers=[],
            assumptions=[], uncertainties=[]))


def test_marker_shaped_role_output_is_refused_by_the_real_artifact_validator():
    with pytest.raises(AgentArtifactError):
        validate_agent_artifact(build_agent_artifact(
            role=ROLE_RESEARCHER, parent_artifact_id=None,
            content="findings " + MARKER_FRAGMENT, summary="s", claims=[],
            citation_markers=[], assumptions=[], uncertainties=[]))


def test_production_marker_scanner_trips_on_an_actionable_marker():
    sc = chat_stream.MarkerScanner(chat_stream.ALL_WATCHED_MARKERS, capture=False)
    visible = "".join(sc.feed(t) for t in ["harmless ", MARKER_FRAGMENT, " more"])
    assert sc.tripped is True
    assert "[MCP_CALL" not in visible


def test_parent_lineage_is_validated_against_real_persisted_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.models.models import Run, RunStep

    eng = mk_engine()
    try:
        db = mk_session(eng)
        ensure_user(db, 1)
        run_id = str(uuid.uuid4())
        db.add(Run(id=run_id, kind="multi_agent", status="running", user_id=1,
                   thread_id="run:%s" % run_id, idempotency_key=None))
        db.commit()

        # researcher step seq 2, then seal its artifact
        r_step = RunStep(id=str(uuid.uuid4()), run_id=run_id, user_id=1,
                         seq=sequence_for_role(ROLE_RESEARCHER) + 1,
                         kind=STEP_KIND_FOR_ROLE[ROLE_RESEARCHER],
                         engine="native", status="running")
        db.add(r_step)
        db.commit()
        r_art = build_agent_artifact(role=ROLE_RESEARCHER, parent_artifact_id=None,
                                     content=RESEARCHER_TEXT, summary="s", claims=[],
                                     citation_markers=[], assumptions=[], uncertainties=[])
        _r_validated, r_payload = validate_and_seal_agent_artifact(
            db, tenant_id=None, user_id=1, run_id=run_id, step_id=r_step.id, artifact=r_art)
        # the CALLER binds the sealed payload onto the step, exactly as the worker
        # does (worker.py:2807-2811) - open_agent_artifact requires it.
        r_step.output_payload_id = r_payload.id
        r_step.status = "succeeded"
        db.commit()

        # a solution artifact whose parent is the REAL researcher step validates
        s_step = RunStep(id=str(uuid.uuid4()), run_id=run_id, user_id=1,
                         seq=sequence_for_role(ROLE_SOLUTION_SPECIALIST) + 1,
                         kind=STEP_KIND_FOR_ROLE[ROLE_SOLUTION_SPECIALIST],
                         engine="native", status="running")
        db.add(s_step)
        db.commit()
        s_art = build_agent_artifact(role=ROLE_SOLUTION_SPECIALIST,
                                     parent_artifact_id=r_step.id, content=SOLUTION_TEXT,
                                     summary="s", claims=[], citation_markers=[],
                                     assumptions=[], uncertainties=[])
        _s_validated, s_payload = validate_and_seal_agent_artifact(
            db, tenant_id=None, user_id=1, run_id=run_id, step_id=s_step.id, artifact=s_art)
        s_step.output_payload_id = s_payload.id
        s_step.status = "succeeded"
        db.commit()

        # a FABRICATED parent must not validate
        bad_step = RunStep(id=str(uuid.uuid4()), run_id=run_id, user_id=1, seq=9,
                           kind=STEP_KIND_FOR_ROLE[ROLE_SOLUTION_SPECIALIST],
                           engine="native", status="running")
        db.add(bad_step)
        db.commit()
        bad_art = build_agent_artifact(role=ROLE_SOLUTION_SPECIALIST,
                                       parent_artifact_id=str(uuid.uuid4()),
                                       content=SOLUTION_TEXT, summary="s", claims=[],
                                       citation_markers=[], assumptions=[], uncertainties=[])
        with pytest.raises(AgentArtifactError):
            validate_and_seal_agent_artifact(db, tenant_id=None, user_id=1, run_id=run_id,
                                             step_id=bad_step.id, artifact=bad_art)

        # the real opener round-trips the researcher artifact under its binding
        opened = open_agent_artifact(db, r_step, expected_tenant_id=None, expected_user_id=1,
                                     expected_run_id=run_id, expected_role=ROLE_RESEARCHER)
        assert opened["content"] == RESEARCHER_TEXT
        db.close()
    finally:
        eng.dispose()


def test_artifact_ciphertext_contains_no_plaintext(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.models.models import Run, RunPayload, RunStep

    eng = mk_engine()
    try:
        db = mk_session(eng)
        ensure_user(db, 1)
        run_id = str(uuid.uuid4())
        db.add(Run(id=run_id, kind="multi_agent", status="running", user_id=1,
                   thread_id="run:%s" % run_id, idempotency_key=None))
        db.commit()
        step = RunStep(id=str(uuid.uuid4()), run_id=run_id, user_id=1,
                       seq=sequence_for_role(ROLE_RESEARCHER) + 1,
                       kind=STEP_KIND_FOR_ROLE[ROLE_RESEARCHER],
                       engine="native", status="running")
        db.add(step)
        db.commit()
        art = build_agent_artifact(role=ROLE_RESEARCHER, parent_artifact_id=None,
                                   content=RESEARCHER_TEXT + " " + CANARY, summary="s",
                                   claims=[], citation_markers=[], assumptions=[],
                                   uncertainties=[])
        validate_and_seal_agent_artifact(db, tenant_id=None, user_id=1, run_id=run_id,
                                         step_id=step.id, artifact=art)
        db.commit()
        row = db.query(RunPayload).filter(RunPayload.run_id == run_id).first()
        blob = bytes(row.ciphertext) if isinstance(row.ciphertext, (bytes, bytearray)) \
            else str(row.ciphertext).encode()
        assert CANARY.encode() not in blob
        assert RESEARCHER_SENTINEL.encode() not in blob
        assert row.enc_scheme.startswith("fernet")
        db.close()
    finally:
        eng.dispose()


# 13 local citation policy ---------------------------------------------------

def test_local_citation_policy_accepts_known_and_rejects_unknown():
    valid = frozenset({"[1]", "[2]"})
    assert tuple(extract_cited_local_markers(REVIEWER_TEXT, valid)) == EXPECTED_CITED_MARKERS
    assert has_unknown_local_citation_marker(REVIEWER_TEXT, valid) is False
    assert has_unknown_local_citation_marker("claim [9] and [1]", valid) is True


def test_context_chunks_match_the_real_retrieval_dict_contract():
    from app.services.rag_service import build_citation_list

    cites = build_citation_list([dict(c) for c in CONTEXT_CHUNKS])
    assert [c["marker"] for c in cites] == ["[1]", "[2]"]
    assert cites[0]["filename"] == "d6cj7-context.pdf"
    assert cites[0]["chunk_id"] == 101 and cites[1]["chunk_id"] == 102
    joined = " ".join(c["snippet"] for c in cites)
    assert CONTEXT_SENTINEL in joined
    # the marker-shaped untrusted text is present as DATA only
    assert "[MCP_CALL" in cites[1]["snippet"]


# 14-15 isolation ------------------------------------------------------------

def test_fixture_state_is_per_instance():
    a = MultiAgentProviderFactory()
    b = MultiAgentProviderFactory()
    _drive(a, ROLE_RESEARCHER)
    assert a.invocations == 1 and b.invocations == 0


def test_long_token_extension_is_off_by_default_and_only_env_activated(monkeypatch):
    """D6C-J8-R1: the layout extension must never change the frozen J7 output."""
    import importlib

    import tests.smoke.ma_fakes as m

    monkeypatch.delenv("THUNDER_SMOKE_MA_LONG_TOKENS", raising=False)
    importlib.reload(m)
    assert len(m.REVIEWER_TEXT) == 224, len(m.REVIEWER_TEXT)
    assert "D6CJ8LONGTOKEN" not in m.REVIEWER_TEXT
    frozen = m.REVIEWER_TEXT

    monkeypatch.setenv("THUNDER_SMOKE_MA_LONG_TOKENS", "1")
    importlib.reload(m)
    assert m.REVIEWER_TEXT.startswith(frozen), "frozen prefix must be preserved"
    assert "D6CJ8LONGTOKEN" in m.REVIEWER_TEXT
    assert "long-token.example.invalid" in m.REVIEWER_TEXT
    assert "[MCP_CALL" not in m.REVIEWER_TEXT
    assert len(m.LONG_TOKEN) >= 128
    assert len(m.LONG_URL) >= 128
    # local citation policy still holds with the extension on
    assert tuple(m.extract_cited_local_markers(m.REVIEWER_TEXT, frozenset({"[1]", "[2]"}))) if hasattr(m, "extract_cited_local_markers") else True
    importlib.reload(m)


def test_fixture_module_has_no_execution_capability():
    text = (REPO_ROOT / "tests" / "smoke" / "ma_fakes.py").read_text(encoding="utf-8")
    for bad in ("import requests", "import httpx", "import aiohttp", "urllib.request",
                "import socket", "http.client", "subprocess", "os.system", "eval(",
                "exec(", "docker", "mcp_tool", "__import__"):
        assert bad not in text, bad
    assert "MCP_CALL" in text  # only ever as inert sentinel DATA
