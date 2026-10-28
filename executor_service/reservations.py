"""MP-4C - PRE-DISPATCH capacity reservations.

The smallest contract that lets the backend discover "busy" BEFORE the gateway
commits DispatchRecord(outcome='started'):

  reserve  -> acquire one user slot AND one global slot for a BOUND ACTION
  consume  -> the matching signed execution grant takes that slot over
  release  -> the backend abandons the reservation (denial / cancel)
  finish   -> the slot is returned after the run

Reserving NEVER runs code: no argv, no container, no nonce is touched here.

Scope and honesty: this book is IN-PROCESS. A broker restart loses every
reservation, so a later execution grant then fails CLOSED with
executor_reservation_missing and ZERO containers start. Reservations are
deliberately NOT durable: durability belongs to the nonce store, and the decision
explicitly forbids claiming an atomic transaction spanning the application DB and
this store.

Authority: a reservation is a CAPACITY claim, not an approval. The application's
DispatchRecord remains the approval/execution authority.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from executor_service.quota import QuotaBroker

STATUS_RESERVED = "executor_reserved"
STATUS_RESERVATION_EXISTS = "executor_reservation_exists"
STATUS_BUSY = "executor_quota_exceeded"


@dataclass
class Reservation:
    key: str
    user_id: int
    expires_at: int
    consumed: bool = False


class ReservationBook:
    """Reservations keyed by the BOUND ACTION, sharing the service's quota broker."""

    def __init__(self, broker: Optional[QuotaBroker] = None) -> None:
        self.broker = broker if broker is not None else QuotaBroker()
        self._lock = threading.Lock()
        self._by_key: Dict[str, Reservation] = {}

    # -- internals ---------------------------------------------------
    def _drop_locked(self, record: Reservation) -> None:
        self._by_key.pop(record.key, None)
        self.broker.release(record.user_id)

    def _prune_locked(self, now: int) -> int:
        gone = 0
        for record in list(self._by_key.values()):
            if record.expires_at < int(now):
                self._drop_locked(record)
                gone += 1
        return gone

    # -- contract ----------------------------------------------------
    def reserve(self, *, key: str, user_id: int, expires_at: int,
                now: int) -> Tuple[bool, str]:
        """Acquire a slot for this bound action. Refuses a duplicate or a busy cap."""
        with self._lock:
            self._prune_locked(now)
            if key in self._by_key:
                return False, STATUS_RESERVATION_EXISTS
            if not self.broker.try_acquire(user_id):
                return False, STATUS_BUSY
            self._by_key[key] = Reservation(key=key, user_id=int(user_id),
                                            expires_at=int(expires_at))
            return True, STATUS_RESERVED

    def peek(self, key: str, now: int) -> Optional[Reservation]:
        """The live, unconsumed reservation for this bound action, if any."""
        with self._lock:
            self._prune_locked(now)
            record = self._by_key.get(key)
            if record is None or record.consumed:
                return None
            return record

    def consume(self, key: str, now: int) -> Optional[Reservation]:
        """Atomically take over the reservation. None = missing/expired/replayed."""
        with self._lock:
            self._prune_locked(now)
            record = self._by_key.get(key)
            if record is None or record.consumed:
                return None
            record.consumed = True
            return record

    def finish(self, key: str) -> bool:
        """Return the slot after a run (consumed or not)."""
        with self._lock:
            record = self._by_key.pop(key, None)
            if record is None:
                return False
            self.broker.release(record.user_id)
            return True

    def release(self, key: str, now: int) -> bool:
        """The backend abandons the reservation (denial / cancel)."""
        with self._lock:
            self._prune_locked(now)
            record = self._by_key.pop(key, None)
            if record is None:
                return False
            self.broker.release(record.user_id)
            return True

    def live_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._by_key.values() if not r.consumed)

    def prune(self, now: int) -> int:
        with self._lock:
            return self._prune_locked(now)


__all__ = ["ReservationBook", "Reservation", "STATUS_RESERVED",
           "STATUS_RESERVATION_EXISTS", "STATUS_BUSY"]
