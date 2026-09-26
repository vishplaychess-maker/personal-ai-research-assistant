"""MP-4C - durable nonce store: tests FIRST (RED before implementation).

The store is the REPLAY GUARD ONLY - not an approval system. It is a single
broker key plus a persistent SQLite volume. Decisions pinned here:
  * a jti is consumed ATOMICALLY and exactly once;
  * the record survives a process restart on the same volume;
  * an unavailable or corrupt store fails CLOSED (zero starts);
  * code, tokens and raw output are NEVER written;
  * records are retained until expiry + skew, then pruned.
"""
from __future__ import annotations

import threading

import pytest

from executor_service.nonce_store import NonceStore, NonceStoreError

CANARY = "MP4C_NONCE_CANARY_" + "n" * 12


def _store(tmp_path, name="nonces.db"):
    return NonceStore(str(tmp_path / name))


# -- consume exactly once -------------------------------------------


def test_a_fresh_jti_is_consumed_once(tmp_path):
    st = _store(tmp_path)
    try:
        assert st.consume("jti-1", expires_at=10_000, now=9_000) is True
        assert st.consume("jti-1", expires_at=10_000, now=9_000) is False
    finally:
        st.close()


def test_distinct_jtis_are_independent(tmp_path):
    st = _store(tmp_path)
    try:
        assert st.consume("a", expires_at=10_000, now=9_000) is True
        assert st.consume("b", expires_at=10_000, now=9_000) is True
        assert st.consume("a", expires_at=10_000, now=9_000) is False
    finally:
        st.close()


def test_concurrent_consumption_of_one_jti_yields_exactly_one_winner(tmp_path):
    st = _store(tmp_path)
    wins = []
    lock = threading.Lock()
    start = threading.Barrier(16)

    def worker():
        start.wait()
        if st.consume("shared", expires_at=10_000, now=9_000):
            with lock:
                wins.append(1)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    st.close()
    assert len(wins) == 1, wins


# -- the RESTART property (same volume) -----------------------------


def test_a_consumed_jti_survives_a_store_reopen(tmp_path):
    path = str(tmp_path / "nonces.db")
    first = NonceStore(path)
    try:
        assert first.consume("persisted", expires_at=10_000, now=9_000) is True
    finally:
        first.close()
    second = NonceStore(path)          # a RESTART on the same volume
    try:
        assert second.consume("persisted", expires_at=10_000, now=9_000) is False
    finally:
        second.close()


def test_a_fresh_volume_starts_empty(tmp_path):
    """Documents the boundary: a DIFFERENT volume cannot reject the replay."""
    a = NonceStore(str(tmp_path / "a.db"))
    b = NonceStore(str(tmp_path / "b.db"))
    try:
        assert a.consume("x", expires_at=10_000, now=9_000) is True
        assert b.consume("x", expires_at=10_000, now=9_000) is True
    finally:
        a.close()
        b.close()


# -- fail closed -----------------------------------------------------


def test_an_unavailable_store_raises(tmp_path):
    missing_dir = str(tmp_path / "nope" / "nonces.db")
    with pytest.raises(NonceStoreError):
        NonceStore(missing_dir)


def test_a_corrupt_store_raises_on_consume(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a sqlite database at all\x00\x01")
    with pytest.raises(NonceStoreError):
        st = NonceStore(str(path))
        st.consume("jti", expires_at=10_000, now=9_000)


def test_a_corrupt_store_raises_on_open(tmp_path):
    path = tmp_path / "corrupt2.db"
    path.write_bytes(b"garbage" * 100)
    with pytest.raises(NonceStoreError):
        NonceStore(str(path))


# -- retention / pruning --------------------------------------------


def test_records_are_retained_until_expiry_plus_skew(tmp_path):
    st = _store(tmp_path)
    try:
        st.consume("j", expires_at=1_000, now=900)
        assert st.prune(now=1_000 + 30, skew=30) == 0
        assert st.consume("j", expires_at=1_000, now=1_000) is False
        assert st.prune(now=1_000 + 31, skew=30) == 1
        assert st.count() == 0
    finally:
        st.close()


# -- the store never holds sensitive material ------------------------


def test_the_store_contains_no_code_token_or_raw_output(tmp_path):
    path = str(tmp_path / "nonces.db")
    st = NonceStore(path)
    try:
        st.consume("jti-" + CANARY, expires_at=10_000, now=9_000)
    finally:
        st.close()
    blob = open(path, "rb").read()
    assert CANARY.encode() not in blob, "the jti must not be stored verbatim"
    assert b"print(" not in blob
    assert b"Bearer" not in blob


def test_the_schema_has_only_replay_columns(tmp_path):
    path = str(tmp_path / "nonces.db")
    st = NonceStore(path)
    try:
        cols = st.columns()
    finally:
        st.close()
    assert set(cols) == {"jti_hash", "expires_at", "created_at"}, cols



def test_seen_is_non_destructive(tmp_path):
    """`seen` only READS: it must never insert, consume or delete, so it can be
    used to classify a refusal that has already decided not to burn the grant."""
    from executor_service.nonce_store import NonceStore

    store = NonceStore(str(tmp_path / "seen.db"))
    try:
        assert store.seen("jti-seen") is False
        assert store.count() == 0, "seen must not insert"
        assert store.consume("jti-seen", expires_at=9_999_999_999, now=1) is True
        assert store.seen("jti-seen") is True
        assert store.seen("jti-other") is False
        assert store.count() == 1, "seen must not consume or insert"
    finally:
        store.close()
