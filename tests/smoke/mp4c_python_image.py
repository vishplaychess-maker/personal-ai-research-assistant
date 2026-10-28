"""MP-4C — TEST-ONLY shim over the PROMOTED image pin.

The authoritative pin and local-inspection helpers now live in the separately
runnable executor service (`executor_service/image.py`), so this suite and the
adversarial probe exercise the SAME constants the service runs. Kept as a module
so existing imports keep working.

NOTE: to patch the pin/readiness in a test, patch `executor_service.image` — the
real module — not this re-export.
"""
from __future__ import annotations

from executor_service.image import (  # noqa: F401
    IMAGE_AMD64_DIGEST,
    IMAGE_INDEX_DIGEST,
    IMAGE_REF,
    IMAGE_VERSION,
    PINNED_REF,
    PLATFORM,
    PROVISION_COMMANDS,
    _docker,
    local_repo_digests,
    pinned_reference_is_digest_form,
    readiness,
)

__all__ = [
    "IMAGE_REF", "IMAGE_VERSION", "PLATFORM", "IMAGE_INDEX_DIGEST",
    "IMAGE_AMD64_DIGEST", "PINNED_REF", "PROVISION_COMMANDS", "local_repo_digests",
    "readiness", "pinned_reference_is_digest_form",
]
