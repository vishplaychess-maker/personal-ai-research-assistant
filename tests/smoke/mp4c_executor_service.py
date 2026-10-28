"""MP-4C — TEST-ONLY prototype over the PROMOTED execution contract.

The fixed argv, the stdin-only `ExecutionPlan` and the atomic caps are now
authoritative in `executor_service/` (the separately runnable service), so this
module no longer defines a second copy of any of them.

What remains here is the `ExecutorService` PROTOTYPE used by the earlier
in-process contract tests: it exercises guard ORDERING (every rejection happens
before anything is recorded as started) without launching a container.
"""
from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from executor_service import image
from executor_service.contract import (  # noqa: F401
    CONTAINER_PREFIX,
    DEFAULT_OWNER,
    DOCKER_STDIN_FLAG,
    MAX_CODE_BYTES,
    OWNER_LABEL,
    PYTHON_ARGV,
    ExecutionPlan,
    build_argv,
    build_plan,
)
from executor_service.quota import (  # noqa: F401
    GLOBAL_ACTIVE,
    PER_USER_ACTIVE,
    QuotaBroker as SingleBroker,
)

#: Bounded, machine-readable rejection reasons (never code / argv / host paths).
REJECT_UNAUTHORIZED = "executor_unauthorized"
REJECT_IMAGE_MISSING = "python_image_not_provisioned"
REJECT_IMAGE_DIGEST = "python_image_digest_mismatch"
REJECT_READINESS = "executor_readiness_failed"
REJECT_QUOTA = "executor_quota_exceeded"
REJECT_CODE_INVALID = "executor_code_rejected"


@dataclass(frozen=True)
class ExecutionRequest:
    """The COMPLETE client-controlled surface: code + authenticated identity only.

    No image, argv, mounts, network, limits, env or flags may be supplied — they
    are fixed by the service.
    """

    code: str
    user_id: int
    run_id: str
    tenant_id: Optional[str] = None
    token: Optional[str] = None


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    reason: Optional[str] = None
    text: str = ""
    is_error: bool = True


class ExecutorService:
    """Test-only prototype of the guard-ordering boundary. LAUNCHES NOTHING."""

    def __init__(self, *, token: str, broker: Optional[SingleBroker] = None) -> None:
        self._token = token
        self.broker = broker or SingleBroker()
        self.started = []
        self.rejections = []

    def _reject(self, reason: str) -> ExecutionResult:
        self.rejections.append(reason)
        return ExecutionResult(ok=False, reason=reason, text="", is_error=True)

    def _authorized(self, token: Optional[str]) -> bool:
        return hmac.compare_digest(token or "", self._token)

    def prepare(self, req: ExecutionRequest, *,
                expected_digest: Optional[str] = None) -> Tuple[Optional[ExecutionPlan], Optional[str]]:
        """ALL guards, in order, BEFORE anything is recorded as started."""
        if not self._authorized(req.token):
            return None, REJECT_UNAUTHORIZED
        code = req.code or ""
        if (not isinstance(code, str) or not code.strip()
                or len(code.encode("utf-8")) > MAX_CODE_BYTES):
            return None, REJECT_CODE_INVALID
        kw = {} if expected_digest is None else {"expected_digest": expected_digest}
        ok, reason = image.readiness(**kw)
        if not ok:
            return None, (REJECT_IMAGE_DIGEST if reason == "python_image_digest_mismatch"
                          else REJECT_IMAGE_MISSING)
        if not self.broker.try_acquire(req.user_id):
            return None, REJECT_QUOTA
        name = CONTAINER_PREFIX + uuid.uuid4().hex[:10]
        return build_plan(code, name=name), None

    def execute(self, req: ExecutionRequest, *, runner=None,
                expected_digest: Optional[str] = None) -> ExecutionResult:
        plan, reason = self.prepare(req, expected_digest=expected_digest)
        if plan is None:
            return self._reject(reason)
        # Only non-source bookkeeping is ever recorded — never the code itself.
        self.started.append({"run_id": req.run_id, "user_id": req.user_id})
        try:
            if runner is None:
                return ExecutionResult(ok=True, reason=None, text="", is_error=False)
            return runner(plan)
        finally:
            self.broker.release(req.user_id)
