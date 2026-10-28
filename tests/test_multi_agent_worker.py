"""Phase 1B.2R MP-3C3D5C — durable Multi-Agent worker: end-to-end
Researcher -> Solution Specialist -> Reviewer execution, crash/recovery,
cancellation, marker/citation fail-closed behavior, and atomic finalization.

Fake providers only — no live LLM, no live network, no tool execution.
Reuses the exact hermetic fixture/injection patterns MP-3C1's
(`_submission`/`_seed`/`_worker`/`FakeProvider`) and D4C's own worker test
file already established.
"""
from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import Message, MessageRole, Run, RunEvent, RunStep
from app.run_engine import chat_stream
from app.run_engine import submission as sub
from app.run_engine.context import AuthContext
from app.run_engine.multi_agent_orchestration import (
    ROLE_REVIEWER,
    ROLE_RESEARCHER,
    ROLE_SOLUTION_SPECIALIST,
)
from app.run_engine.multi_agent_prompts import (
    REVIEWER_SYSTEM_PROMPT,
    RESEARCHER_SYSTEM_PROMPT,
    SOLUTION_SPECIALIST_SYSTEM_PROMPT,
)
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "unit-test-hmac-key-multi-agent-0123"
CANARY = "CANARY-secret-role-output-must-not-leak"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "multi_agent_role_timeout_s", 30.0)
    monkeypatch.setattr(settings, "multi_agent_total_deadline_s", 90.0)



@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


class RoleAwareProvider:
    """One fake provider instance shared across all 3 role calls — returns
    the Nth configured response on the Nth ``generate_stream_async`` call,
    tracking exactly what each call was invoked with."""

    def __init__(self, responses, *, error_at_call=None, raises_at_call=None, timeout_at_call=None):
        self.responses = list(responses)
        self.error_at_call = error_at_call
        self.raises_at_call = raises_at_call
        self.timeout_at_call = timeout_at_call
        self.calls = 0
        self.seen: list[dict] = []

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        idx = self.calls
        self.calls += 1
        self.seen.append({"messages": messages, "system_prompt": system_prompt, "model": model_name})
        if self.raises_at_call is not None and idx == self.raises_at_call:
            raise RuntimeError("boom")
        if self.timeout_at_call is not None and idx == self.timeout_at_call:
            import asyncio
            await asyncio.sleep(3600)  # never completes within the test's deadline
            return
        text = self.responses[idx] if idx < len(self.responses) else ""
        for ch in text:
            yield {"type": "token", "token": ch}
        if self.error_at_call is not None and idx == self.error_at_call:
            yield {"type": "error", "error": "boom UPSTREAM-SECRET"}
            return
        yield {"type": "done", "response": text}


_CITED_MARKER = "[1]"


def _context_with_citation():
    return chat_stream.build_context_snapshot(
        rag_context_text="Some retrieved passage.",
        citations=[{
            "marker": _CITED_MARKER, "document_id": 1, "chunk_id": 1,
            "filename": "doc.txt", "page_number": None, "snippet": "passage snippet",
        }],
        sources_used=True,
    )


def _submission(*, message="Please help with X.", provider="snap-prov", model="snap-model"):
    return sub.build_chat_submission(
        session_id=1, message=message, image_url=None, provider=provider,
        model=model, mode="multi_agent", system_prompt=None, options={},
    )


def _seed(eng, *, uid=7, sid=1, context=None, submission=None):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        from app.models.models import ResearchSession
        if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
            db.add(ResearchSession(id=sid, user_id=uid, title="t"))
            db.commit()
        ctx = make_ctx(db, user_id=uid, sid="fam-alpha")
        run, created = sub.create_chat_run(
            db, ctx, submission=submission or _submission(),
            idempotency_key=uuid.uuid4().hex, context=context,
        )
        assert created
        return run.id
    finally:
        db.close()


def _worker(eng, provider):
    return RunWorker(mk_factory(eng), chat_provider=lambda chat_input, *, db, user_id: provider)


def _ctx_for(eng, uid=7):
    db = mk_session(eng)
    try:
        return make_ctx(db, user_id=uid, sid="fam-alpha")
    finally:
        db.close()


# ── P1-P4: happy path, call count/order, sealed provider/model, no fallback ──


def test_full_three_role_happy_path(eng):
    rid = _seed(eng, context=_context_with_citation())
    provider = RoleAwareProvider([
        f"Researcher finds {_CITED_MARKER} relevant.",
        "Solution specialist proposes X.",
        f"Final answer citing {_CITED_MARKER}.",
    ])
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert len(out) == 1 and out[0].status == "succeeded"
    assert provider.calls == 3

    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        assert run.status == "succeeded"
        messages = db.query(Message).filter(Message.session_id == 1,
                                            Message.role == MessageRole.assistant).all()
        assert len(messages) == 1
        assert messages[0].content == f"Final answer citing {_CITED_MARKER}."
        citations = json.loads(messages[0].citations)
        assert citations == [{
            "marker": _CITED_MARKER, "document_id": 1, "chunk_id": 1,
            "filename": "doc.txt", "page_number": None, "snippet": "passage snippet",
        }]
    finally:
        db.close()


def test_exactly_three_provider_calls_in_correct_role_order(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider(["r out", "s out", "v out"])
    _worker(eng, provider).advance_multi_agent_runs()
    assert provider.calls == 3
    assert provider.seen[0]["system_prompt"] == RESEARCHER_SYSTEM_PROMPT
    assert provider.seen[1]["system_prompt"] == SOLUTION_SPECIALIST_SYSTEM_PROMPT
    assert provider.seen[2]["system_prompt"] == REVIEWER_SYSTEM_PROMPT


def test_same_sealed_provider_model_for_all_roles(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider(["r out", "s out", "v out"])
    _worker(eng, provider).advance_multi_agent_runs()
    assert all(c["model"] == "snap-model" for c in provider.seen)


def test_no_fallback_chain_provider_identity_is_only_the_sealed_snapshot(eng):
    """The worker's provider FACTORY is called with the ChatInput carrying
    exactly the sealed submission's provider/model — never a deployment
    default or per-request override — proving no fallback substitution
    happened before the fake provider was even invoked."""
    rid = _seed(eng)
    seen_inputs = []

    def factory(chat_input, *, db, user_id):
        seen_inputs.append(chat_input)
        return RoleAwareProvider(["r", "s", "v"])

    from app.run_engine.worker import RunWorker
    RunWorker(mk_factory(eng), chat_provider=factory).advance_multi_agent_runs()
    assert len(seen_inputs) == 3
    assert all(ci.provider == "snap-prov" and ci.model == "snap-model" for ci in seen_inputs)


# ── P5-P7: sealed-before-invoked, only validated parents consumed ─────


def test_researcher_content_reaches_solution_specialist_prompt(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider([f"{CANARY}-researcher", "solution text", "final text"])
    _worker(eng, provider).advance_multi_agent_runs()
    solution_prompt_text = provider.seen[1]["messages"][0]["content"]
    assert f"{CANARY}-researcher" in solution_prompt_text
    assert "Researcher findings" in solution_prompt_text  # untrusted-block label present


def test_reviewer_consumes_both_prior_validated_artifacts(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider([f"{CANARY}-r", f"{CANARY}-s", "final"])
    _worker(eng, provider).advance_multi_agent_runs()
    reviewer_prompt_text = provider.seen[2]["messages"][0]["content"]
    assert f"{CANARY}-r" in reviewer_prompt_text
    assert f"{CANARY}-s" in reviewer_prompt_text
    assert "Solution Specialist proposal" in reviewer_prompt_text


# ── P8-P9: exactly one final message, content equals validated output ──


def test_exactly_one_final_message_and_content_matches(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider(["r", "s", "the exact final answer"])
    _worker(eng, provider).advance_multi_agent_runs()
    db = mk_session(eng)
    try:
        msgs = db.query(Message).filter(Message.role == MessageRole.assistant).all()
        assert len(msgs) == 1
        assert msgs[0].content == "the exact final answer"
    finally:
        db.close()


# ── P10: citations equal markers referenced, first-reference order ─────


def test_citations_first_reference_order_and_dedup(eng):
    rid = _seed(eng, context=chat_stream.build_context_snapshot(
        rag_context_text="ctx", sources_used=True,
        citations=[
            {"marker": "[1]", "document_id": 1, "chunk_id": 1, "filename": "a", "page_number": None, "snippet": "s1"},
            {"marker": "[2]", "document_id": 2, "chunk_id": 2, "filename": "b", "page_number": None, "snippet": "s2"},
        ],
    ))
    provider = RoleAwareProvider(["r", "s", "cite [2] then [2] again then [1]."])
    _worker(eng, provider).advance_multi_agent_runs()
    db = mk_session(eng)
    try:
        msg = db.query(Message).filter(Message.role == MessageRole.assistant).first()
        markers = [c["marker"] for c in json.loads(msg.citations)]
        assert markers == ["[2]", "[1]"]
    finally:
        db.close()


# ── P11: no intermediate role output leaks anywhere non-plaintext-scoped ──


def test_no_intermediate_role_output_in_message_events_refs(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider([f"{CANARY}-researcher-private",
                                  f"{CANARY}-solution-private", "public final text"])
    _worker(eng, provider).advance_multi_agent_runs()
    db = mk_session(eng)
    try:
        msg = db.query(Message).filter(Message.role == MessageRole.assistant).first()
        assert CANARY not in msg.content
        events = db.query(RunEvent).filter(RunEvent.run_id == rid).all()
        blob = "\n".join(e.data or "" for e in events)
        assert CANARY not in blob
        for step in db.query(RunStep).filter(RunStep.run_id == rid).all():
            assert CANARY not in (step.input_ref or "")
            assert CANARY not in (step.output_ref or "")
            assert CANARY not in (step.error_code or "")
    finally:
        db.close()


# ── P12: progress events appear in exact truthful order ────────────────


def test_progress_events_exact_order(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider(["r", "s", "v"])
    _worker(eng, provider).advance_multi_agent_runs()
    db = mk_session(eng)
    try:
        rows = (db.query(RunEvent)
                .filter(RunEvent.run_id == rid, RunEvent.type == "agent.progress")
                .order_by(RunEvent.run_seq.asc()).all())
        codes = [json.loads(r.data)["code"] for r in rows]
        assert codes == [
            "agent_researching", "agent_research_complete",
            "agent_solving", "agent_solution_complete",
            "agent_reviewing", "agent_finalizing",
        ]
        from app.run_engine.messages_stream import _map_event
        for row, code in zip(rows, codes):
            wire, terminal = _map_event(db, None, row)
            assert not terminal
            assert "event: agent_progress" in wire
            assert json.loads(wire.split("data: ", 1)[1]) == {"code": code}
    finally:
        db.close()


def test_progress_event_payload_contains_only_the_code(eng):
    rid = _seed(eng)
    _worker(eng, RoleAwareProvider(["r", "s", "v"])).advance_multi_agent_runs()
    db = mk_session(eng)
    try:
        rows = (db.query(RunEvent)
                .filter(RunEvent.run_id == rid, RunEvent.type == "agent.progress").all())
        for r in rows:
            assert set(json.loads(r.data).keys()) == {"code"}
    finally:
        db.close()


# ── P13: SSE replay has no gap/duplicate ────────────────────────────────


def test_event_replay_cursor_has_no_gap_or_duplicate(eng):
    rid = _seed(eng)
    _worker(eng, RoleAwareProvider(["r", "s", "v"])).advance_multi_agent_runs()
    db = mk_session(eng)
    try:
        from app.run_engine.events import read_run_events

        ctx = _ctx_for(eng)
        first = read_run_events(db, ctx, rid, after_seq=0)
        seqs1 = [e.run_seq for e in first]
        cursor = seqs1[len(seqs1) // 2]
        second = read_run_events(db, ctx, rid, after_seq=cursor)
        seqs2 = [e.run_seq for e in second]
        assert all(s > cursor for s in seqs2)          # no gap-before-cursor duplication
        assert seqs1 == sorted(set(seqs1))              # strictly increasing, no dup
        assert seqs2 == [s for s in seqs1 if s > cursor]  # exact tail, nothing missing
    finally:
        db.close()


# ── P16: flags remain OFF by default ────────────────────────────────────


def test_multi_agent_flags_off_by_default_in_real_settings():
    from app.config import Settings

    s = Settings()
    assert s.run_multi_agent_enabled is False


# ═══════════════ Q — failure / recovery / cancellation ═══════════════


def test_cancellation_before_researcher_zero_provider_calls(eng):
    rid = _seed(eng)
    db = mk_session(eng)
    try:
        ctx = make_ctx(db, user_id=7, sid="fam-alpha")
        from app.run_engine.engine import request_cancellation
        request_cancellation(db, ctx, rid)
    finally:
        db.close()
    provider = RoleAwareProvider(["r", "s", "v"])
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "cancelled"
    assert provider.calls == 0


def test_cancellation_between_roles(eng):
    """A cancel arriving after Researcher succeeds but before Solution
    Specialist's provider call starts is honored BEFORE that call, not only
    mid-stream — proven via a provider whose first call cancels the run as
    a side effect, never letting a second call happen."""
    rid = _seed(eng)

    class _CancelAfterFirst(RoleAwareProvider):
        async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
            if self.calls == 0:
                d = mk_session(eng)
                try:
                    ctx = make_ctx(d, user_id=7, sid="fam-alpha")
                    from app.run_engine.engine import request_cancellation
                    request_cancellation(d, ctx, rid)
                finally:
                    d.close()
            async for chunk in super().generate_stream_async(messages, system_prompt, model_name):
                yield chunk

    provider = _CancelAfterFirst(["r", "s", "v"])
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "cancelled"
    assert provider.calls == 1  # Researcher ran; Solution Specialist never invoked


def test_provider_timeout_for_a_role_fails_the_run(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 1)
    monkeypatch.setattr(settings, "multi_agent_role_timeout_s", 2.0)
    rid = _seed(eng)
    provider = RoleAwareProvider(["r", "s", "v"], timeout_at_call=1)
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "failed" and out[0].detail == "provider_timeout"
    db = mk_session(eng)
    try:
        step = (db.query(RunStep).filter(RunStep.run_id == rid,
                                         RunStep.kind == "agent_solution").first())
        assert step.status == "incomplete" and step.error_code == "provider_timeout"
    finally:
        db.close()


def test_provider_error_for_a_role_fails_the_run_without_leaking_provider_text(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider(["r", "s", "v"], error_at_call=0)
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "failed" and out[0].detail == "provider_error"
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        assert "UPSTREAM-SECRET" not in (run.status_reason or "")
    finally:
        db.close()


def test_resume_from_sealed_researcher_artifact_without_recalling_researcher(eng):
    """First sweep runs Researcher then dies mid-Solution-Specialist
    (simulated via error_at_call=1, which fails the RUN, not a crash — for
    a genuine crash-recovery proof see the expired-lease test below). This
    test instead directly proves the CONTINUATION contract: seed a run with
    the Researcher step already sealed by a real prior call, then verify a
    fresh sweep with a provider that would raise if Researcher were
    re-invoked completes successfully having called the provider only
    twice (Solution Specialist + Reviewer)."""
    rid = _seed(eng)
    # First sweep: let Researcher succeed, then force Solution Specialist to
    # fail via a raised exception so the RUN fails but Researcher's artifact
    # stays sealed.
    first = RoleAwareProvider(["researcher output"], raises_at_call=1)
    out1 = _worker(eng, first).advance_multi_agent_runs()
    assert out1[0].status == "failed" and first.calls == 2

    db = mk_session(eng)
    try:
        researcher_step = (db.query(RunStep)
                           .filter(RunStep.run_id == rid, RunStep.kind == "agent_research")
                           .first())
        assert researcher_step.status == "succeeded"
        # Force the run back to a resumable state for this test's purpose —
        # a real resume happens via lease expiry (see the outcome_unknown
        # test below); here we only need to prove Researcher is never
        # re-invoked once its artifact is sealed.
        from app.models.models import Run as _Run
        db.execute(__import__("sqlalchemy").update(_Run).where(_Run.id == rid).values(
            status="queued", lease_owner=None, lease_expires_at=None))
        db.commit()
    finally:
        db.close()

    class _FailsIfResearcherInvoked(RoleAwareProvider):
        async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
            if system_prompt == RESEARCHER_SYSTEM_PROMPT:
                raise AssertionError("Researcher must not be re-invoked once sealed")
            async for chunk in super().generate_stream_async(messages, system_prompt, model_name):
                yield chunk

    second = _FailsIfResearcherInvoked(["unused", "solution output", "final output"])
    out2 = _worker(eng, second).advance_multi_agent_runs()
    assert out2[0].status == "succeeded"
    assert second.calls == 2  # only Solution Specialist + Reviewer


def test_expired_running_role_step_becomes_outcome_unknown(eng):
    """A crash after the pending->running commit (durable invocation-start
    proof) but before the artifact is sealed must NEVER be re-invoked —
    proven by manually placing a role step in `running` with an expired
    lease and confirming the sweep declares outcome_unknown without calling
    any provider."""
    rid = _seed(eng)
    import datetime as _dt
    db = mk_session(eng)
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        from app.models.models import Run as _Run
        db.execute(__import__("sqlalchemy").update(_Run).where(_Run.id == rid).values(
            status="running", lease_owner="dead/worker", lease_expires_at=now - _dt.timedelta(seconds=60),
            run_deadline_at=now + _dt.timedelta(seconds=300),
        ))
        db.add(RunStep(id=str(uuid.uuid4()), run_id=rid, user_id=7, session_id=1, seq=2,
                       kind="agent_research", engine="native", status="running",
                       started_at=now))
        db.commit()
    finally:
        db.close()

    provider = RoleAwareProvider(["r", "s", "v"])
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "outcome_unknown"
    assert provider.calls == 0


def test_pending_role_step_with_expired_lease_is_reclaimed_and_invoked_once(eng):
    """A crash BEFORE the pending->running commit is safe to reclaim — the
    pending row is reused (not duplicated) and the provider is called
    exactly once for that role."""
    rid = _seed(eng)
    import datetime as _dt
    db = mk_session(eng)
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        from app.models.models import Run as _Run
        db.execute(__import__("sqlalchemy").update(_Run).where(_Run.id == rid).values(
            status="running", lease_owner="dead/worker", lease_expires_at=now - _dt.timedelta(seconds=60),
            run_deadline_at=now + _dt.timedelta(seconds=300),
        ))
        db.add(RunStep(id=str(uuid.uuid4()), run_id=rid, user_id=7, session_id=1, seq=2,
                       kind="agent_research", engine="native", status="pending"))
        db.commit()
        pending_count_before = db.query(RunStep).filter(
            RunStep.run_id == rid, RunStep.kind == "agent_research").count()
    finally:
        db.close()

    provider = RoleAwareProvider(["r", "s", "v"])
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "succeeded"
    db = mk_session(eng)
    try:
        rows = db.query(RunStep).filter(RunStep.run_id == rid, RunStep.kind == "agent_research").all()
        assert len(rows) == 1 and rows[0].status == "succeeded"
    finally:
        db.close()


def test_marker_emitted_by_a_role_fails_the_run_closed(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider(["researcher ok", "[python_code] evil [/python_code]", "v"])
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "failed" and out[0].detail == "tool_resume_required"
    db = mk_session(eng)
    try:
        step = db.query(RunStep).filter(RunStep.run_id == rid, RunStep.kind == "agent_solution").first()
        assert step.status == "incomplete"
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
    finally:
        db.close()


def test_unknown_numeric_citation_fails_closed(eng):
    rid = _seed(eng, context=_context_with_citation())
    provider = RoleAwareProvider(["r", "s", "cites an unknown [99] marker."])
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "failed" and out[0].detail == "citation_validation_failed"


@pytest.mark.parametrize("role_index", [0, 1, 2])
@pytest.mark.parametrize("tail", ["[", "[MCP_CA", "safe text [save_mem"])
def test_held_marker_tail_fails_closed_in_every_role(eng, role_index, tail):
    rid = _seed(eng)
    responses = ["research", "solution", "review"]
    responses[role_index] = tail
    provider = RoleAwareProvider(responses)
    outcome = _worker(eng, provider).advance_multi_agent_runs()[0]
    assert outcome.status == "failed" and outcome.detail == "tool_resume_required"
    assert provider.calls == role_index + 1
    with mk_session(eng) as db:
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
        assert db.query(RunEvent).filter(RunEvent.run_id == rid, RunEvent.category == "text").count() == 0


def test_web_marker_fails_closed(eng):
    rid = _seed(eng, context=_context_with_citation())
    provider = RoleAwareProvider(["r", "s", "cites a web marker [W1] we never sealed."])
    out = _worker(eng, provider).advance_multi_agent_runs()
    assert out[0].status == "failed" and out[0].detail == "citation_validation_failed"


def test_resweep_produces_no_second_message(eng):
    rid = _seed(eng)
    _worker(eng, RoleAwareProvider(["r", "s", "final"])).advance_multi_agent_runs()
    # Run is now terminal (succeeded) — a stray extra sweep must be a no-op.
    out2 = _worker(eng, RoleAwareProvider(["r2", "s2", "final2"])).advance_multi_agent_runs()
    assert out2 == []  # terminal — not even selected by the claim query
    db = mk_session(eng)
    try:
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 1
    finally:
        db.close()


def test_stale_worker_cannot_finalize(eng):
    """A worker whose lease was stolen (version bumped by someone else)
    between sealing the Reviewer artifact and finalizing must not create
    the Message — proven by directly calling the finalize path with a
    ClaimedRun carrying a stale version."""
    rid = _seed(eng)
    _worker(eng, RoleAwareProvider(["r", "s", "final"])).advance_multi_agent_runs()
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        assert run.status == "succeeded"  # already finalized by the real flow above
        # Simulate a stale re-finalize attempt against a run already moved on:
        from app.run_engine.worker import ClaimedRun
        stale = ClaimedRun(run_id=rid, kind="multi_agent", user_id=7, tenant_id=None,
                           auth_session_ref="fam-alpha", lease_owner="stale/owner",
                           version=run.version + 999, attempt=0)
        reviewer_step = db.query(RunStep).filter(RunStep.run_id == rid,
                                                 RunStep.kind == "agent_review").first()
        w = _worker(eng, RoleAwareProvider([]))
        result = w._finalize_multi_agent_success(db, stale, reviewer_step)
        assert result.status == "abandoned"
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 1
    finally:
        db.close()


def test_secret_canary_absent_from_status_reason_and_ciphertext(eng):
    rid = _seed(eng)
    provider = RoleAwareProvider([f"{CANARY}-r", f"{CANARY}-s", "public final"])
    _worker(eng, provider).advance_multi_agent_runs()
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        assert CANARY not in (run.status_reason or "")
        from app.models.models import RunPayload
        for p in db.query(RunPayload).filter(RunPayload.run_id == rid).all():
            assert CANARY not in p.ciphertext
    finally:
        db.close()



# ═══════════ R — crash-safe final-text publication (D5C closeout) ═══════════


class _SimulatedCrashMidPublish(RuntimeError):
    """Stands in for the process dying between two committed text-event
    chunks — raised from inside `TextEventBuffer._flush`."""


def _drive_to_partial_final_text_crash(eng, monkeypatch, final_text):
    """Drive a REAL three-role multi-agent run to the exact crash window:
    validated Reviewer text partially committed as text events, no assistant
    Message, run still `running`. Returns the run id, with the dead worker's
    lease already expired so a recovery sweep can claim it.

    `TextEventBuffer` commits EACH capped chunk in its OWN session, so
    interrupting the SECOND chunk write leaves a genuinely partial committed
    prefix — the state a boolean "already published?" guard mishandles.
    """
    from sqlalchemy import update

    from app.run_engine import text_events

    rid = _seed(eng)
    real_append = text_events.append_text_event
    state = {"armed": True, "writes": 0}

    def _interrupt_on_second_chunk(*args, **kwargs):
        state["writes"] += 1
        if state["armed"] and state["writes"] == 2:
            raise _SimulatedCrashMidPublish()
        return real_append(*args, **kwargs)

    monkeypatch.setattr(text_events, "append_text_event", _interrupt_on_second_chunk)
    provider = RoleAwareProvider(["researcher output", "solution output", final_text])
    with pytest.raises(_SimulatedCrashMidPublish):
        _worker(eng, provider).advance_multi_agent_runs()
    assert provider.calls == 3   # all three roles really ran before the crash
    assert state["writes"] == 2  # chunk 1 committed, chunk 2 interrupted
    state["armed"] = False

    import datetime as _dt
    from sqlalchemy import update as _update

    db = mk_session(eng)
    try:
        persisted = "".join(
            json.loads(e.data)["text"]
            for e in (db.query(RunEvent)
                      .filter(RunEvent.run_id == rid, RunEvent.category == "text")
                      .order_by(RunEvent.run_seq.asc()).all())
        )
        assert 0 < len(persisted) < len(final_text)   # really a PARTIAL prefix
        assert final_text.startswith(persisted)
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
        run = db.query(Run).filter(Run.id == rid).first()
        assert run.status == "running"                # recoverable, not terminal
        db.execute(_update(Run).where(Run.id == rid).values(
            lease_expires_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=60)))
        db.commit()
    finally:
        db.close()
    return rid


def _replayed_text(eng, rid):
    db = mk_session(eng)
    try:
        return "".join(
            json.loads(e.data)["text"]
            for e in (db.query(RunEvent)
                      .filter(RunEvent.run_id == rid, RunEvent.category == "text")
                      .order_by(RunEvent.run_seq.asc()).all())
        )
    finally:
        db.close()


def test_partial_final_text_publish_recovers_exact_suffix_without_gap_or_duplicate(
    eng, monkeypatch,
):
    """A crash AFTER some validated Reviewer text chunks are committed but
    BEFORE the final Message/run commit must neither drop the unpublished
    suffix nor duplicate the published prefix. Recovery continues from the
    EXACT persisted prefix, re-invokes no provider, and creates exactly one
    Message carrying the full validated text."""
    # Longer than run_text_event_max_bytes so _flush writes >1 capped chunk.
    final_text = "".join(chr(ord("a") + (i % 26)) for i in range(12000))
    rid = _drive_to_partial_final_text_crash(eng, monkeypatch, final_text)

    recovery = RoleAwareProvider([])          # yields nothing if ever invoked
    out = _worker(eng, recovery).advance_multi_agent_runs()
    assert recovery.calls == 0                # sealed Reviewer artifact reused
    assert out[0].status == "succeeded"

    assert _replayed_text(eng, rid) == final_text   # no gap, no duplicate
    db = mk_session(eng)
    try:
        msgs = db.query(Message).filter(Message.role == MessageRole.assistant).all()
        assert len(msgs) == 1                 # no second Message
        assert msgs[0].content == final_text
    finally:
        db.close()


def test_unexpected_persisted_text_fails_closed_without_a_message(eng, monkeypatch):
    """Persisted text that is NOT an exact prefix of the validated Reviewer
    artifact is corrupt/unexpected state: finalization must fail closed and
    publish nothing rather than show text the Reviewer never produced."""
    final_text = "".join(chr(ord("a") + (i % 26)) for i in range(12000))
    rid = _drive_to_partial_final_text_crash(eng, monkeypatch, final_text)

    from app.run_engine.text_events import append_text_event

    db = mk_session(eng)
    try:
        append_text_event(db, run_id=rid, tenant_id=None, user_id=7,
                          text="NOT-A-PREFIX-OF-THE-REVIEWER-OUTPUT")
    finally:
        db.close()

    before = _replayed_text(eng, rid)
    assert not final_text.startswith(before)   # the corrupt state is real

    out = _worker(eng, RoleAwareProvider([])).advance_multi_agent_runs()
    assert out[0].status == "failed"
    assert out[0].detail == "text_recovery_mismatch"
    assert _replayed_text(eng, rid) == before   # nothing more was published
    db = mk_session(eng)
    try:
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
    finally:
        db.close()
