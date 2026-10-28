"""MP-4C — atomic single-instance quotas.

One ACTIVE execution per user, two GLOBALLY. Acquire happens BEFORE any container
is started; a request that cannot acquire is REJECTED — never queued, never
started. All operations take one lock, so the caps hold under contention.

Scope and limits (stated, not implied): this is a PROCESS-LOCAL broker. It is
correct for the single-instance executor service this slice builds. A
multi-instance deployment needs separately designed and tested shared
coordination; it is NOT provided here.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict

PER_USER_ACTIVE = 1
GLOBAL_ACTIVE = 2


@dataclass
class QuotaBroker:
    per_user: int = PER_USER_ACTIVE
    global_limit: int = GLOBAL_ACTIVE
    _active: Dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def try_acquire(self, user_id: int) -> bool:
        with self._lock:
            uid = int(user_id)
            if self._active.get(uid, 0) >= self.per_user:
                return False
            if sum(self._active.values()) >= self.global_limit:
                return False
            self._active[uid] = self._active.get(uid, 0) + 1
            return True

    def release(self, user_id: int) -> None:
        with self._lock:
            uid = int(user_id)
            cur = self._active.get(uid, 0)
            if cur <= 1:
                self._active.pop(uid, None)
            else:
                self._active[uid] = cur - 1

    def active_total(self) -> int:
        with self._lock:
            return sum(self._active.values())
