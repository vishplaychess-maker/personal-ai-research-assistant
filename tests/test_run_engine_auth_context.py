"""Phase 1B.2R MP-1A — access-token `sid` claim + AuthContext session binding."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from jose import jwt

from app.config import settings
from app.database import Base
from app.models.models import RefreshSession, User
from app.run_engine.context import build_auth_context
from tests.run_engine_helpers import mk_engine, mk_session


# ── unit: build_auth_context against a hermetic DB ────────────────────


@pytest.fixture
def db():
    eng = mk_engine()
    s = mk_session(eng)
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _user(db, uid=1):
    u = User(id=uid, username=f"u{uid}", email=f"u{uid}@t.co", hashed_password="x")
    db.add(u)
    db.commit()
    return u


def _family(db, user_id, family_id, *, revoked=False, ttl_days=30):
    now = dt.datetime.utcnow()
    row = RefreshSession(
        user_id=user_id,
        family_id=family_id,
        token_hash=uuid.uuid4().hex,
        expires_at=now + dt.timedelta(days=ttl_days),
        revoked_at=now if revoked else None,
        created_at=now,
    )
    db.add(row)
    db.commit()


def test_valid_family_yields_bound_context(db):
    u = _user(db)
    _family(db, u.id, "fam-1")
    ctx = build_auth_context(db, user=u, token_payload={"sub": "1", "sid": "fam-1"})
    assert ctx.auth_session_ref == "fam-1"
    assert ctx.durable_session_binding_valid is True
    assert ctx.user_id == 1
    assert ctx.tenant_id is None


def test_missing_sid_claim_is_unbound(db):
    u = _user(db)
    _family(db, u.id, "fam-1")
    ctx = build_auth_context(db, user=u, token_payload={"sub": "1"})
    assert ctx.auth_session_ref is None
    assert ctx.durable_session_binding_valid is False


def test_revoked_family_is_unbound(db):
    u = _user(db)
    _family(db, u.id, "fam-r", revoked=True)
    ctx = build_auth_context(db, user=u, token_payload={"sub": "1", "sid": "fam-r"})
    assert ctx.durable_session_binding_valid is False


def test_expired_family_is_unbound(db):
    u = _user(db)
    _family(db, u.id, "fam-e", ttl_days=-1)
    ctx = build_auth_context(db, user=u, token_payload={"sub": "1", "sid": "fam-e"})
    assert ctx.durable_session_binding_valid is False


def test_unknown_sid_is_unbound(db):
    u = _user(db)
    ctx = build_auth_context(db, user=u, token_payload={"sub": "1", "sid": "nope"})
    assert ctx.durable_session_binding_valid is False


def test_blank_sid_is_normalized_to_none(db):
    u = _user(db)
    ctx = build_auth_context(db, user=u, token_payload={"sub": "1", "sid": "   "})
    assert ctx.auth_session_ref is None
    assert ctx.durable_session_binding_valid is False


# ── integration: login / refresh mint a bound access token ────────────


def _decode(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


def _register_login(client):
    name = f"testuser_{uuid.uuid4().hex[:8]}"
    r = client.post("/api/auth/register", json={
        "username": name, "email": f"{name}@t.co", "password": "securePass123!",
    })
    assert r.status_code == 201
    lg = client.post("/api/auth/login", json={
        "username": name, "password": "securePass123!",
    })
    assert lg.status_code == 200
    return name, lg


def test_login_access_token_carries_sid(client):
    from app.database import SessionLocal

    name, lg = _register_login(client)
    claims = _decode(lg.json()["access_token"])
    assert "sid" in claims and claims["sid"]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == name).first()
        fams = {s.family_id for s in db.query(RefreshSession).filter(
            RefreshSession.user_id == user.id
        ).all()}
    finally:
        db.close()
    assert claims["sid"] in fams


def test_refresh_keeps_the_same_sid(client):
    _name, lg = _register_login(client)
    sid_1 = _decode(lg.json()["access_token"])["sid"]

    rf = client.post("/api/auth/refresh", cookies=lg.cookies)
    assert rf.status_code == 200, rf.text
    sid_2 = _decode(rf.json()["access_token"])["sid"]
    assert sid_2 == sid_1  # family_id is stable across rotation


def test_login_creates_exactly_one_family(client):
    from app.database import SessionLocal

    name, _lg = _register_login(client)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == name).first()
        rows = db.query(RefreshSession).filter(RefreshSession.user_id == user.id).all()
    finally:
        db.close()
    assert len({r.family_id for r in rows}) == 1
    assert len(rows) == 1


def test_login_access_token_failure_revokes_the_orphan_refresh_family(client, monkeypatch):
    """If access-token signing fails AFTER the refresh session is persisted, the
    undisclosed session must be revoked so families cannot accumulate."""
    from app.database import SessionLocal
    import app.routes.auth as auth_routes

    name = f"testuser_{uuid.uuid4().hex[:8]}"
    assert client.post("/api/auth/register", json={
        "username": name, "email": f"{name}@t.co", "password": "securePass123!",
    }).status_code == 201

    def boom(*a, **k):
        raise RuntimeError("token signer down")

    monkeypatch.setattr(auth_routes, "create_access_token", boom)
    # TestClient re-raises unhandled server exceptions — the failure is surfaced,
    # never swallowed; the orphan-session cleanup runs before it propagates.
    with pytest.raises(RuntimeError):
        client.post("/api/auth/login", json={
            "username": name, "password": "securePass123!",
        })

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == name).first()
        active = db.query(RefreshSession).filter(
            RefreshSession.user_id == user.id,
            RefreshSession.revoked_at.is_(None),
        ).count()
    finally:
        db.close()
    assert active == 0  # no orphan active family left behind
